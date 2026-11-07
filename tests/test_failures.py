import json
from pathlib import Path

import httpx
import openai
import pytest

from llm_batch_starter.batch import submit_job
from llm_batch_starter.config import DEFAULT_SCHEMA, Settings
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.reconcile import is_retryable, reconcile_results, validate_completion
from llm_batch_starter.state import State
from llm_batch_starter.synthetic import generate_dataset


def completion():
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "category": "shipping",
                            "priority": "normal",
                            "summary": "Delivery question",
                        }
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 40},
    }


class ThrottleOnce:
    def __init__(self):
        self.calls = 0

    def complete(self, record):
        self.calls += 1
        if self.calls == 1:
            response = httpx.Response(
                429,
                headers={"retry-after-ms": "0"},
                request=httpx.Request("POST", "https://example.openai.azure.com"),
            )
            raise openai.RateLimitError("Synthetic throttle", response=response, body={})
        return completion(), {}


def test_retry_error_is_visible_even_when_record_recovers(tmp_path: Path):
    provider = ThrottleOnce()
    report = run_pipeline(
        generate_dataset(1),
        Settings(rpm=10000, coordination_dir=str(tmp_path / "locks")),
        "sequential",
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
        provider=provider,
    )
    assert provider.calls == 2
    assert report["succeeded"] == 1
    assert report["failed"] == 0
    assert report["errors_429"] == 1
    assert report["http_attempts"] == 2


def test_permanent_failures_are_not_auto_retryable():
    assert is_retryable({"status_code": 429})
    assert is_retryable({"status_code": 503})
    assert not is_retryable({"status_code": 403})
    assert not is_retryable({"error": "refusal"})
    assert not is_retryable({"error": "missing_result"})
    assert not is_retryable({"error": "invalid_output_schema"})


def test_malformed_response_fails_explicitly():
    assert validate_completion({"choices": ["invalid"]}, DEFAULT_SCHEMA)["status"] == "failed"
    records = generate_dataset(1)
    output = json.dumps({"custom_id": records[0].id, "response": "invalid"})
    results, _ = reconcile_results(records, output, "", DEFAULT_SCHEMA)
    assert results[0]["status"] == "failed"


def test_ambiguous_submission_is_not_resent(tmp_path: Path):
    store = State(tmp_path / "state.sqlite3")

    class Provider:
        def find_submission(self, key):
            return None

        def submit(self, *args):
            raise AssertionError("Must not issue another paid submission")

    try:
        with pytest.raises(RuntimeError, match="ambiguous"):
            submit_job(
                {"id": "p", "status": "submitting", "submission_key": "test-key"},
                Provider(),
                store,
                "test-run",
            )
        assert store.items("jobs", "test-run")[0]["status"] == "ambiguous"
    finally:
        store.close()


def test_resume_preserves_terminal_failure_without_reissuing(tmp_path: Path):
    class Refused:
        calls = 0

        def complete(self, record):
            self.calls += 1
            return {"choices": [{"finish_reason": "stop", "message": {"refusal": "test"}}]}, {}

    provider = Refused()
    settings = Settings(coordination_dir=str(tmp_path / "locks"))
    args = (
        generate_dataset(1),
        settings,
        "sequential",
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
    )
    first = run_pipeline(*args, provider=provider)
    second = run_pipeline(*args, provider=provider, run_id=first["id"])
    assert provider.calls == 1
    assert second["status"] == "partial_failure"
    assert first["ended_at"] == second["ended_at"]
