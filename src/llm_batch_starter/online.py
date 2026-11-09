from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

import openai
from filelock import FileLock

from .config import Settings
from .prepare import estimate_input_tokens
from .provider import rate_headers
from .quality import annotate_result
from .reconcile import validate_completion
from .retry import AdmissionLimiter, retry_delay
from .state import State
from .synthetic import Record
from .telemetry import elapsed_between, event, safe_error, utc_now


class CompletionProvider(Protocol):
    def complete(self, record: Record) -> tuple[dict[str, Any], dict[str, str]]: ...


class CostGuard:
    def __init__(self, limit: float):
        self.limit = limit
        self.reserved = 0.0
        self.lock = threading.Lock()

    def reserve(self, amount: float) -> None:
        with self.lock:
            if self.reserved + amount > self.limit:
                raise TimeoutError("Inference estimate budget exhausted")
            self.reserved += amount


def partition_client_groups(records: list[Record], count: int) -> list[list[Record]]:
    if not 1 <= count <= len(records):
        raise ValueError("Client group count must be between one and the record count")
    size, remainder = divmod(len(records), count)
    groups = []
    start = 0
    for index in range(count):
        end = start + size + int(index < remainder)
        groups.append(records[start:end])
        start = end
    return groups


def run_online(
    records: list[Record],
    settings: Settings,
    store: State,
    run_id: str,
    provider: CompletionProvider,
    *,
    parallel: bool,
    client_groups: int | None = None,
) -> None:
    limiter = AdmissionLimiter(settings.rpm, settings.tpm)
    full_reproduction = settings.observation_profile == "full-reproduction"
    prior_elapsed = elapsed_between(store.get_run(run_id)["started_at"]) if full_reproduction else 0
    deadline = time.monotonic() + max(0, settings.run_timeout_seconds - prior_elapsed)
    cost = CostGuard(settings.max_run_cost_usd)
    events = store.items("events", run_id) if full_reproduction else []
    started_requests = [e for e in events if e["stage"] == "http_request_started"]
    finished_requests = {
        (e["record_id"], e["attempt"]): e for e in events if e["stage"] == "http_attempt"
    }
    if full_reproduction:
        limiter.attempts = len(started_requests)
        cost.reserved = sum(
            (
                e["estimated_input_tokens"] * settings.input_price_per_million
                + settings.max_output_tokens * settings.output_price_per_million
            )
            / 1_000_000
            for e in started_requests
        )
    completed = {
        item["id"]
        for item in store.items("records", run_id)
        if item["status"] in {"succeeded", "failed"}
    }
    lock_key = hashlib.sha256((settings.endpoint + settings.online_deployment).encode()).hexdigest()
    lock_dir = Path(settings.coordination_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)

    last_started = {e["record_id"]: e for e in started_requests}

    def process(record: Record, group_id: str | None = None) -> None:
        if record.id in completed:
            return
        if record.id in last_started:
            previous = last_started[record.id]
            finish = finished_requests.get((record.id, previous["attempt"]))
            status = finish.get("status_code") if finish else None
            interrupted_outcome = {
                "status": "failed",
                "error": "interrupted_after_response"
                if finish and status != 200
                else "ambiguous_online_result",
                "status_code": status if status != 200 else None,
                "retryable": bool(finish and status in {408, 409, 429, 500, 502, 503, 504}),
                "group_id": group_id,
                "attempts": previous["attempt"],
            }
            annotate_result(record, interrupted_outcome)
            store.save("records", run_id, record.id, interrupted_outcome)
            return
        started = time.monotonic()
        record_started_at = utc_now()
        tokens = estimate_input_tokens(record, settings)
        outcome: dict[str, Any] = {"status": "failed", "error": "attempts_exhausted"}
        attempts_made = 0
        for attempt in range(1, settings.max_attempts + 1):
            try:
                wait = limiter.acquire(
                    tokens + settings.max_output_tokens,
                    deadline,
                    settings.max_http_attempts,
                    observe=settings.observe_service_throttling,
                )
                cost.reserve(
                    (
                        tokens * settings.input_price_per_million
                        + settings.max_output_tokens * settings.output_price_per_million
                    )
                    / 1_000_000
                )
            except (TimeoutError, ValueError) as exc:
                outcome = {
                    "status": "failed",
                    "error": str(exc),
                    "retryable": isinstance(exc, TimeoutError),
                }
                break
            store.add_event(
                run_id, event("admission_wait", duration_seconds=wait, record_id=record.id)
            )
            api_started = time.monotonic()
            requested_at = utc_now()
            if full_reproduction:
                store.add_event(
                    run_id,
                    event(
                        "http_request_started",
                        record_id=record.id,
                        group_id=group_id,
                        attempt=attempt,
                        estimated_input_tokens=tokens,
                    ),
                )
            attempts_made += 1
            try:
                body, headers = provider.complete(record)
                elapsed = time.monotonic() - api_started
                store.add_event(
                    run_id,
                    event(
                        "http_attempt",
                        duration_seconds=elapsed,
                        record_id=record.id,
                        status_code=200,
                        attempt=attempt,
                        requested_at=requested_at,
                        estimated_input_tokens=tokens,
                        headers=rate_headers(headers),
                        usage=body.get("usage") or {},
                        group_id=group_id,
                    ),
                )
                outcome = validate_completion(body, settings.output_schema)
                break
            except (openai.APIStatusError, openai.APIConnectionError) as exc:
                error = safe_error(exc)
                status = error["status_code"]
                store.add_event(
                    run_id,
                    event(
                        "http_attempt",
                        duration_seconds=time.monotonic() - api_started,
                        record_id=record.id,
                        status_code=status,
                        attempt=attempt,
                        requested_at=requested_at,
                        estimated_input_tokens=tokens,
                        error=error["type"],
                        headers=error["headers"],
                        group_id=group_id,
                    ),
                )
                outcome = {"status": "failed", "error": error["type"], "status_code": status}
                if status is not None and status not in {408, 409, 429} and status < 500:
                    break
                if attempt == settings.max_attempts:
                    break
                delay = retry_delay(error["headers"], attempt)
                if time.monotonic() + delay >= deadline:
                    outcome["error"] = "retry_deadline_reached"
                    break
                time.sleep(delay)
                store.add_event(
                    run_id,
                    event(
                        "retry_wait", duration_seconds=delay, record_id=record.id, attempt=attempt
                    ),
                )
        outcome.update(
            id=record.id,
            elapsed_seconds=time.monotonic() - started,
            expected_label=record.expected_label,
            started_at=record_started_at,
            ended_at=utc_now(),
            group_id=group_id,
            attempts=attempts_made,
        )
        annotate_result(record, outcome)
        store.save("records", run_id, record.id, outcome)

    def process_group(index: int, group: list[Record]) -> None:
        group_id = f"group-{index:02d}"
        old = next((j for j in store.items("jobs", run_id) if j["id"] == group_id), None)
        if old and old["status"] in {"succeeded", "partial_failure"} and all(
            record.id in completed for record in group
        ):
            return
        job = old or {
            "id": group_id,
            "kind": "client_group",
            "created_at": utc_now(),
            "record_ids": [record.id for record in group],
        }
        job.update(status="running")
        store.save("jobs", run_id, group_id, job)
        for record in group:
            process(record, group_id)
        ids = set(job["record_ids"])
        outcomes = [r for r in store.items("records", run_id) if r["id"] in ids]
        successes = sum(r["status"] == "succeeded" for r in outcomes)
        job.update(
            status="succeeded" if successes == len(group) else "partial_failure",
            completed_at=utc_now(),
            request_counts={
                "total": len(group),
                "completed": successes,
                "failed": len(outcomes) - successes,
            },
        )
        store.save("jobs", run_id, group_id, job)

    with FileLock(str(lock_dir / f"{lock_key}.lock"), timeout=0):
        if client_groups is not None:
            groups = partition_client_groups(records, client_groups)
            with ThreadPoolExecutor(max_workers=client_groups) as executor:
                list(executor.map(lambda pair: process_group(*pair), enumerate(groups)))
        elif parallel:
            with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
                list(executor.map(process, records))
        else:
            for record in records:
                process(record)
    outcomes = store.items("records", run_id)
    success = len(outcomes) == len(records) and all(r["status"] == "succeeded" for r in outcomes)
    store.finish(run_id, "succeeded" if success else "partial_failure")
