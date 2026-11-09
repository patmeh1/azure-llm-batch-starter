from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Any

from .batch import run_batches
from .config import Settings
from .online import run_online
from .prepare import estimate_cost, input_fingerprint, prepare_requests
from .provider import AzureProvider
from .reporting import export_results
from .state import State
from .synthetic import Record, write_records
from .telemetry import event, timed_stage, utc_now


def run_pipeline(
    records: list[Record],
    settings: Settings,
    mode: str,
    state_path: Path,
    output_dir: Path,
    *,
    run_id: str | None = None,
    wait: bool = True,
    provider: Any = None,
    workload_profile: dict[str, Any] | None = None,
    parent_run_id: str | None = None,
) -> dict[str, Any]:
    invocation_started_at = utc_now()
    preparation_started = time.perf_counter()
    if mode not in {"sequential", "parallel", "batch"}:
        raise ValueError("Invalid processing mode")
    partitions = prepare_requests(records, settings)
    estimate = estimate_cost(records, settings, batch=mode == "batch")
    if estimate["estimated_inference_usd"] > settings.max_run_cost_usd:
        raise ValueError(
            "Estimated inference exceeds max_run_cost_usd; review before increasing it"
        )
    run_id = run_id or uuid.uuid4().hex
    if len(run_id) > 64 or not run_id.replace("-", "").isalnum():
        raise ValueError("Invalid run ID")
    store = State(state_path)
    directory = output_dir / run_id
    fingerprint = input_fingerprint(records, settings)
    existing = {r["id"]: r for r in store.list_runs()}
    if run_id in existing:
        if existing[run_id]["fingerprint"] != fingerprint or existing[run_id]["mode"] != mode:
            store.close()
            raise ValueError("Resume requires identical inputs, model, and configuration")
        if existing[run_id]["status"] in {
            "succeeded",
            "partial_failure",
            "cancelled",
            "needs_attention",
        }:
            try:
                return export_results(store, run_id, directory)
            finally:
                store.close()
    else:
        directory.mkdir(parents=True, exist_ok=False)
        write_records(directory / "input.jsonl", records)
        (directory / "config.json").write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        (directory / "prompt.md").write_text(settings.prompt + "\n", encoding="utf-8", newline="\n")
        store.save_run(
            {
                "id": run_id,
                "mode": mode,
                "status": "running",
                "started_at": invocation_started_at,
                "ended_at": None,
                "record_count": len(records),
                "fingerprint": fingerprint,
                "metadata": settings.public_metadata(),
                "estimate": estimate,
                "source": "live",
                "directory": str(directory),
                "workload_profile": workload_profile or {},
                "parent_run_id": parent_run_id,
            }
        )
        store.add_event(
            run_id, event("preparation", duration_seconds=time.perf_counter() - preparation_started)
        )
    owns_provider = provider is None
    actual_provider = provider or AzureProvider(settings)
    try:
        with timed_stage(lambda e: store.add_event(run_id, e), "pipeline"):
            if mode == "batch":
                run_batches(
                    partitions, settings, store, run_id, actual_provider, directory, wait=wait
                )
            else:
                run_online(
                    records, settings, store, run_id, actual_provider, parallel=mode == "parallel"
                )
        return export_results(store, run_id, directory)
    finally:
        if owns_provider:
            actual_provider.close()
        store.close()
