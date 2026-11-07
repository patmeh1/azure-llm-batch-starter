import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from llm_batch_starter.config import Settings
from llm_batch_starter.scheduler import SchedulerAccess


@pytest.mark.parametrize(
    ("name", "parent"), [("synthetic_workflow", None), ("retry_workflow", "parent-run")]
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
