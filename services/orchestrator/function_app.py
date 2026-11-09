from __future__ import annotations

import json
import logging
from datetime import timedelta

import azure.durable_functions as df
import azure.functions as func

from llm_batch_starter.azure_state import AzureState, create_cloud_run, valid_run_id
from llm_batch_starter.cloud_activities import CloudActivities
from llm_batch_starter.complex_workload import configure, generate_complex
from llm_batch_starter.config import WORKFLOW_VERSION, load_settings
from llm_batch_starter.prepare import estimate_cost
from llm_batch_starter.quota import update_reservations
from llm_batch_starter.synthetic import generate_dataset

app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)


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
