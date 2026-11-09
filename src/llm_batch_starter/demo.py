from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from .complex_workload import Expected, configure, generate_complex
from .config import Settings
from .quality import annotate_result
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
        run_id = "simulated-complex-v2"
        configured, profile = configure(Settings())
        store.save_run(
            {
                "id": run_id,
                "mode": "batch",
                "status": "succeeded",
                "source": "simulated",
                "record_count": 13,
                "started_at": (now - timedelta(seconds=180)).isoformat(),
                "ended_at": now.isoformat(),
                "metadata": configured.public_metadata(),
                "workload_profile": profile,
            }
        )
        for index, record in enumerate(generate_complex(13)):
            expected = Expected.model_validate(record.expected_result)
            confidence = expected.minimum_confidence
            result = {
                **expected.fields,
                "confidence": confidence,
                "confidence_basis": expected.archived_basis
                if confidence == expected.archived_score
                else "Illustrative synthetic evidence supports this fixture.",
            }
            if index == 0:
                result["primary_route"] = "R001" if result["primary_route"] != "R001" else "R002"
            outcome = {
                "status": "succeeded",
                "result": result,
                "usage": {"prompt_tokens": 32000, "completion_tokens": 350},
            }
            annotate_result(record, outcome)
            store.save("records", run_id, record.id, outcome)
        store.save(
            "jobs",
            run_id,
            "part-0000",
            {
                "id": "part-0000",
                "status": "collected",
                "created_at": (now - timedelta(seconds=180)).isoformat(),
                "completed_at": now.isoformat(),
                "estimated_tokens": 416000,
                "request_counts": {"total": 13, "completed": 13, "failed": 0},
            },
        )
    finally:
        store.close()
