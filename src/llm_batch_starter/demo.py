from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import Settings
from .state import State
from .telemetry import event


def seed_demo(path: Path) -> None:
    store = State(path)
    now = datetime.now(UTC)
    scenarios = [
        ("sequential", "succeeded", 100, 0, 0, 1000),
        ("parallel", "succeeded", 100, 0, 6, 250),
        ("batch", "succeeded", 100, 0, 0, 420),
        ("batch", "needs_attention", 94, 6, 2, 610),
        ("batch", "pending", 0, 0, 0, None),
        ("batch", "cancelled", 42, 0, 0, 180),
    ]
    try:
        for index, (mode, status, good, bad, throttled, elapsed) in enumerate(scenarios):
            run_id = f"simulated-{index}"
            start = now - timedelta(seconds=elapsed or 45)
            store.save_run(
                {
                    "id": run_id,
                    "mode": mode,
                    "status": status,
                    "source": "simulated",
                    "record_count": 100,
                    "started_at": start.isoformat(),
                    "ended_at": now.isoformat() if elapsed is not None else None,
                    "metadata": Settings().public_metadata(),
                }
            )
            for record_index in range(good + bad):
                record_id = f"record-{record_index:06d}"
                store.save(
                    "records",
                    run_id,
                    record_id,
                    {
                        "id": record_id,
                        "status": "succeeded" if record_index < good else "failed",
                        "error": None if record_index < good else "invalid_output_schema",
                        "usage": {"prompt_tokens": 400, "completion_tokens": 80},
                    },
                )
            for attempt in range(throttled):
                item = event(
                    "http_attempt", duration_seconds=0.1, status_code=429, error="RateLimitError"
                )
                item["event_id"] = f"simulated-throttle-{attempt}"
                store.add_event(run_id, item)
            store.save(
                "jobs",
                run_id,
                "part-0000",
                {
                    "id": "part-0000",
                    "status": status,
                    "created_at": start.isoformat(),
                    "submitted_at": start.isoformat(),
                    "completed_at": now.isoformat() if elapsed is not None else None,
                    "estimated_tokens": 40000,
                },
            )
    finally:
        store.close()
