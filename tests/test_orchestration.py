import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_batch_starter.config import WORKFLOW_VERSION
from llm_batch_starter.quota import update_reservations
from services.orchestrator import function_app
from services.orchestrator.function_app import partition_steps, recovery_steps


def reservation(key="a", tokens=600):
    return {"key": key, "tokens": tokens, "max_tokens": 1000, "max_active": 2}


def test_shared_budget_is_idempotent_and_bounded():
    state, allowed = update_reservations({}, "acquire", reservation())
    assert allowed and state == {"a": 600}
    repeated, allowed = update_reservations(state, "acquire", reservation())
    assert allowed and repeated == state
    denied, allowed = update_reservations(state, "acquire", reservation("b"))
    assert not allowed and denied == state
    released, _ = update_reservations(state, "release", {"key": "a"})
    assert released == {}


def test_reserved_request_cannot_change_shape():
    with pytest.raises(ValueError, match="fingerprint"):
        update_reservations({"a": 500}, "acquire", reservation())


def test_oversized_reservation_fails_instead_of_waiting_forever():
    with pytest.raises(ValueError, match="Invalid"):
        update_reservations({}, "acquire", reservation(tokens=2000))


class Context:
    def __init__(self):
        self.current_utc_datetime = datetime(2026, 1, 1, tzinfo=UTC)
        self.version = WORKFLOW_VERSION

    def get_input(self):
        return {
            "run_id": "test-run",
            "part": {"id": "part-0000", "tokens": 500},
            "limits": {
                "cloud_timeout_seconds": 300,
                "max_enqueued_tokens": 1000,
                "max_active_batches": 1,
                "poll_seconds": 1,
                "max_poll_seconds": 5,
            },
        }

    def call_activity_with_retry(self, name, retry, value):
        return ("activity", name, value)

    def call_entity(self, entity, name, value):
        return ("entity", name, value)

    def create_timer(self, deadline):
        return ("timer", deadline)

    def set_custom_status(self, value):
        self.status = value

    def call_sub_orchestrator(self, name, value, *, instance_id, version):
        return ("child", name, value, instance_id, version)

    def task_all(self, tasks):
        return ("all", tasks)

    def call_activity(self, name, value):
        return ("activity", name, value)


def test_queued_cancellation_does_not_wait_for_capacity_or_submit():
    context = Context()
    steps = partition_steps(context)
    assert next(steps)[1] == "check_partition_cancellation"
    assert steps.send(False)[1] == "acquire"
    assert steps.send(False)[0] == "timer"
    context.current_utc_datetime += timedelta(seconds=60)
    assert steps.send(None)[1] == "check_partition_cancellation"
    with pytest.raises(StopIteration) as stopped:
        steps.send(True)
    assert stopped.value.value["status"] == "cancelled_before_submission"
    assert not stopped.value.value["release"]


@pytest.mark.parametrize("release", [True, False])
def test_partition_releases_only_confirmed_terminal_reservations(release):
    steps = partition_steps(Context())
    assert next(steps)[1] == "check_partition_cancellation"
    assert steps.send(False)[1] == "acquire"
    assert steps.send(True)[1] == "step_partition"
    result = {
        "terminal": True,
        "release": release,
        "status": "completed" if release else "ambiguous",
    }
    if release:
        assert steps.send(result)[1] == "release"
        with pytest.raises(StopIteration):
            steps.send(None)
    else:
        with pytest.raises(StopIteration) as stopped:
            steps.send(result)
        assert stopped.value.value["status"] == "ambiguous"


@pytest.mark.parametrize("version", [None, ""])
def test_legacy_replay_keeps_the_original_first_activity_sequence(version):
    context = Context()
    context.version = version
    steps = partition_steps(context)
    assert next(steps)[1] == "acquire"
    assert steps.send(True)[1] == "step_partition"


def test_worker_and_clients_use_the_same_workflow_version():
    host = json.loads(
        (Path(__file__).parents[1] / "services" / "orchestrator" / "host.json").read_text()
    )
    assert host["extensions"]["durableTask"]["defaultVersion"] == WORKFLOW_VERSION
    assert host["extensions"]["durableTask"]["versionMatchStrategy"] == "CurrentOrOlder"
    assert host["extensions"]["durableTask"]["versionFailureStrategy"] == "Reject"


def test_recovery_uses_fresh_history_but_the_same_saved_partitions():
    context = Context()
    context.instance_id = "recovery-run"
    context.get_input = lambda: {"artifact_run_id": "original-run"}
    steps = recovery_steps(context)
    assert next(steps) == ("activity", "prepare_run", "original-run")
    waiting = steps.send({"parts": [{"id": "part-0000", "tokens": 500}]})
    child = waiting[1][0]
    assert child[2]["run_id"] == "original-run"
    assert child[3] == "recovery-run-part-0000"
    assert child[4] == WORKFLOW_VERSION
    assert steps.send(None) == ("activity", "finalize_run", "original-run")
    with pytest.raises(StopIteration):
        steps.send({"status": "succeeded"})


def test_cancellation_can_arrive_before_bootstrap_persists_the_run(monkeypatch, function_handlers):
    saved = []

    class Store:
        def save(self, *args):
            saved.append(args)

        def close(self):
            pass

    monkeypatch.setattr(function_app, "AzureState", lambda settings: Store())
    handler = function_handlers["request_run_cancellation"]
    assert handler({"run_id": "new-run"})["status"] == "cancellation_requested"
    assert saved == [("control", "new-run", "cancel", {"requested": True})]
