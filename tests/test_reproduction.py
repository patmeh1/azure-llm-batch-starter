import hashlib
import json
import threading
from pathlib import Path

import pytest

from llm_batch_starter.complex_workload import Expected
from llm_batch_starter.config import Settings
from llm_batch_starter.online import partition_client_groups
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.reproduction import (
    V2_PROMPT_HASH,
    ReproductionSpec,
    VerifiedProvider,
    execute_reproduction,
    export_reproduction,
    plan_reproduction,
    reproduction_settings,
)
from llm_batch_starter.state import State
from llm_batch_starter.synthetic import generate_dataset


def base(tmp_path):
    return Settings(
        endpoint="https://private-account.openai.azure.com",
        model_name="gpt-5-mini",
        model_version="2025-08-07",
        online_deployment="gpt5-mini-online",
        input_price_per_million=0.275,
        cached_input_price_per_million=0.0275,
        output_price_per_million=2.2,
        coordination_dir=str(tmp_path / "locks"),
    )


def simple_completion(record):
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "content": json.dumps(
                        {
                            "category": record.expected_label,
                            "priority": "normal",
                            "summary": "Synthetic result",
                        }
                    )
                },
            }
        ],
        "usage": {"prompt_tokens": 150, "completion_tokens": 40},
    }, {"x-ratelimit-limit-tokens": "150000", "x-ratelimit-limit-requests": "150"}


def test_full_group_partition_is_exactly_five_contiguous_340_record_groups():
    rows = generate_dataset(1700)
    groups = partition_client_groups(rows, 5)
    assert [len(group) for group in groups] == [340] * 5
    assert [record for group in groups for record in group] == rows
    assert groups[1][0].id == "record-000340"


def test_client_batch_executes_each_group_sequentially_in_parallel(tmp_path):
    barrier = threading.Barrier(5)
    seen = []
    lock = threading.Lock()

    class Provider:
        def complete(self, record):
            number = int(record.id.rsplit("-", 1)[1])
            if number % 2 == 0:
                barrier.wait(timeout=10)
            with lock:
                seen.append(number)
            return simple_completion(record)

    report = run_pipeline(
        generate_dataset(10),
        Settings(concurrency=5, rpm=10000, tpm=1_000_000, coordination_dir=str(tmp_path / "locks")),
        "client-batch",
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
        provider=Provider(),
    )
    assert report["succeeded"] == 10
    assert len(report["jobs"]) == 5
    assert all(job["kind"] == "client_group" for job in report["jobs"])
    assert all(job["request_counts"]["total"] == 2 for job in report["jobs"])
    for first in range(0, 10, 2):
        assert seen.index(first) < seen.index(first + 1)


def test_v2_default_settings_fingerprint_is_not_changed_by_v3_field():
    settings = Settings()
    legacy = settings.model_dump_json(
        exclude={"scheduler_endpoint", "task_hub", "observation_profile", "model_resource_id"}
    )
    assert settings.fingerprint() == hashlib.sha256(legacy.encode()).hexdigest()


@pytest.mark.parametrize(
    "changes",
    [
        {"max_http_attempts": 5101},
        {"max_attempts": 4},
        {"run_timeout_seconds": 36001},
        {"max_run_cost_usd": 76},
        {"concurrency": 6},
        {"model_name": "different-model"},
    ],
)
def test_large_unpaced_reproduction_is_still_strictly_bounded(tmp_path, changes):
    settings, _ = reproduction_settings(base(tmp_path), ReproductionSpec(count=10))
    with pytest.raises(ValueError, match="Full reproduction"):
        Settings.model_validate({**settings.model_dump(), **changes})


def test_resume_does_not_repeat_an_inflight_unknown_response(tmp_path):
    class Interrupted:
        def complete(self, record):
            raise RuntimeError("Simulated interruption after request-start persistence")

    settings = Settings.model_validate(
        {
            **base(tmp_path).model_dump(),
            "observe_service_throttling": True,
            "observation_profile": "full-reproduction",
            "max_http_attempts": 3,
            "max_run_cost_usd": 1,
        }
    )
    records = generate_dataset(3)
    args = (records, settings, "sequential", tmp_path / "state.sqlite3", tmp_path / "runs")
    with pytest.raises(RuntimeError, match="Simulated interruption"):
        run_pipeline(*args, run_id="interrupted-run", provider=Interrupted())
    called = []

    class Resume:
        def complete(self, record):
            called.append(record.id)
            return simple_completion(record)

    report = run_pipeline(*args, run_id="interrupted-run", provider=Resume())
    assert called == [records[1].id, records[2].id]
    assert report["succeeded"] == 2 and report["failed"] == 1
    store = State(tmp_path / "state.sqlite3", readonly=True)
    try:
        first = store.items("records", "interrupted-run")[0]
        assert first["error"] == "ambiguous_online_result"
        assert first["retryable"] is False
        assert (
            len(
                [
                    e
                    for e in store.items("events", "interrupted-run")
                    if e["stage"] == "http_request_started"
                ]
            )
            == 3
        )
    finally:
        store.close()


def test_attempt_limit_is_not_reset_by_resume(tmp_path):
    class Interrupted:
        def complete(self, record):
            raise RuntimeError("Interruption")

    settings = Settings.model_validate(
        {
            **base(tmp_path).model_dump(),
            "observe_service_throttling": True,
            "observation_profile": "full-reproduction",
            "max_http_attempts": 1,
            "max_run_cost_usd": 1,
        }
    )
    args = (
        generate_dataset(2),
        settings,
        "sequential",
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
    )
    with pytest.raises(RuntimeError):
        run_pipeline(*args, run_id="limited", provider=Interrupted())
    report = run_pipeline(*args, run_id="limited", provider=Interrupted())
    assert report["failed"] == 2
    assert report["succeeded"] == 0


def test_original_deadline_is_not_reset_by_resume(tmp_path):
    class Interrupted:
        def complete(self, record):
            raise RuntimeError("Interruption")

    settings = Settings.model_validate(
        {
            **base(tmp_path).model_dump(),
            "observe_service_throttling": True,
            "observation_profile": "full-reproduction",
            "max_http_attempts": 6,
            "max_run_cost_usd": 1,
            "run_timeout_seconds": 60,
        }
    )
    args = (
        generate_dataset(2),
        settings,
        "sequential",
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
    )
    with pytest.raises(RuntimeError):
        run_pipeline(*args, run_id="deadline", provider=Interrupted())
    store = State(tmp_path / "state.sqlite3")
    try:
        run = store.get_run("deadline")
        run["started_at"] = "2000-01-01T00:00:00+00:00"
        store.save_run(run)
    finally:
        store.close()
    report = run_pipeline(*args, run_id="deadline", provider=Interrupted())
    assert report["failed"] == 2 and report["succeeded"] == 0


def test_service_limit_mismatch_stops_instead_of_faking_equivalence():
    class Provider:
        def complete(self, record):
            return {}, {"x-ratelimit-limit-tokens": "600000", "x-ratelimit-limit-requests": "600"}

    with pytest.raises(ValueError, match="TPM/RPM differs"):
        VerifiedProvider(Provider(), ReproductionSpec()).complete(None)


def test_non_us_configuration_is_rejected(tmp_path):
    settings = Settings.model_validate({**base(tmp_path).model_dump(), "region": "swedencentral"})
    with pytest.raises(ValueError, match="US resource region"):
        reproduction_settings(settings, ReproductionSpec())


def test_two_mode_runner_preserves_v2_prompts_and_exports_only_safe_data(tmp_path, monkeypatch):
    spec = ReproductionSpec(count=10)
    plan = plan_reproduction(spec, base(tmp_path))
    assert plan["workload_profile"]["prompt_bundle_sha256"] == V2_PROMPT_HASH
    assert plan["workload_profile"]["batch_api_used"] is False
    assert plan["conservative_all_attempts_estimate_usd"] < spec.max_estimated_inference_usd
    monkeypatch.setattr("llm_batch_starter.reproduction.time.sleep", lambda _: None)
    calls = []

    class Provider:
        def __init__(self, settings):
            pass

        def complete(self, record):
            calls.append(record.id)
            expected = Expected.model_validate(record.expected_result)
            confidence = expected.minimum_confidence
            result = {
                **expected.fields,
                "confidence": confidence,
                "confidence_basis": expected.archived_basis
                if confidence == expected.archived_score
                else "Synthetic direct evidence.",
            }
            return {
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
                "usage": {"prompt_tokens": 32000, "completion_tokens": 350},
            }, {"x-ratelimit-limit-tokens": "150000", "x-ratelimit-limit-requests": "150"}

        def close(self):
            pass

    result = execute_reproduction(
        spec, base(tmp_path), tmp_path / "experiments", provider_factory=Provider
    )
    assert result["status"] == "completed"
    assert len(calls) == 20
    path = Path(result["report_path"])
    resumed = execute_reproduction(
        spec, base(tmp_path), tmp_path / "experiments", resume=path, provider_factory=Provider
    )
    assert resumed["ended_at"] == result["ended_at"]
    assert len(calls) == 20
    exported = export_reproduction(path, tmp_path / "public")
    text = json.dumps(exported)
    assert "private-account" not in text and "state_path" not in text
    assert exported["results_final"]
    assert len((tmp_path / "public" / "client-batch-results.jsonl").read_text().splitlines()) == 10
