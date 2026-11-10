import copy
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import openai
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from typer.testing import CliRunner

from llm_batch_starter.cli import app
from llm_batch_starter.complex_workload import Expected
from llm_batch_starter.config import WORKFLOW_VERSION, Settings
from llm_batch_starter.multi_cloud import MultiActivities
from llm_batch_starter.multi_deployment import (
    ARMS,
    MultiDeploymentSpec,
    export_multi,
    plan_multi,
    public_targets,
    retry_seconds,
    update_router,
    v4_settings,
)
from llm_batch_starter.scheduler import SchedulerAccess
from llm_batch_starter.state import State
from services.orchestrator.function_app import multi_deployment_steps, multi_group_steps

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)


def begin_router(arm="single", **overrides):
    spec = MultiDeploymentSpec(count=5, **overrides)
    state, _ = update_router({}, "acquire", {"owner": "test"})
    state, _ = update_router(
        state,
        "begin",
        {
            "owner": "test",
            "arm": "single",
            "specification": spec.model_dump(),
            "now": NOW.isoformat(),
        },
    )
    if arm == "pool":
        state, _ = update_router(
            state,
            "begin",
            {
                "owner": "test",
                "arm": "pool",
                "specification": spec.model_dump(),
                "now": NOW.isoformat(),
            },
        )
    return state


def route(state, key="record-0-1", now=NOW, cost=0.01):
    return update_router(
        state,
        "route",
        {
            "owner": "test",
            "arm": state["arm"],
            "key": key,
            "now": now.isoformat(),
            "estimated_cost_usd": cost,
        },
    )


def test_equal_aggregate_quota_and_unchanged_prompt_in_preview():
    plan = plan_multi(MultiDeploymentSpec(), Settings())
    assert plan["status"] == "planned_not_executed"
    assert plan["conservative_all_attempts_estimate_usd"] < 10
    for arm in ARMS:
        assert sum(t["allocated_tpm"] for t in public_targets(arm)) == 150000
        assert sum(t["allocated_rpm"] for t in public_targets(arm)) == 150
    assert len(public_targets("pool")) == 3
    assert (
        plan["workload_profile"]["prompt_bundle_sha256"]
        == "469532c76922192658dd12a81cd842a8b2328d529db6acbb4c433385e30cb4fe"
    )


def test_pool_round_robin_skips_only_the_throttled_target():
    state = begin_router("pool")
    state, first = route(state, "a")
    assert first["target"] == "pool-1"
    state, _ = update_router(
        state,
        "cooldown",
        {
            "owner": "test",
            "arm": "pool",
            "key": "a",
            "until": (NOW + timedelta(seconds=60)).isoformat(),
        },
    )
    seen = []
    for key in ["b", "c", "d", "e"]:
        state, result = route(state, key)
        seen.append(result["target"])
    assert seen == ["pool-2", "pool-3", "pool-2", "pool-3"]
    state, result = route(state, "later", now=NOW + timedelta(seconds=61))
    assert result["target"] == "pool-1"


def test_all_targets_cooling_wait_without_spending_an_attempt():
    state = begin_router()
    state, _ = route(state)
    state, _ = update_router(
        state,
        "cooldown",
        {
            "owner": "test",
            "arm": "single",
            "key": "record-0-1",
            "until": (NOW + timedelta(seconds=60)).isoformat(),
        },
    )
    before = copy.deepcopy(state)
    state, result = route(state, "record-0-2")
    assert result["wait_until"] == (NOW + timedelta(seconds=60)).isoformat()
    assert state == before


def test_router_replay_does_not_reset_reservations_deadline_or_budget():
    state = begin_router()
    state, result = route(state)
    replayed, repeated = route(state)
    assert replayed == state and repeated == result
    state, _ = update_router(
        state,
        "begin",
        {
            "owner": "test",
            "arm": "single",
            "specification": MultiDeploymentSpec(count=5).model_dump(),
            "now": (NOW + timedelta(hours=1)).isoformat(),
        },
    )
    assert state["attempts"] == 1
    _, expired = route(state, "new", now=NOW + timedelta(hours=3))
    assert expired["terminal_error"] == "admission_deadline"


def test_router_cost_attempt_and_owner_guards():
    state = begin_router(max_estimated_inference_usd=0.02)
    state, _ = route(state, cost=0.01)
    _, denied = route(state, "second", cost=0.01)
    assert denied["terminal_error"] == "attempt_or_cost_budget_exhausted"
    _, other = update_router(state, "acquire", {"owner": "other"})
    assert not other["acquired"]
    with pytest.raises(ValueError, match="owned"):
        update_router(state, "release", {"owner": "other"})
    state = begin_router()
    for index in range(20):
        state, result = route(state, str(index))
        assert "target" in result
    _, result = route(state, "twenty-first")
    assert result["terminal_error"] == "attempt_or_cost_budget_exhausted"


@pytest.mark.parametrize(
    "headers,expected",
    [
        ({"retry-after-ms": "1500"}, 1.5),
        ({"Retry-After": "30"}, 30),
        ({"Retry-After": "Tue, 10 Nov 2026 12:01:00 GMT"}, 60),
        ({"retry-after-ms": "0"}, 0),
    ],
)
def test_service_retry_guidance_takes_precedence(headers, expected):
    assert retry_seconds(headers, 1, now=NOW) == expected


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "invalid"])
def test_invalid_retry_headers_use_a_bounded_finite_fallback(value):
    delay = retry_seconds({"retry-after-ms": value}, 1, now=NOW)
    assert 30 <= delay <= 33


class Store:
    def __init__(self):
        self.blobs, self.runs, self.documents = {}, {}, {}

    def put_blob(self, name, data, *, overwrite=False):
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("Already exists")
        self.blobs[name] = bytes(data)

    def get_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("Not found")
        return self.blobs[name]

    def save_run(self, value, *, create=False):
        if create and value["id"] in self.runs:
            raise ResourceExistsError("Already exists")
        self.runs[value["id"]] = copy.deepcopy(value)

    def get_run(self, run_id):
        return copy.deepcopy(self.runs[run_id])

    def save(self, table, run_id, key, value):
        self.documents[(table, run_id, key)] = copy.deepcopy(value)

    def get_item(self, table, run_id, key):
        return copy.deepcopy(self.documents.get((table, run_id, key)))

    def items(self, table, run_id):
        return [
            copy.deepcopy(value)
            for (kind, rid, _), value in self.documents.items()
            if kind == table and rid == run_id
        ]

    def add_event(self, run_id, value):
        self.save("events", run_id, value["event_id"], value)


def gold_completion(record):
    expected = Expected.model_validate(record.expected_result)
    score = expected.minimum_confidence
    result = {
        **expected.fields,
        "confidence": score,
        "confidence_basis": expected.archived_basis
        if score == expected.archived_score
        else "Direct synthetic evidence supports this result.",
    }
    return {
        "model": "gpt-5-mini-2025-08-07",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
        "usage": {"prompt_tokens": 30500, "completion_tokens": 350, "total_tokens": 30850},
    }


def bootstrap(provider_factory):
    store = Store()
    settings, profile = v4_settings(Settings())
    activities = MultiActivities(settings, store, provider_factory)
    spec = MultiDeploymentSpec(count=5)
    activities.bootstrap(
        {
            "run_id": "test-root",
            "count": 10,
            "specification": spec.model_dump(),
            "workload_profile": profile,
            "expected_model": ["gpt-5-mini", "2025-08-07"],
        }
    )
    activities.begin_arm("test-root", "single")
    return activities, store


def attempt_item(**overrides):
    return {
        "run_id": "test-root",
        "arm": "single",
        "index": 0,
        "attempt": 1,
        "target": "control",
        "router_wait_seconds": 0,
        **overrides,
    }


def test_interrupted_paid_intent_is_not_blindly_resent():
    calls = []

    class Interrupted:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            raise RuntimeError("Crash after sending")

        def close(self):
            pass

    activities, store = bootstrap(Interrupted)
    with pytest.raises(RuntimeError, match="Crash"):
        activities.attempt(attempt_item())
    result = activities.attempt(attempt_item())
    assert result["terminal"]
    assert len(calls) == 1
    assert store.items("records", "test-root-single")[0]["error"] == "ambiguous_online_result"


def test_throttle_replay_records_one_attempt_and_preserves_retry_directive():
    calls = []

    class Throttled:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            response = httpx.Response(
                429,
                headers={"retry-after-ms": "45000"},
                request=httpx.Request("POST", "https://example.openai.azure.com"),
            )
            raise openai.RateLimitError(
                "rate limit exceeded",
                response=response,
                body={"error": {"code": "rate_limit", "message": "rate limit exceeded"}},
            )

        def close(self):
            pass

    activities, store = bootstrap(Throttled)
    first = activities.attempt(attempt_item())
    again = activities.attempt(attempt_item())
    assert first == again and first["retry_after"] == 45 and not first["terminal"]
    assert len(calls) == 1
    assert (
        len([e for e in store.items("events", "test-root-single") if e["stage"] == "http_attempt"])
        == 1
    )


def test_two_arms_reconcile_with_withheld_gold_and_sanitized_export(tmp_path):
    calls = []

    class Provider:
        def __init__(self, settings):
            self.settings = settings

        def complete(self, record):
            calls.append((self.settings.online_deployment, record.id))
            limit = "150000" if self.settings.online_deployment == "gpt5-mini-online" else "50000"
            return gold_completion(record), {
                "x-ratelimit-limit-tokens": limit,
                "x-ratelimit-limit-requests": "150" if limit == "150000" else "50",
            }

        def close(self):
            pass

    activities, store = bootstrap(Provider)
    for arm in ARMS:
        activities.begin_arm("test-root", arm)
        for index in range(5):
            target = "control" if arm == "single" else f"pool-{index % 3 + 1}"
            result = activities.attempt(attempt_item(arm=arm, index=index, target=target))
            assert result["terminal"]
        report = activities.finalize_arm("test-root", arm)["report"]
        assert report["succeeded"] == report["quality"]["passed_records"] == 5
    final = activities.finalize("test-root")
    assert final["status"] == "succeeded"
    assert final["acceptance_status"] == "passed"
    assert len(calls) == 10
    assert store.get_blob("test-root/analysis.csv").count(b"\n") == 11
    private = tmp_path / "report.json"
    private.write_text(json.dumps(final["report"]))
    exported = export_multi(private, tmp_path / "public")
    assert exported["results_final"]
    assert len(exported["stages"]) == 2
    assert "gpt5-mini-v4-" not in json.dumps(exported)
    assert (tmp_path / "public" / "output-samples.json").exists()


def test_cancellation_and_deadline_do_not_issue_inference():
    def forbidden(settings):
        raise AssertionError("Must not call inference")

    activities, store = bootstrap(forbidden)
    store.save("control", "test-root", "cancel", {"requested": True})
    assert activities.attempt(attempt_item())["terminal"]
    assert store.items("records", "test-root-single")[0]["error"] == "cancelled_before_attempt"
    store.documents.pop(("control", "test-root", "cancel"))
    stage = store.get_run("test-root-single")
    stage["started_at"] = "2000-01-01T00:00:00+00:00"
    store.save_run(stage)
    assert activities.attempt(attempt_item(index=1))["terminal"]
    assert store.items("records", "test-root-single")[-1]["error"] == "admission_deadline"


def test_v4_cli_is_dry_run_without_yes(monkeypatch, tmp_path):
    scenario = tmp_path / "scenario.json"
    scenario.write_text(MultiDeploymentSpec(count=5).model_dump_json())
    monkeypatch.setattr(
        SchedulerAccess, "start_multi", lambda *args: pytest.fail("No paid start in preview")
    )
    result = CliRunner().invoke(app, ["v4-run", "--scenario", str(scenario)])
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "planned_not_executed"


def test_v4_starter_contains_no_raw_records(tmp_path, monkeypatch):
    monkeypatch.setattr("llm_batch_starter.scheduler.verify_topology", lambda *_: [])
    access = SchedulerAccess.__new__(SchedulerAccess)
    access.settings = Settings()
    access.registry = State(tmp_path / "state.sqlite3")
    scheduled = []
    access.client = SimpleNamespace(
        schedule_new_orchestration=lambda name, **kwargs: scheduled.append((name, kwargs))
    )
    try:
        run_id = access.start_multi(MultiDeploymentSpec(count=5))
        name, payload = scheduled[0]
        assert name == "multi_deployment_workflow" and payload["version"] == WORKFLOW_VERSION
        assert payload["input"]["count"] == 10
        assert "expected_result" not in json.dumps(payload)
        assert "CANARY-" not in json.dumps(payload)
        assert access.registry.get_run(run_id)["mode"] == "multi-deployment"
    finally:
        access.registry.close()


class Context:
    def __init__(self):
        self.instance_id = "test-root"
        self.current_utc_datetime = NOW
        self.version = WORKFLOW_VERSION
        self.options = {
            "run_id": self.instance_id,
            "arm": "pool",
            "group": 0,
            "specification": MultiDeploymentSpec(count=5).model_dump(),
            "request_costs": [0.01] * 5,
        }

    def get_input(self):
        return self.options

    def call_activity(self, name, value):
        return ("activity", value)

    def call_activity_with_retry(self, name, retry, value):
        return ("activity_retry", value)

    def call_entity(self, entity, operation, value):
        return ("entity", operation, value)

    def create_timer(self, deadline):
        return ("timer", deadline)

    def set_custom_status(self, value):
        self.status = value

    def call_sub_orchestrator(self, name, value, *, instance_id, version):
        return ("child", name, value, instance_id, version)

    def task_all(self, tasks):
        return ("all", tasks)


def test_worker_uses_durable_timers_and_retries_a_different_eligible_target():
    context = Context()
    steps = multi_group_steps(context)
    assert next(steps)[1]["operation"] == "record_state"
    assert steps.send({"terminal": False})[1] == "route"
    timer = steps.send({"wait_until": (NOW + timedelta(seconds=60)).isoformat()})
    assert timer == ("timer", NOW + timedelta(seconds=30))
    context.current_utc_datetime += timedelta(seconds=30)
    assert steps.send(None)[1]["operation"] == "record_state"
    assert steps.send({"terminal": False})[1] == "route"
    attempt = steps.send({"target": "pool-1"})
    assert attempt[1]["operation"] == "attempt" and attempt[1]["router_wait_seconds"] == 30
    assert (
        steps.send(
            {
                "terminal": False,
                "retryable": True,
                "retry_after": 45,
                "completed_at": context.current_utc_datetime.isoformat(),
            }
        )[1]
        == "cooldown"
    )
    assert steps.send({})[1]["operation"] == "record_state"
    assert steps.send({"terminal": False})[1] == "route"
    second = steps.send({"target": "pool-2"})
    assert second[1]["attempt"] == 2 and second[1]["target"] == "pool-2"
    with pytest.raises(StopIteration):
        steps.send({"terminal": True, "retryable": False})


def test_root_holds_one_routing_lease_and_sequences_matched_arms():
    context = Context()
    steps = multi_deployment_steps(context)
    assert next(steps)[1]["operation"] == "bootstrap"
    prepared = {
        "specification": MultiDeploymentSpec(count=5).model_dump(),
        "request_costs": [0.01] * 5,
    }
    assert steps.send(prepared)[1] == "acquire"
    assert steps.send({"acquired": True})[1] == "begin"
    assert steps.send({})[1]["operation"] == "begin"
    fanout = steps.send({})
    assert fanout[0] == "all" and len(fanout[1]) == 5
    assert all(task[2]["arm"] == "single" for task in fanout[1])


@pytest.mark.parametrize("ambiguous", [False, True])
def test_root_releases_the_lease_only_after_confirmed_terminal_outcomes(ambiguous):
    context = Context()
    steps = multi_deployment_steps(context)
    spec = MultiDeploymentSpec(count=5).model_dump()
    next(steps)
    steps.send({"specification": spec, "request_costs": [0.01] * 5})
    steps.send({"acquired": True})
    steps.send({})
    steps.send({})
    summary = {
        "status": "succeeded",
        "succeeded": 5,
        "failed": 0,
        "pending": 0,
        "elapsed_seconds": 10,
        "errors_429": 0,
        "semantic_status": "passed",
    }
    assert steps.send(None)[1]["operation"] == "finalize_arm"
    assert steps.send({"report": summary})[0] == "timer"
    assert steps.send(None)[1] == "begin"
    assert steps.send({})[1]["operation"] == "begin"
    assert steps.send({})[0] == "all"
    assert steps.send(None)[1]["operation"] == "finalize_arm"
    assert steps.send({"report": summary})[1]["operation"] == "finalize"
    result = {
        "router_reconciliation_required": ambiguous,
        "status": "needs_attention" if ambiguous else "succeeded",
    }
    if ambiguous:
        with pytest.raises(StopIteration):
            steps.send(result)
    else:
        assert steps.send(result)[1] == "release"
        with pytest.raises(StopIteration):
            steps.send({})
