from llm_batch_starter.reporting import summarize_documents
from llm_batch_starter.telemetry import elapsed_between


def test_utc_wall_time():
    assert elapsed_between("2026-01-01T10:00:00+00:00", "2026-01-01T10:01:00+00:00") == 60


def test_failed_attempt_time_does_not_disappear():
    run = {
        "id": "test",
        "mode": "parallel",
        "record_count": 2,
        "started_at": "2026-01-01T10:00:00+00:00",
        "ended_at": "2026-01-01T10:02:00+00:00",
    }
    records = [{"id": "a", "status": "succeeded"}, {"id": "b", "status": "failed"}]
    events = [
        {
            "at": "2026-01-01T10:00:00+00:00",
            "stage": "http_attempt",
            "status_code": 429,
            "duration_seconds": 1,
        },
        {"at": "2026-01-01T10:00:01+00:00", "stage": "retry_wait", "duration_seconds": 60},
    ]
    report = summarize_documents(run, records, events, [])
    assert report["elapsed_seconds"] == 120
    assert report["completed_records_per_minute"] == 0.5
    assert report["errors_429"] == 1
    assert report["retry_wait_seconds"] == 60
    assert report["input_tokens"] is None
    assert report["output_tokens"] is None
    assert report["usage_reports"] == 0
    assert not report["usage_complete"]


def test_reported_zero_usage_is_distinct_from_absent_usage():
    run = {
        "id": "test",
        "mode": "batch",
        "record_count": 1,
        "started_at": "2026-01-01T10:00:00+00:00",
        "ended_at": "2026-01-01T10:01:00+00:00",
    }
    report = summarize_documents(
        run,
        [{"id": "a", "status": "failed", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}],
        [],
        [],
    )
    assert report["input_tokens"] == report["output_tokens"] == 0
    assert report["usage_reports"] == 1
    assert report["usage_complete"]
