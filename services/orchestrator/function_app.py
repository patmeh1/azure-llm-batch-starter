from __future__ import annotations

import atexit
import json
import logging
import threading
from datetime import UTC, datetime, timedelta

import azure.durable_functions as df
import azure.functions as func

from llm_batch_starter.adaptive import ADAPTIVE_ARMS, update_admission
from llm_batch_starter.adaptive_cloud import AdaptiveActivities
from llm_batch_starter.azure_state import AzureState, create_cloud_run, valid_run_id
from llm_batch_starter.cloud_activities import CloudActivities
from llm_batch_starter.complex_workload import configure, generate_complex
from llm_batch_starter.config import WORKFLOW_VERSION, load_settings
from llm_batch_starter.multi_cloud import MultiActivities
from llm_batch_starter.multi_deployment import ARMS, update_router
from llm_batch_starter.prepare import estimate_cost
from llm_batch_starter.quota import update_reservations
from llm_batch_starter.synthetic import generate_dataset

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)

_adaptive_service: AdaptiveActivities | None = None
_adaptive_lock = threading.Lock()


def adaptive_service() -> AdaptiveActivities:
    global _adaptive_service
    with _adaptive_lock:
        if _adaptive_service is None:
            _adaptive_service = AdaptiveActivities()
            atexit.register(_adaptive_service.close)
        return _adaptive_service


@app.orchestration_trigger(context_name="context")
def adaptive_workflow(context: df.DurableOrchestrationContext):
    return (yield from adaptive_steps(context))


def adaptive_steps(context: df.DurableOrchestrationContext):
    root = context.instance_id
    prepared = yield context.call_activity_with_retry(
        "v5_activity",
        df.RetryOptions(5000, 3),
        {"operation": "bootstrap", **context.get_input(), "run_id": root},
    )
    lease = df.EntityId("multi_router", "gpt5-mini-matched")
    acquired = yield context.call_entity(lease, "acquire", {"owner": root})
    if not acquired["acquired"]:
        raise RuntimeError("Another V4/V5 experiment holds the routing lease; investigate it first")
    spec, completed = prepared["specification"], []
    for arm in ADAPTIVE_ARMS:
        if completed:
            yield context.create_timer(
                v4_time(context) + timedelta(seconds=spec["cooldown_seconds"])
            )
        for stage in (arm + "-warmup", arm):
            started = yield context.call_activity_with_retry(
                "v5_activity",
                df.RetryOptions(2000, 3),
                {
                    "operation": "begin",
                    "run_id": root,
                    "arm": stage,
                },
            )
            router = df.EntityId("adaptive_router", f"{root}-{stage}")
            yield context.call_entity(
                router,
                "begin",
                {
                    "owner": root,
                    "stage": stage,
                    "now": started["started_at"],
                    **prepared,
                },
            )
            context.set_custom_status({"phase": stage, "completed_stages": completed})
            tasks = [
                context.call_sub_orchestrator(
                    "adaptive_worker",
                    {"run_id": root, "arm": stage, "worker": worker},
                    instance_id=f"{root}-{stage}-worker-{worker}",
                    version=context.version,
                )
                for worker in range(spec["workers"])
            ]
            yield context.task_all(tasks)
            snapshot = yield context.call_entity(
                router,
                "snapshot",
                {
                    "owner": root,
                    "now": v4_time(context).isoformat(),
                },
            )
            if snapshot["completed"] != snapshot["expected"]:
                raise RuntimeError("V5 admission queue did not reconcile every record")
            yield context.call_activity_with_retry(
                "v5_activity",
                df.RetryOptions(2000, 3),
                {
                    "operation": "save_admission",
                    "run_id": root,
                    "arm": stage,
                    "snapshot": snapshot,
                },
            )
            result = yield context.call_activity_with_retry(
                "v5_activity",
                df.RetryOptions(2000, 3),
                {
                    "operation": "finalize_arm",
                    "run_id": root,
                    "arm": stage,
                },
            )
            report = result["report"]
            completed.append(
                {
                    "arm": stage,
                    **{
                        key: report[key]
                        for key in (
                            "status",
                            "succeeded",
                            "failed",
                            "pending",
                            "elapsed_seconds",
                            "errors_429",
                            "http_200_latency_mean_seconds",
                            "all_record_elapsed_mean_seconds",
                        )
                    },
                }
            )
            context.set_custom_status({"phase": stage, "completed_stages": completed})
            if stage.endswith("-warmup"):
                if report["failed"]:
                    yield context.call_activity_with_retry(
                        "v5_activity",
                        df.RetryOptions(2000, 3),
                        {"operation": "cancel", "run_id": root},
                    )
                else:
                    yield context.create_timer(
                        v4_time(context) + timedelta(seconds=spec["cooldown_seconds"])
                    )
    result = yield context.call_activity_with_retry(
        "v5_activity", df.RetryOptions(2000, 3), {"operation": "finalize", "run_id": root}
    )
    if not result["router_reconciliation_required"]:
        yield context.call_entity(lease, "release", {"owner": root})
    return result


@app.orchestration_trigger(context_name="context")
def adaptive_worker(context: df.DurableOrchestrationContext):
    return (yield from adaptive_worker_steps(context))


def adaptive_worker_steps(context: df.DurableOrchestrationContext):
    options = context.get_input()
    root, stage, worker = options["run_id"], options["arm"], options["worker"]
    router = df.EntityId("adaptive_router", f"{root}-{stage}")
    sequence, completed = 0, 0
    while True:
        cancelled = yield context.call_activity_with_retry(
            "v5_activity", df.RetryOptions(2000, 3), {"operation": "control", "run_id": root}
        )
        key = f"{worker}-{sequence}"
        grant = yield context.call_entity(
            router,
            "claim",
            {
                "owner": root,
                "key": key,
                "now": v4_time(context).isoformat(),
                **cancelled,
            },
        )
        if grant.get("done"):
            return {"worker": worker, "processed": completed}
        if grant.get("wait_until"):
            yield context.create_timer(datetime.fromisoformat(grant["wait_until"]))
            continue
        item = {"run_id": root, "arm": stage, **grant}
        if grant.get("terminal_error"):
            result = yield context.call_activity_with_retry(
                "v5_activity",
                df.RetryOptions(2000, 3),
                {
                    "operation": "finish_record",
                    **item,
                    "error": grant["terminal_error"],
                },
            )
        else:
            result = yield context.call_activity("v5_activity", {"operation": "attempt", **item})
        yield context.call_entity(
            router,
            "ack",
            {
                "owner": root,
                "key": key,
                "now": v4_time(context).isoformat(),
                "response": result,
            },
        )
        completed += int(result["terminal"])
        sequence += 1
        context.set_custom_status(
            {
                "arm": stage,
                "worker": worker,
                "processed": completed,
                "last_progress_at": v4_time(context).isoformat(),
            }
        )


@app.entity_trigger(context_name="context")
def adaptive_router(context: df.DurableEntityContext):
    state, result = update_admission(
        context.get_state(lambda: {}),
        context.operation_name,
        context.get_input(),
    )
    context.set_state(state)
    context.set_result(result)


@app.activity_trigger(input_name="item")
def v5_activity(item: dict):
    activities = adaptive_service()
    operation = item["operation"]
    if operation == "bootstrap":
        return activities.bootstrap(item)
    if operation == "begin":
        return activities.begin_arm(item["run_id"], item["arm"])
    if operation == "control":
        return activities.control(item["run_id"])
    if operation == "cancel":
        return activities.cancel(item["run_id"])
    if operation == "attempt":
        return activities.attempt(item)
    if operation == "finish_record":
        return activities.finish_record(item)
    if operation == "save_admission":
        return activities.save_admission(item["run_id"], item["arm"], item["snapshot"])
    if operation == "finalize_arm":
        return activities.finalize_arm(item["run_id"], item["arm"])
    if operation == "finalize":
        return activities.finalize(item["run_id"])
    raise ValueError("Unsupported V5 activity operation")


def v4_time(context: df.DurableOrchestrationContext) -> datetime:
    value = context.current_utc_datetime
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@app.orchestration_trigger(context_name="context")
def multi_deployment_workflow(context: df.DurableOrchestrationContext):
    return (yield from multi_deployment_steps(context))


def multi_deployment_steps(context: df.DurableOrchestrationContext):
    options = dict(context.get_input())
    root = context.instance_id
    options["run_id"] = root
    prepared = yield context.call_activity_with_retry(
        "v4_activity", df.RetryOptions(5000, 3), {"operation": "bootstrap", **options}
    )
    router = df.EntityId("multi_router", "gpt5-mini-matched")
    acquired = yield context.call_entity(router, "acquire", {"owner": root})
    if not acquired["acquired"]:
        raise RuntimeError(
            "Another v4 experiment holds the routing lease; inspect it rather than running competing traffic"
        )
    spec = prepared["specification"]
    completed = []
    for arm in ARMS:
        if completed:
            yield context.create_timer(
                v4_time(context) + timedelta(seconds=spec["cooldown_seconds"])
            )
        yield context.call_entity(
            router,
            "begin",
            {
                "owner": root,
                "arm": arm,
                "specification": spec,
                "now": v4_time(context).isoformat(),
            },
        )
        yield context.call_activity(
            "v4_activity", {"operation": "begin", "run_id": root, "arm": arm}
        )
        context.set_custom_status({"phase": arm, "completed_stages": completed})
        tasks = [
            context.call_sub_orchestrator(
                "multi_deployment_group",
                {"run_id": root, "arm": arm, "group": group, **prepared},
                instance_id=f"{root}-{arm}-worker-{group}",
                version=context.version,
            )
            for group in range(spec["workers"])
        ]
        yield context.task_all(tasks)
        result = yield context.call_activity(
            "v4_activity", {"operation": "finalize_arm", "run_id": root, "arm": arm}
        )
        report = result["report"]
        completed.append(
            {
                "arm": arm,
                **{
                    key: report[key]
                    for key in (
                        "status",
                        "succeeded",
                        "failed",
                        "pending",
                        "elapsed_seconds",
                        "errors_429",
                        "semantic_status",
                    )
                },
            }
        )
        context.set_custom_status({"phase": arm, "completed_stages": completed})
    result = yield context.call_activity("v4_activity", {"operation": "finalize", "run_id": root})
    if not result["router_reconciliation_required"]:
        yield context.call_entity(router, "release", {"owner": root})
    return result


@app.orchestration_trigger(context_name="context")
def multi_deployment_group(context: df.DurableOrchestrationContext):
    return (yield from multi_group_steps(context))


def multi_group_steps(context: df.DurableOrchestrationContext):
    options = context.get_input()
    root, arm, group, spec = (
        options["run_id"],
        options["arm"],
        options["group"],
        options["specification"],
    )
    router = df.EntityId("multi_router", "gpt5-mini-matched")
    size = spec["count"] // spec["workers"]
    for index in range(group * size, (group + 1) * size):
        waited = 0.0
        terminal = False
        for attempt in range(1, spec["max_attempts"] + 1):
            item = {"run_id": root, "arm": arm, "index": index, "attempt": attempt}
            while True:
                status = yield context.call_activity(
                    "v4_activity", {"operation": "record_state", **item}
                )
                if status["terminal"]:
                    terminal = True
                    break
                key = f"{arm}-{index}-{attempt}"
                route = yield context.call_entity(
                    router,
                    "route",
                    {
                        "owner": root,
                        "arm": arm,
                        "key": key,
                        "now": v4_time(context).isoformat(),
                        "estimated_cost_usd": options["request_costs"][index],
                    },
                )
                if route.get("terminal_error"):
                    yield context.call_activity(
                        "v4_activity",
                        {
                            "operation": "finish_record",
                            **item,
                            "error": route["terminal_error"],
                            "router_wait_seconds": waited,
                        },
                    )
                    terminal = True
                    break
                if route.get("wait_until"):
                    before = v4_time(context)
                    until = min(
                        datetime.fromisoformat(route["wait_until"]), before + timedelta(seconds=30)
                    )
                    yield context.create_timer(until)
                    waited += max(0, (v4_time(context) - before).total_seconds())
                    continue
                response = yield context.call_activity(
                    "v4_activity",
                    {
                        "operation": "attempt",
                        **item,
                        "target": route["target"],
                        "router_wait_seconds": waited,
                    },
                )
                if response.get("retryable"):
                    until = datetime.fromisoformat(response["completed_at"]) + timedelta(
                        seconds=response["retry_after"]
                    )
                    yield context.call_entity(
                        router,
                        "cooldown",
                        {
                            "owner": root,
                            "arm": arm,
                            "key": key,
                            "until": until.isoformat(),
                        },
                    )
                terminal = response["terminal"]
                break
            if terminal:
                break
        context.set_custom_status(
            {
                "arm": arm,
                "group": group,
                "processed": index - group * size + 1,
                "total": size,
                "last_progress_at": v4_time(context).isoformat(),
            }
        )
    return {"arm": arm, "group": group, "processed": size}


@app.entity_trigger(context_name="context")
def multi_router(context: df.DurableEntityContext):
    state, result = update_router(
        context.get_state(lambda: {}), context.operation_name, context.get_input()
    )
    context.set_state(state)
    context.set_result(result)


@app.activity_trigger(input_name="item")
def v4_activity(item: dict):
    activities = MultiActivities()
    try:
        operation = item["operation"]
        if operation == "bootstrap":
            return activities.bootstrap(item)
        if operation == "begin":
            return activities.begin_arm(item["run_id"], item["arm"])
        if operation == "record_state":
            return activities.record_state(item)
        if operation == "attempt":
            return activities.attempt(item)
        if operation == "finish_record":
            return activities.finish_record(item)
        if operation == "finalize_arm":
            return activities.finalize_arm(item["run_id"], item["arm"])
        if operation == "finalize":
            return activities.finalize(item["run_id"])
        raise ValueError("Unsupported v4 activity operation")
    finally:
        activities.close()


@app.orchestration_trigger(context_name="context")
def synthetic_workflow(context: df.DurableOrchestrationContext):
    options = context.get_input()
    options["run_id"] = context.instance_id
    yield context.call_activity_with_retry("bootstrap_synthetic", df.RetryOptions(5000, 3), options)
    result = yield context.call_sub_orchestrator(
        "batch_workflow",
        context.instance_id,
        instance_id=f"{context.instance_id}-processor",
        version=context.version,
    )
    return result


@app.activity_trigger(input_name="options")
def bootstrap_synthetic(options: dict):
    count, seed, reference = (
        int(options["count"]),
        int(options["seed"]),
        int(options.get("reference_tokens", 0)),
    )
    if not 1 <= count <= 10000 or not 0 <= reference <= 120000:
        raise ValueError("Synthetic input exceeds the bounded demo limits")
    settings = load_settings(auth_mode="managed_identity")
    probe = generate_dataset(1, seed=seed, reference_tokens=reference)
    if (
        estimate_cost(probe, settings, batch=True)["estimated_inference_usd"] * count
        > settings.max_run_cost_usd
    ):
        raise ValueError("Synthetic input exceeds the server-side inference estimate budget")
    if len(probe[0].text.encode()) * count > 40_000_000:
        raise ValueError("Synthetic manifest is too large; split this demo run")
    records = generate_dataset(count, seed=seed, reference_tokens=reference)
    run = create_cloud_run(records, settings, run_id=options["run_id"], enqueue=False)
    return {"run_id": run["id"], "record_count": count}


@app.orchestration_trigger(context_name="context")
def complex_workflow(context: df.DurableOrchestrationContext):
    options = context.get_input()
    options["run_id"] = context.instance_id
    yield context.call_activity_with_retry("bootstrap_complex", df.RetryOptions(5000, 3), options)
    return (
        yield context.call_sub_orchestrator(
            "batch_workflow",
            context.instance_id,
            instance_id=f"{context.instance_id}-processor",
            version=context.version,
        )
    )


@app.activity_trigger(input_name="options")
def bootstrap_complex(options: dict):
    settings, profile = configure(load_settings(auth_mode="managed_identity"))
    if options["workload_profile"]["prompt_bundle_sha256"] != profile["prompt_bundle_sha256"]:
        raise ValueError(
            "Client/worker complex Markdown prompt bundles differ; redeploy or update the client"
        )
    if options["expected_model"] != [settings.model_name, settings.model_version]:
        raise ValueError("Client and deployed complex model/version differ")
    records = generate_complex(int(options["count"]), int(options["seed"]))
    run = create_cloud_run(
        records, settings, run_id=options["run_id"], enqueue=False, workload_profile=profile
    )
    return {"run_id": run["id"], "record_count": len(records), "workload_profile": profile}


@app.orchestration_trigger(context_name="context")
def cancel_workflow(context: df.DurableOrchestrationContext):
    return (yield context.call_activity("request_run_cancellation", context.get_input()))


@app.activity_trigger(input_name="options")
def request_run_cancellation(options: dict):
    run_id = valid_run_id(options["run_id"])
    store = AzureState(load_settings(auth_mode="managed_identity"))
    try:
        store.save("control", run_id, "cancel", {"requested": True})
        return {"run_id": run_id, "status": "cancellation_requested"}
    finally:
        store.close()


@app.orchestration_trigger(context_name="context")
def retry_workflow(context: df.DurableOrchestrationContext):
    options = context.get_input()
    options["run_id"] = context.instance_id
    prepared = yield context.call_activity_with_retry(
        "bootstrap_retry", df.RetryOptions(5000, 3), options
    )
    context.set_custom_status(prepared)
    return (
        yield context.call_sub_orchestrator(
            "batch_workflow",
            context.instance_id,
            instance_id=f"{context.instance_id}-processor",
            version=context.version,
        )
    )


@app.activity_trigger(input_name="options")
def bootstrap_retry(options: dict):
    activities = CloudActivities()
    try:
        return activities.prepare_retry(
            valid_run_id(options.get("parent_artifact_run_id", options["parent_run_id"])),
            valid_run_id(options["run_id"]),
        )
    finally:
        activities.close()


@app.orchestration_trigger(context_name="context")
def recovery_workflow(context: df.DurableOrchestrationContext):
    return (yield from recovery_steps(context))


def recovery_steps(context: df.DurableOrchestrationContext):
    run_id = context.get_input()["artifact_run_id"]
    details = yield context.call_activity_with_retry(
        "prepare_run", df.RetryOptions(5000, 3), run_id
    )
    context.set_custom_status({"run_id": run_id, "parts": details["parts"], "stage": "recovering"})
    tasks = [
        context.call_sub_orchestrator(
            "batch_partition",
            {"run_id": run_id, "part": part, "limits": details},
            instance_id=f"{context.instance_id}-{part['id']}",
            version=context.version,
        )
        for part in details["parts"]
    ]
    yield context.task_all(tasks)
    return (yield context.call_activity("finalize_run", run_id))


@app.queue_trigger(
    arg_name="message", queue_name="batch-requests", connection="AzureWebJobsStorage"
)
@app.durable_client_input(client_name="client")
async def start_run(message: func.QueueMessage, client: df.DurableOrchestrationClient):
    value = json.loads(message.get_body())
    run_id = valid_run_id(value["run_id"])
    if value.get("action") == "cancel":
        store = AzureState(load_settings(auth_mode="managed_identity"))
        try:
            store.save("control", run_id, "cancel", {"requested": True})
        finally:
            store.close()
        return
    if value.get("action") != "start":
        raise ValueError("Unsupported queue action")
    existing = await client.get_status(run_id)
    if existing is None:
        await client.start_new(
            "batch_workflow", instance_id=run_id, client_input=run_id, version=WORKFLOW_VERSION
        )
    logging.info("Batch starter accepted run %s", run_id)


@app.orchestration_trigger(context_name="context")
def batch_workflow(context: df.DurableOrchestrationContext):
    run_id = context.get_input()
    details = yield context.call_activity_with_retry(
        "prepare_run", df.RetryOptions(5000, 3), run_id
    )
    context.set_custom_status({"run_id": run_id, "parts": details["parts"], "stage": "monitoring"})
    tasks = [
        context.call_sub_orchestrator(
            "batch_partition",
            {"run_id": run_id, "part": part, "limits": details},
            instance_id=f"{run_id}-{part['id']}",
            version=context.version,
        )
        for part in details["parts"]
    ]
    yield context.task_all(tasks)
    result = yield context.call_activity("finalize_run", run_id)
    context.set_custom_status(result)
    return result


@app.orchestration_trigger(context_name="context")
def batch_partition(context: df.DurableOrchestrationContext):
    return (yield from partition_steps(context))


def partition_steps(context: df.DurableOrchestrationContext):
    item = context.get_input()
    run_id, part, limits = item["run_id"], item["part"], item["limits"]
    key = f"{run_id}-{part['id']}"
    coordinator = df.EntityId("batch_budget", "batch-deployment")
    deadline = context.current_utc_datetime + timedelta(seconds=limits["cloud_timeout_seconds"])
    while True:
        # Preserve the original unversioned activity sequence during deterministic replay.
        if context.version:
            cancelled = yield context.call_activity_with_retry(
                "check_partition_cancellation",
                df.RetryOptions(5000, 3),
                {"run_id": run_id, "part_id": part["id"]},
            )
            if cancelled:
                result = {
                    "terminal": True,
                    "release": False,
                    "status": "cancelled_before_submission",
                }
                context.set_custom_status(result)
                return result
        if context.current_utc_datetime >= deadline:
            yield context.call_activity(
                "cancel_partition", {"run_id": run_id, "part_id": part["id"]}
            )
            return {"terminal": True, "release": False, "status": "admission_timeout"}
        acquired = yield context.call_entity(
            coordinator,
            "acquire",
            {
                "key": key,
                "tokens": part["tokens"],
                "max_tokens": limits["max_enqueued_tokens"],
                "max_active": limits["max_active_batches"],
            },
        )
        if acquired:
            break
        context.set_custom_status({"terminal": False, "status": "waiting_for_admission"})
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=60))
    interval = limits["poll_seconds"]
    cancellation_requested = False
    while True:
        if context.current_utc_datetime >= deadline and not cancellation_requested:
            yield context.call_activity(
                "cancel_partition", {"run_id": run_id, "part_id": part["id"]}
            )
            cancellation_requested = True
        if context.current_utc_datetime >= deadline + timedelta(minutes=30):
            return {"terminal": True, "release": False, "status": "cancellation_needs_attention"}
        result = yield context.call_activity_with_retry(
            "step_partition",
            df.RetryOptions(5000, 3),
            {
                "run_id": run_id,
                "part_id": part["id"],
            },
        )
        context.set_custom_status(result)
        if result["terminal"]:
            if result["release"]:
                yield context.call_entity(coordinator, "release", {"key": key})
            return result
        delay = max(interval, result.get("retry_after", 0))
        yield context.create_timer(context.current_utc_datetime + timedelta(seconds=delay))
        interval = min(limits["max_poll_seconds"], interval * 1.5)


@app.entity_trigger(context_name="context")
def batch_budget(context: df.DurableEntityContext):
    state = context.get_state(lambda: {})
    value = context.get_input()
    state, allowed = update_reservations(state, context.operation_name, value)
    context.set_result(allowed)
    context.set_state(state)


@app.activity_trigger(input_name="run_id")
def prepare_run(run_id: str):
    activities = CloudActivities()
    try:
        return activities.prepare(run_id)
    finally:
        activities.close()


@app.activity_trigger(input_name="item")
def step_partition(item: dict):
    activities = CloudActivities()
    try:
        return activities.step(item["run_id"], item["part_id"])
    finally:
        activities.close()


@app.activity_trigger(input_name="run_id")
def finalize_run(run_id: str):
    activities = CloudActivities()
    try:
        return activities.finalize(run_id)
    finally:
        activities.close()


@app.activity_trigger(input_name="item")
def check_partition_cancellation(item: dict):
    activities = CloudActivities()
    try:
        return activities.admission_cancelled(item["run_id"], item["part_id"])
    finally:
        activities.close()


@app.activity_trigger(input_name="item")
def cancel_partition(item: dict):
    activities = CloudActivities()
    try:
        activities.request_cancel(item["run_id"], item["part_id"])
    finally:
        activities.close()
