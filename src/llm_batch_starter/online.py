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
from .telemetry import event, safe_error, utc_now


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


def run_online(
    records: list[Record],
    settings: Settings,
    store: State,
    run_id: str,
    provider: CompletionProvider,
    *,
    parallel: bool,
) -> None:
    limiter = AdmissionLimiter(settings.rpm, settings.tpm)
    deadline = time.monotonic() + settings.run_timeout_seconds
    cost = CostGuard(settings.max_run_cost_usd)
    completed = {
        item["id"]
        for item in store.items("records", run_id)
        if item["status"] in {"succeeded", "failed"}
    }
    lock_key = hashlib.sha256((settings.endpoint + settings.online_deployment).encode()).hexdigest()
    lock_dir = Path(settings.coordination_dir)
    lock_dir.mkdir(parents=True, exist_ok=True)

    def process(record: Record) -> None:
        if record.id in completed:
            return
        started = time.monotonic()
        tokens = estimate_input_tokens(record, settings)
        outcome: dict[str, Any] = {"status": "failed", "error": "attempts_exhausted"}
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
        )
        annotate_result(record, outcome)
        store.save("records", run_id, record.id, outcome)

    with FileLock(str(lock_dir / f"{lock_key}.lock"), timeout=0):
        if parallel:
            with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
                list(executor.map(process, records))
        else:
            for record in records:
                process(record)
    outcomes = store.items("records", run_id)
    success = len(outcomes) == len(records) and all(r["status"] == "succeeded" for r in outcomes)
    store.finish(run_id, "succeeded" if success else "partial_failure")
