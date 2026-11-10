import copy
import io
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import grpc
import httpx
import openai
import pytest
from test_multi_deployment import Context, Store, gold_completion
from typer.testing import CliRunner

from llm_batch_starter.adaptive import (
    ADAPTIVE_STAGES,
    ADAPTIVE_TARGETS,
    AdaptiveSpec,
    adaptive_plan,
    adaptive_settings,
    export_adaptive,
    update_admission,
)
from llm_batch_starter.adaptive_cloud import AdaptiveActivities
from llm_batch_starter.cli import app
from llm_batch_starter.config import WORKFLOW_VERSION, Settings
from llm_batch_starter.reporting import summarize_documents
from llm_batch_starter.scheduler import SchedulerAccess
from llm_batch_starter.state import State
from llm_batch_starter.topology import verify_topology
from services.orchestrator.function_app import adaptive_steps, adaptive_worker_steps

NOW = datetime(2026, 11, 10, 20, 0, tzinfo=UTC)
RESOURCE = "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/test/providers/Microsoft.CognitiveServices/accounts/example"


def begin(stage="adaptive", count=10, **spec_values):
    spec = AdaptiveSpec(count=count, headroom=1, **spec_values)
    return update_admission(
        {},
        "begin",
        {
            "owner": "root",
            "stage": stage,
            "now": NOW.isoformat(),
            "specification": spec.model_dump(),
            "request_tokens": [40000] * (count + 5),
            "request_costs": [0.01] * (count + 5),
        },
    )[0]


def claim(state, key, now=NOW, **extras):
    return update_admission(
        state,
        "claim",
        {
            "owner": "root",
            "key": key,
            "now": now.isoformat(),
            **extras,
        },
    )


def ack(state, key, now=NOW, **response):
    return update_admission(
        state,
        "ack",
        {
            "owner": "root",
            "key": key,
            "now": now.isoformat(),
            "response": {"terminal": True, **response},
        },
    )


def test_v5_preview_includes_five_targets_and_bounded_warmup_cost():
    plan = adaptive_plan(AdaptiveSpec(), Settings())
    assert plan["status"] == "planned_not_executed"
    assert plan["expected_benchmark_records"] == 1040
    assert plan["expected_warmup_records"] == 10
    assert plan["max_http_attempts"] == 4200
    assert 55.89 < plan["conservative_all_attempts_estimate_usd"] < 75
    for arm in plan["arms"]:
        assert len(arm["targets"]) == 5
        assert sum(t["allocated_tpm"] for t in arm["targets"]) == 300000
    assert plan["workload_profile"]["experiment"] == "v5-adaptive"


def test_admission_reserves_before_dispatch_and_weights_available_capacity():
    state = begin()
    initial = []
    for index in range(5):
        state, grant = claim(state, f"w{index}")
        initial.append(grant["target"])
    assert set(initial) == set(ADAPTIVE_TARGETS)
    state, waiting = claim(state, "busy")
    assert "wait_until" in waiting
    assert state["attempts"] == 5
    for index in range(5):
        state, _ = ack(state, f"w{index}")
    state, grant = claim(state, "next", NOW + timedelta(seconds=1))
    assert grant["target"] == "target-1"
    state, _ = ack(state, "next", NOW + timedelta(seconds=1))
    _, waiting = claim(state, "not-enough", NOW + timedelta(seconds=1))
    assert "wait_until" in waiting


def test_reactive_arm_is_not_silently_token_paced():
    state = begin("reactive")
    for index in range(5):
        state, _ = claim(state, str(index))
        state, _ = ack(state, str(index))
    _, grant = claim(state, "again")
    assert "target" in grant


def test_shared_queue_assigns_next_record_to_any_worker_and_retries_once():
    state = begin("reactive")
    state, first = claim(state, "worker-a")
    state, second = claim(state, "worker-b")
    assert first["index"] == 0 and second["index"] == 1
    state, _ = ack(
        state,
        "worker-b",
        terminal=False,
        retryable=True,
        retry_after=60,
        completed_at=NOW.isoformat(),
    )
    before = copy.deepcopy(state)
    state, _ = ack(state, "worker-b", terminal=False)
    assert state == before
    state, retry = claim(state, "worker-c")
    assert retry["index"] == 1 and retry["attempt"] == 2
    assert retry["target"] != second["target"]
    before = copy.deepcopy(state)
    state, repeated = claim(state, "worker-c")
    assert repeated == retry and state == before


def test_original_deadline_and_cost_budget_survive_replay():
    state = begin()
    original_deadline = state["deadline"]
    state, _ = update_admission(
        state,
        "begin",
        {
            "owner": "root",
            "stage": "adaptive",
            "now": (NOW + timedelta(hours=1)).isoformat(),
            "specification": AdaptiveSpec(count=10).model_dump(),
        },
    )
    assert state["deadline"] == original_deadline
    _, grant = claim(state, "expired", NOW + timedelta(hours=3))
    assert grant["terminal_error"] == "admission_deadline"
    state["cost_limit"] = 0
    _, grant = claim(state, "cost")
    assert grant["terminal_error"] == "attempt_or_cost_budget_exhausted"


def test_cancelled_records_do_not_reserve_or_dispatch_inference():
    state = begin()
    state, grant = claim(state, "cancel", cancelled=True)
    assert grant["terminal_error"] == "cancelled_before_attempt"
    assert state["attempts"] == 0 and "target" not in grant


def test_warmups_reach_each_target_and_share_the_total_budget():
    state = begin("adaptive-warmup")
    used = []
    for index in range(5):
        state, grant = claim(state, str(index))
        used.append(grant["target"])
        state, _ = ack(state, str(index))
    assert len(set(used)) == 5
    assert state["completed"] == 5
    assert claim(state, "done")[1] == {"done": True}
    measured = begin("adaptive")
    assert state["cost_limit"] + measured["cost_limit"] == 37.5


def test_full_520_record_retry_ledger_stays_below_scheduler_payload_limit(record_property):
    state = begin("reactive", count=520)
    now = NOW
    for sequence in range(520 * 4):
        key = f"worker-0-{sequence}"
        state, grant = claim(state, key, now)
        assert "target" in grant
        state, _ = ack(
            state,
            key,
            now,
            terminal=grant["attempt"] == 4,
            status_code=429,
            retryable=True,
            retry_after=0,
            completed_at=now.isoformat(),
        )
        now += timedelta(seconds=1)
    assert state["completed"] == 520 and state["attempts"] == 2080
    size = len(json.dumps(state).encode())
    record_property("max_entity_state_bytes", size)
    assert size < 900_000  # Leave headroom below the scheduler's 1 MiB payload limit.


def test_feedback_is_bounded_and_invalid_headers_are_visible():
    state = begin()
    state, grant = claim(state, "one")
    state, _ = ack(
        state,
        "one",
        budget_headers={
            "x-ratelimit-limit-tokens": "999999",
            "x-ratelimit-remaining-tokens": "999999",
            "x-ratelimit-remaining-requests": "nan",
        },
    )
    target = state["targets"][grant["target"]]
    assert target["tokens"] <= ADAPTIVE_TARGETS[grant["target"]]["tpm"]
    assert state["invalid_budget_headers"] == 1
    assert state["feedback_updates"] == 1


def test_temporary_lower_limit_does_not_permanently_starve_a_target():
    state = begin()
    state, grant = claim(state, "one")
    state, _ = ack(
        state,
        "one",
        budget_headers={
            "x-ratelimit-limit-tokens": "10000",
            "x-ratelimit-remaining-tokens": "0",
        },
    )
    for alias, node in state["targets"].items():
        if alias != grant["target"]:
            node["cooldown"] = NOW.timestamp() + 1000
    _, wait = claim(state, "later", NOW + timedelta(seconds=10))
    assert "wait_until" in wait
    state, later = claim(state, "later", NOW + timedelta(seconds=120))
    assert later["target"] == grant["target"]
    assert state["targets"][later["target"]]["token_limit"] == 100000


def test_averages_use_measured_durations_and_explicit_denominators():
    run = {
        "id": "test",
        "mode": "parallel",
        "started_at": NOW.isoformat(),
        "ended_at": (NOW + timedelta(seconds=100)).isoformat(),
        "record_count": 3,
    }
    records = [
        {
            "id": "a",
            "status": "succeeded",
            "elapsed_seconds": 10,
            "backlog_wait_seconds": 0,
            "queue_to_completion_seconds": 10,
        },
        {
            "id": "b",
            "status": "succeeded",
            "elapsed_seconds": 30,
            "backlog_wait_seconds": 20,
            "queue_to_completion_seconds": 50,
        },
        {
            "id": "c",
            "status": "failed",
            "error": "RateLimitError",
            "elapsed_seconds": 80,
            "backlog_wait_seconds": 20,
            "queue_to_completion_seconds": 100,
        },
    ]
    events = [
        {
            "stage": "http_attempt",
            "record_id": str(i),
            "at": NOW.isoformat(),
            "status_code": code,
            "duration_seconds": seconds,
        }
        for i, (code, seconds) in enumerate([(200, 2), (429, 1), (200, 6)])
    ]
    report = summarize_documents(run, records, events, [])
    assert report["http_200_latency_mean_seconds"] == 4
    assert report["http_200_latency_measured_count"] == 2
    assert report["http_attempt_latency_mean_seconds"] == 3
    assert report["successful_record_elapsed_mean_seconds"] == 20
    assert report["all_record_elapsed_mean_seconds"] == 40
    assert report["record_elapsed_measured_count"] == 3
    assert report["queue_to_completion_mean_seconds"] == pytest.approx(160 / 3)
    assert report["all_record_elapsed_mean_seconds"] != 100 / 3


def setup_activities(provider_factory):
    settings, profile = adaptive_settings(Settings(model_resource_id=RESOURCE))
    store = Store()
    activities = AdaptiveActivities(settings, store, provider_factory, lambda *_: [])
    spec = AdaptiveSpec(count=5)
    prepared = activities.bootstrap(
        {
            "run_id": "root",
            "count": 10,
            "specification": spec.model_dump(),
            "expected_model": ["gpt-5-mini", "2025-08-07"],
            "workload_profile": profile,
            "model_resource_id": RESOURCE,
        }
    )
    return activities, store, prepared


def test_cloud_stages_reuse_clients_and_export_only_safe_data(tmp_path):
    created, closed, calls = [], [], []

    class Provider:
        def __init__(self, settings):
            self.name = settings.online_deployment
            created.append(self.name)

        def complete(self, record):
            calls.append(record.id)
            return gold_completion(record), {"x-ratelimit-limit-tokens": "50000"}

        def close(self):
            closed.append(self.name)

    activities, store, prepared = setup_activities(Provider)
    aliases = list(ADAPTIVE_TARGETS)
    for arm in ADAPTIVE_STAGES:
        activities.begin_arm("root", arm)
        for index in range(5):
            actual = index + 5 if arm.endswith("-warmup") else index
            result = activities.attempt(
                {
                    "run_id": "root",
                    "arm": arm,
                    "index": actual,
                    "attempt": 1,
                    "target": aliases[index],
                    "router_wait_seconds": 0,
                }
            )
            assert result["terminal"]
        activities.save_admission("root", arm, {"completed": 5, "expected": 5})
        activities.finalize_arm("root", arm)
    final = activities.finalize("root")
    assert final["succeeded"] == 10 and final["pending"] == 0
    assert len(calls) == 20 and len(created) == 5 and not closed
    assert len(final["report"]["warmup_stages"]) == 2
    assert final["report"]["queue_to_completion_measured_count"] == 10
    assert prepared["request_tokens"] and len(prepared["request_tokens"]) == 10
    source = tmp_path / "report.json"
    source.write_text(json.dumps(final["report"]))
    exported = export_adaptive(source, tmp_path / "public")
    assert exported["results_final"]
    assert (
        exported["reported_model_usage_estimate_usd"] > final["report"]["priced_usage_estimate_usd"]
    )
    assert RESOURCE not in json.dumps(exported)
    assert "model_resource_id" not in json.dumps(exported)
    activities.close()
    assert len(closed) == 5


def test_uncertain_paid_intent_is_not_replayed_by_the_cached_activity_service():
    calls = []

    class Interrupted:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            raise RuntimeError("Interrupted after paid intent")

        def close(self):
            pass

    activities, store, _ = setup_activities(Interrupted)
    activities.begin_arm("root", "adaptive")
    item = {"run_id": "root", "arm": "adaptive", "index": 0, "attempt": 1, "target": "target-1"}
    with pytest.raises(RuntimeError, match="Interrupted"):
        activities.attempt(item)
    assert activities.attempt(item)["terminal"]
    assert len(calls) == 1
    assert store.items("records", "root-adaptive")[0]["error"] == "ambiguous_online_result"


def test_confirmed_429_is_reconciled_without_an_extra_sdk_call():
    calls = []

    class Throttled:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            response = httpx.Response(
                429,
                request=httpx.Request("POST", "https://example.openai.azure.com"),
                headers={"retry-after-ms": "45000"},
            )
            raise openai.RateLimitError(
                "Rate limit exceeded", response=response, body={"error": "Rate limit exceeded"}
            )

        def close(self):
            pass

    activities, _, _ = setup_activities(Throttled)
    activities.begin_arm("root", "reactive")
    item = {"run_id": "root", "arm": "reactive", "index": 0, "attempt": 1, "target": "target-1"}
    first = activities.attempt(item)
    assert first["retry_after"] == 45 and not first["terminal"]
    assert activities.attempt(item) == first
    assert len(calls) == 1


def test_cli_is_a_preview_without_yes(tmp_path):
    path = tmp_path / "scenario.json"
    path.write_text(AdaptiveSpec(count=5).model_dump_json())
    result = CliRunner().invoke(app, ["v5-run", "--scenario", str(path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["status"] == "planned_not_executed"


def test_v5_starter_verifies_topology_and_does_not_send_raw_records(tmp_path, monkeypatch):
    verified, scheduled = [], []
    monkeypatch.setattr(
        "llm_batch_starter.scheduler.verify_topology", lambda *_: verified.append(True)
    )
    access = SchedulerAccess.__new__(SchedulerAccess)
    access.settings = Settings(model_resource_id=RESOURCE)
    access.registry = State(tmp_path / "state.sqlite3")
    access.client = SimpleNamespace(
        schedule_new_orchestration=lambda name, **kwargs: scheduled.append((name, kwargs))
    )
    try:
        run_id = access.start_adaptive(AdaptiveSpec(count=5))
        assert verified
        name, args = scheduled[0]
        assert name == "adaptive_workflow" and args["version"] == WORKFLOW_VERSION
        assert args["input"]["count"] == 10
        assert "expected_result" not in json.dumps(args)
        assert access.registry.get_run(run_id)["mode"] == "adaptive-pool"
    finally:
        access.registry.close()


@pytest.mark.parametrize("code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.PERMISSION_DENIED])
def test_observer_reconnects_only_transient_reads_without_starting_work(
    tmp_path, monkeypatch, code
):
    class TransportError(grpc.RpcError, grpc.Call):
        def code(self):
            return code

        def details(self):
            return "Simulated observer transport failure"

        def is_active(self):
            return False

        def time_remaining(self):
            return None

        def cancel(self):
            return False

        def add_callback(self, callback):
            return False

        def initial_metadata(self):
            return ()

        def trailing_metadata(self):
            return ()

    created, closed, reads, sleeps = [], [], [], []

    class Access:
        def __init__(self, settings):
            self.index = len(created)
            created.append(self)

        def get_run(self, run_id):
            reads.append(run_id)
            if self.index == 0:
                raise TransportError()
            return {"mode": "adaptive-pool", "status": "succeeded", "runtime_status": "completed"}

        def detail(self, run_id):
            return {"acceptance_status": "passed"}

        def close(self):
            closed.append(self.index)

    monkeypatch.setattr("llm_batch_starter.cli.SchedulerAccess", Access)
    monkeypatch.setattr("llm_batch_starter.cli.time.sleep", sleeps.append)
    result = CliRunner().invoke(
        app, ["v5-status", "existing-run", "--wait", "--timeout", "60", "--output", str(tmp_path)]
    )
    if code == grpc.StatusCode.UNAVAILABLE:
        assert result.exit_code == 0, result.output
        assert len(created) == 2 and sleeps == [5]
        assert "reconnecting" in result.stderr
    else:
        assert result.exit_code != 0 and len(created) == 1 and not sleeps
    assert set(reads) == {"existing-run"}
    assert closed == list(range(len(created)))


def test_missing_resource_id_blocks_live_v4_v5_without_inference():
    with pytest.raises(ValueError, match="model_resource_id"):
        verify_topology(Settings(), ADAPTIVE_TARGETS)


def test_topology_validation_rejects_allocation_drift(monkeypatch):
    calls = []
    account = {"location": "southcentralus", "properties": {"customSubDomainName": "example"}}
    deployed = {
        "value": [
            {
                "name": target["deployment"],
                "sku": {"name": "DataZoneStandard", "capacity": target["tpm"] // 1000},
                "properties": {
                    "provisioningState": "Succeeded",
                    "raiPolicyName": "test-policy",
                    "model": {"name": "gpt-5-mini", "version": "2025-08-07"},
                },
            }
            for target in ADAPTIVE_TARGETS.values()
        ],
    }

    def get(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(
            json.dumps(deployed if "/deployments?" in request.full_url else account).encode()
        )

    monkeypatch.setattr("llm_batch_starter.topology.urlopen", get)
    monkeypatch.setattr(
        "llm_batch_starter.topology.credential",
        lambda *_: SimpleNamespace(
            get_token=lambda *_: SimpleNamespace(token="not-a-secret-test-token"),
            close=lambda: None,
        ),
    )
    settings = Settings(model_resource_id=RESOURCE, endpoint="https://example.openai.azure.com")
    assert len(verify_topology(settings, ADAPTIVE_TARGETS)) == 5
    assert all(url.startswith("https://management.azure.com/") for url in calls)
    deployed["value"][0]["sku"]["capacity"] = 150
    with pytest.raises(ValueError, match="allocation"):
        verify_topology(settings, ADAPTIVE_TARGETS)


@pytest.mark.parametrize("cancelled", [False, True])
def test_known_paid_response_reconciles_after_deadline_or_cancellation(cancelled):
    calls = []

    class Provider:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            return gold_completion(record), {}

        def close(self):
            pass

    activities, store, _ = setup_activities(Provider)
    activities.begin_arm("root", "adaptive")
    original = store.save

    def fail_record(table, *args):
        if table == "records":
            raise RuntimeError("Result write interrupted")
        return original(table, *args)

    store.save = fail_record
    item = {"run_id": "root", "arm": "adaptive", "index": 0, "attempt": 1, "target": "target-1"}
    with pytest.raises(RuntimeError, match="interrupted"):
        activities.attempt(item)
    store.save = original
    run = store.get_run("root-adaptive")
    run["started_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    store.save_run(run)
    if cancelled:
        activities.cancel("root")
    result = activities.attempt(item)
    assert result["status"] == "succeeded"
    assert len(calls) == 1


def test_worker_uses_durable_timers_before_any_inference():
    context = Context()
    context.options = {"run_id": "root", "arm": "adaptive", "worker": 0}
    context.current_utc_datetime = NOW
    steps = adaptive_worker_steps(context)
    assert next(steps)[1]["operation"] == "control"
    assert steps.send({"cancelled": False})[1] == "claim"
    wait = steps.send({"wait_until": (NOW + timedelta(seconds=30)).isoformat()})
    assert wait[0] == "timer"
    context.current_utc_datetime += timedelta(seconds=30)
    assert steps.send(None)[1]["operation"] == "control"


def test_v5_root_acquires_the_same_experiment_lease_and_warms_before_benchmark():
    context = Context()
    steps = adaptive_steps(context)
    assert next(steps)[1]["operation"] == "bootstrap"
    task = steps.send(
        {
            "specification": AdaptiveSpec(count=5).model_dump(),
            "request_costs": [0.01] * 10,
            "request_tokens": [34000] * 10,
        }
    )
    assert task[1] == "acquire"
    assert steps.send({"acquired": True})[1]["arm"] == "reactive-warmup"
    assert steps.send({"started_at": NOW.isoformat()})[1] == "begin"
    fanout = steps.send({})
    assert fanout[0] == "all" and len(fanout[1]) == 5
