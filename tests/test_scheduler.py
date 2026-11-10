import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from llm_batch_starter.complex_workload import configure
from llm_batch_starter.config import WORKFLOW_VERSION, Settings
from llm_batch_starter.scheduler import SchedulerAccess
from llm_batch_starter.state import State


@pytest.mark.parametrize(
    ("name", "parent"),
    [("synthetic_workflow", None), ("complex_workflow", None), ("retry_workflow", "parent-run")],
)
def test_failed_native_output_is_not_parsed_as_success_json(tmp_path, monkeypatch, name, parent):
    state = SimpleNamespace(
        name=name,
        runtime_status=SimpleNamespace(name="FAILED"),
        serialized_input=json.dumps({"count": 10, "parent_run_id": parent}),
        serialized_output="Activity failed: this is not JSON",
        serialized_custom_status=None,
        created_at=datetime(2026, 1, 1, 10, 0),
        last_updated_at=datetime(2026, 1, 1, 10, 1),
        failure_details=None,
    )
    client = SimpleNamespace(
        get_orchestration_state=lambda *args, **kwargs: state,
        close=lambda: None,
    )
    monkeypatch.setattr(
        "durabletask.azuremanaged.client.DurableTaskSchedulerClient",
        lambda **kwargs: client,
    )
    monkeypatch.setattr(
        "llm_batch_starter.scheduler.credential",
        lambda settings: SimpleNamespace(close=lambda: None),
    )
    access = SchedulerAccess(
        Settings(scheduler_endpoint="https://example.durabletask.io", task_hub="test"),
        tmp_path / "state.sqlite3",
    )
    try:
        run = access.get_run("synthetic-test")
        assert run["status"] == "needs_attention"
        assert run["failure_type"] == "orchestration_failed"
        assert run["started_at"].endswith("+00:00")
        assert run["parent_run_id"] == parent
        assert access.registry.get_run("synthetic-test")["parent_run_id"] == parent
        report = access.detail("synthetic-test")
        assert report["succeeded"] is None
        assert report["pending"] is None
        assert report["record_count"] == 10
        assert report["errors_429"] is None
        assert report["input_tokens"] is None
        assert report["orchestration_elapsed_seconds"] == 60
    finally:
        access.close()


def test_complex_starter_sends_only_reproducible_metadata(tmp_path):
    settings, profile = configure(Settings())
    access = SchedulerAccess.__new__(SchedulerAccess)
    access.settings = settings
    access.registry = State(tmp_path / "state.sqlite3")
    scheduled = []
    access.client = SimpleNamespace(
        schedule_new_orchestration=lambda name, **kwargs: scheduled.append((name, kwargs))
    )
    try:
        run_id = access.start_complex(13, 42, run_id="saved-complex-run")
        name, payload = scheduled[0]
        assert name == "complex_workflow"
        assert payload["instance_id"] == run_id == "saved-complex-run"
        assert payload["version"] == WORKFLOW_VERSION
        assert (
            payload["input"]["workload_profile"]["prompt_bundle_sha256"]
            == profile["prompt_bundle_sha256"]
        )
        assert payload["input"]["expected_model"] == [settings.model_name, settings.model_version]
        assert "expected_result" not in json.dumps(payload)
        assert "CANARY-" not in json.dumps(payload)
        assert access.registry.get_run(run_id)["workload_profile"]["name"] == "complex-v2"
    finally:
        access.registry.close()


def test_recovery_retains_artifact_identity_and_is_idempotent(tmp_path):
    access = SchedulerAccess.__new__(SchedulerAccess)
    access.registry = State(tmp_path / "state.sqlite3")
    scheduled = []
    access.get_run = lambda run_id: {
        "mode": "batch",
        "runtime_status": "failed",
        "record_count": 1700,
        "artifact_run_id": "original-run",
        "metadata": {},
    }

    def schedule(name, **kwargs):
        assert name == "recovery_workflow"
        scheduled.append(kwargs)
        return kwargs["instance_id"]

    access.client = SimpleNamespace(
        get_orchestration_state=lambda run_id: object() if scheduled else None,
        schedule_new_orchestration=schedule,
    )
    try:
        first = access.recover("failed-root")
        assert access.recover("failed-root") == first
        assert len(scheduled) == 1
        assert scheduled[0]["input"]["artifact_run_id"] == "original-run"
        assert scheduled[0]["input"]["parent_run_id"] == "failed-root"
    finally:
        access.registry.close()


def test_recovery_throughput_keeps_prior_application_time():
    state = SimpleNamespace(
        runtime_status=SimpleNamespace(name="COMPLETED"),
        serialized_input=json.dumps(
            {"artifact_run_id": "original-run", "parent_run_id": "failed-root"}
        ),
        serialized_output=json.dumps({"report": {"elapsed_seconds": 6000, "succeeded": 1700}}),
        created_at=datetime(2026, 1, 1, 10, 0),
        last_updated_at=datetime(2026, 1, 1, 10, 1),
    )
    access = SchedulerAccess.__new__(SchedulerAccess)
    access.client = SimpleNamespace(get_orchestration_state=lambda run_id: state)
    report = access.detail("recovery-run")
    assert report["orchestration_elapsed_seconds"] == 60
    assert report["end_to_end_elapsed_seconds"] == 6000
    assert report["end_to_end_completed_records_per_minute"] == 17
    assert report["artifact_run_id"] == "original-run"


def test_synthetic_budget_is_checked_before_paid_submission(tmp_path, monkeypatch):
    client = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(
        "durabletask.azuremanaged.client.DurableTaskSchedulerClient", lambda **kwargs: client
    )
    monkeypatch.setattr(
        "llm_batch_starter.scheduler.credential",
        lambda settings: SimpleNamespace(close=lambda: None),
    )
    access = SchedulerAccess(
        Settings(
            scheduler_endpoint="https://example.durabletask.io",
            task_hub="test",
            max_run_cost_usd=0.000001,
        ),
        tmp_path / "state.sqlite3",
    )
    try:
        with pytest.raises(ValueError, match="client-side inference estimate budget"):
            access.start_synthetic(1, 42)
        assert access.registry.list_runs() == []
    finally:
        access.close()
