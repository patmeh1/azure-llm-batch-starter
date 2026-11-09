import pytest

from llm_batch_starter.cloud_activities import CloudActivities
from llm_batch_starter.complex_workload import configure
from llm_batch_starter.config import Settings
from llm_batch_starter.synthetic import generate_dataset
from services.orchestrator import function_app


class Store:
    def __init__(self, job):
        self.job = dict(job)

    def get_item(self, table, run_id, key):
        return dict(self.job) if table == "jobs" else {"requested": True}

    def save(self, table, run_id, key, value):
        assert table == "jobs"
        self.job = dict(value)


@pytest.mark.parametrize("status", ["prepared", "cancelled_before_submission", "ambiguous"])
def test_admission_cancellation_preserves_ambiguous_submissions(status):
    activities = CloudActivities.__new__(CloudActivities)
    activities.store = Store({"id": "part-0000", "status": status})
    assert activities.admission_cancelled("test-run", "part-0000") == (status != "ambiguous")
    assert activities.store.job["status"] == (
        "ambiguous" if status == "ambiguous" else "cancelled_before_submission"
    )


@pytest.mark.parametrize("remote_id", [None, "remote-job"])
def test_replayed_blocked_step_preserves_reservation_decision(remote_id):
    activities = CloudActivities.__new__(CloudActivities)
    activities.store = Store({"id": "part-0000", "status": "blocked", "remote_id": remote_id})
    result = activities.step("test-run", "part-0000")
    assert result["terminal"]
    assert result["release"] == (remote_id is None)


def test_cloud_retry_selects_only_confirmed_transient_failures(monkeypatch):
    records = generate_dataset(4)
    outcomes = [
        {"id": records[0].id, "status": "succeeded"},
        {"id": records[1].id, "status": "failed", "status_code": 429},
        {"id": records[2].id, "status": "failed", "error": "refusal"},
        {"id": records[3].id, "status": "failed", "error": "missing_result"},
    ]

    class RetryStore:
        def get_run(self, run_id):
            return {"status": "needs_attention"}

        def items(self, table, run_id):
            return outcomes

    activities = CloudActivities.__new__(CloudActivities)
    activities.store = RetryStore()
    activities.manifest = lambda run_id: (records, Settings())

    def create(selected, settings, *, run_id, enqueue, parent_run_id, workload_profile=None):
        assert [record.id for record in selected] == [records[1].id]
        assert not enqueue and parent_run_id == "parent-run"
        return {"id": run_id}

    monkeypatch.setattr("llm_batch_starter.cloud_activities.create_cloud_run", create)
    assert activities.prepare_retry("parent-run", "retry-run")["record_count"] == 1


@pytest.mark.parametrize("mismatch", ["prompt", "model"])
def test_complex_bootstrap_rejects_mismatches_before_storage_or_inference(
    monkeypatch, function_handlers, mismatch
):
    settings, profile = configure(Settings())
    options = {
        "run_id": "test-run",
        "count": 13,
        "seed": 42,
        "workload_profile": dict(profile),
        "expected_model": [settings.model_name, settings.model_version],
    }
    if mismatch == "prompt":
        options["workload_profile"]["prompt_bundle_sha256"] = "different"
    else:
        options["expected_model"][1] = "different"

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "Mismatched metadata must not reach paid work or create cloud artifacts"
        )

    monkeypatch.setattr(function_app, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr(function_app, "create_cloud_run", forbidden)
    handler = function_handlers["bootstrap_complex"]
    with pytest.raises(ValueError, match="differ"):
        handler(options)


def test_complex_bootstrap_saves_the_same_withheld_gold_and_prompt_hash(
    monkeypatch, function_handlers
):
    settings, profile = configure(Settings())
    saved = []

    def create(records, configured, **kwargs):
        assert len(records) == 13
        assert all(record.expected_result for record in records)
        assert configured.prompt == settings.prompt
        assert kwargs["workload_profile"]["prompt_bundle_sha256"] == profile["prompt_bundle_sha256"]
        assert kwargs["enqueue"] is False
        saved.append(kwargs)
        return {"id": kwargs["run_id"]}

    monkeypatch.setattr(function_app, "load_settings", lambda **kwargs: settings)
    monkeypatch.setattr(function_app, "create_cloud_run", create)
    handler = function_handlers["bootstrap_complex"]
    result = handler(
        {
            "run_id": "test-run",
            "count": 13,
            "seed": 42,
            "workload_profile": profile,
            "expected_model": [settings.model_name, settings.model_version],
        }
    )
    assert result["record_count"] == 13
    assert len(saved) == 1
