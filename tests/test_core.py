from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_batch_starter.config import DEFAULT_SCHEMA, Settings
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.prepare import estimate_input_tokens, prepare_requests
from llm_batch_starter.reconcile import reconcile_results, validate_completion
from llm_batch_starter.retry import AdmissionLimiter, retry_delay
from llm_batch_starter.synthetic import generate_dataset, read_records, write_records


def response(label: str = "shipping") -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "category": label,
                            "priority": "normal",
                            "summary": "Synthetic support request",
                        }
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30, "total_tokens": 230},
    }


class FakeProvider:
    def __init__(self) -> None:
        self.payload = b""
        self.submissions = 0

    def complete(self, record):
        return response(record.expected_label), {"x-ratelimit-remaining-tokens": "10000"}

    def upload(self, payload, filename):
        self.payload = payload
        return "file-test"

    def submit(self, file_id, submission_key):
        self.submissions += 1
        return {"id": "batch-test", "status": "validating"}

    def poll(self, job_id):
        return {"id": job_id, "status": "completed", "output_file_id": "output-test"}

    def download(self, file_id):
        return "\n".join(
            json.dumps(
                {
                    "custom_id": row["custom_id"],
                    "response": {"status_code": 200, "body": response()},
                }
            )
            for row in reversed([json.loads(line) for line in self.payload.splitlines()])
        )

    def find_submission(self, submission_key):
        return None


def test_synthetic_deterministic(tmp_path: Path) -> None:
    a = generate_dataset(10, seed=7)
    assert a == generate_dataset(10, seed=7)
    write_records(tmp_path / "input.jsonl", a)
    assert read_records(tmp_path / "input.jsonl") == a


def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    records = generate_dataset(1) * 2
    write_records(tmp_path / "duplicate.jsonl", records)
    with pytest.raises(ValueError, match="Duplicate"):
        read_records(tmp_path / "duplicate.jsonl")


def test_partition_counts_and_tokens() -> None:
    settings = Settings(records_per_batch=3)
    records = generate_dataset(8)
    parts = prepare_requests(records, settings)
    assert [len(p.records) for p in parts] == [3, 3, 2]
    assert sum(p.estimated_tokens for p in parts) == sum(
        estimate_input_tokens(r, settings) for r in records
    )


def test_context_limit_rejected() -> None:
    with pytest.raises(ValueError, match="context"):
        prepare_requests(
            generate_dataset(1, reference_tokens=2000),
            Settings(context_limit=1500, max_output_tokens=64),
        )


def test_retry_headers() -> None:
    assert retry_delay({"retry-after-ms": "1500"}, 1) == 1.5
    assert retry_delay({"Retry-After": "30"}, 1) == 30
    now = datetime.now(UTC).replace(microsecond=0)
    assert (
        retry_delay(
            {"Retry-After": (now + timedelta(seconds=10)).strftime("%a, %d %b %Y %H:%M:%S GMT")},
            1,
            now=now,
        )
        == 10
    )


def test_throttling_observation_bounded() -> None:
    with pytest.raises(ValueError, match="unpaced"):
        Settings(observe_service_throttling=True)
    Settings(observe_service_throttling=True, max_http_attempts=30, run_timeout_seconds=180)


def test_large_request_cannot_deadlock_limiter() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        AdmissionLimiter(60, 1000).acquire(1001, 99999999999, 2)


def test_refusal_and_truncation_not_success() -> None:
    data = response()
    data["choices"][0]["finish_reason"] = "length"
    assert validate_completion(data, DEFAULT_SCHEMA)["status"] == "failed"
    data["choices"][0]["message"]["refusal"] = "refused"
    assert validate_completion(data, DEFAULT_SCHEMA)["error"] == "refusal"


def test_reconciliation_missing_duplicate_unknown() -> None:
    records = generate_dataset(3)
    entry = {"custom_id": records[0].id, "response": {"status_code": 200, "body": response()}}
    output = "\n".join([json.dumps(entry), json.dumps(entry), '{"custom_id":"unknown"}'])
    results, issues = reconcile_results(records, output, "", DEFAULT_SCHEMA)
    assert [r["error"] for r in results] == ["duplicate_result", "missing_result", "missing_result"]
    assert issues == ["unexpected_result_id"]


@pytest.mark.parametrize("mode", ["sequential", "parallel", "batch"])
def test_complete_pipeline_offline(tmp_path: Path, mode: str) -> None:
    settings = Settings(
        rpm=10000,
        tpm=1_000_000,
        coordination_dir=str(tmp_path / "locks"),
    )
    result = run_pipeline(
        generate_dataset(5),
        settings,
        mode,
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
        provider=FakeProvider(),
    )
    assert result["status"] == "succeeded"
    assert result["succeeded"] == 5
    assert result["failed"] == result["pending"] == 0
    assert result["elapsed_seconds"] >= 0


def test_resume_does_not_repeat_completed_batch(tmp_path: Path) -> None:
    settings, provider = Settings(), FakeProvider()
    records = generate_dataset(2)
    args = (records, settings, "batch", tmp_path / "state.sqlite3", tmp_path / "runs")
    first = run_pipeline(*args, provider=provider)
    run_pipeline(*args, provider=provider, run_id=first["id"])
    assert provider.submissions == 1
