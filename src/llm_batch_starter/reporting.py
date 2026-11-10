from __future__ import annotations

import csv
import io
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .complex_workload import FIELDS
from .quality import summarize_quality
from .reconcile import is_retryable
from .state import State
from .telemetry import elapsed_between


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_documents(
    run: dict[str, Any],
    records: list[dict[str, Any]],
    events: list[dict[str, Any]],
    jobs: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses = Counter(record["status"] for record in records)
    elapsed = elapsed_between(run["started_at"], run.get("ended_at"))
    durations: dict[str, float] = defaultdict(float)
    for item in events:
        if item.get("duration_seconds") is not None:
            durations[item["stage"]] += item["duration_seconds"]
    attempts = [item for item in events if item["stage"] == "http_attempt"]
    usage_items = [item.get("usage") or {} for item in attempts]
    if run["mode"] == "batch":
        usage_items = [item.get("usage") or {} for item in records]
    input_values = [
        item["prompt_tokens"] for item in usage_items if item.get("prompt_tokens") is not None
    ]
    output_values = [
        item["completion_tokens"]
        for item in usage_items
        if item.get("completion_tokens") is not None
    ]
    input_tokens = sum(input_values) if input_values else None
    output_tokens = sum(output_values) if output_values else None
    cached_values = [
        (item.get("prompt_tokens_details") or {}).get("cached_tokens") for item in usage_items
    ]
    reasoning_values = [
        (item.get("completion_tokens_details") or {}).get("reasoning_tokens")
        for item in usage_items
    ]
    cache_tokens = (
        sum(value for value in cached_values if value is not None)
        if any(value is not None for value in cached_values)
        else None
    )
    reasoning = (
        sum(value for value in reasoning_values if value is not None)
        if any(value is not None for value in reasoning_values)
        else None
    )
    successful_latencies = [
        item["duration_seconds"]
        for item in attempts
        if item.get("status_code") == 200 and item.get("duration_seconds") is not None
    ]
    attempt_latencies = [
        item["duration_seconds"] for item in attempts if item.get("duration_seconds") is not None
    ]
    rate_errors = sum(item.get("status_code") == 429 for item in events)
    observed_limits = sorted(
        {
            (
                item.get("headers", {}).get("x-ratelimit-limit-requests", ""),
                item.get("headers", {}).get("x-ratelimit-limit-tokens", ""),
            )
            for item in events
            if item.get("headers", {}).get("x-ratelimit-limit-requests")
            or item.get("headers", {}).get("x-ratelimit-limit-tokens")
        }
    )
    batch_record_errors = (
        sum(item.get("status_code") == 429 for item in records) if run["mode"] == "batch" else 0
    )
    pricing = run.get("metadata", {})
    priced_cost = None
    if (
        "input_price_per_million" in pricing
        and input_tokens is not None
        and output_tokens is not None
    ):
        priced_cost = (
            (input_tokens - (cache_tokens or 0)) * pricing["input_price_per_million"]
            + (cache_tokens or 0) * pricing["cached_input_price_per_million"]
            + output_tokens * pricing["output_price_per_million"]
        ) / 1_000_000
        if run["mode"] == "batch":
            priced_cost *= pricing["batch_discount"]
    expected = run["record_count"]
    quality = summarize_quality(records, expected)
    correctness = [item["label_correct"] for item in records if "label_correct" in item]
    record_durations = [
        item["elapsed_seconds"] for item in records if item.get("elapsed_seconds") is not None
    ]
    good_durations = [
        item["elapsed_seconds"]
        for item in records
        if item["status"] == "succeeded" and item.get("elapsed_seconds") is not None
    ]
    queue_durations = [
        item["queue_to_completion_seconds"]
        for item in records
        if item.get("queue_to_completion_seconds") is not None
    ]
    initial_waits = [
        item["backlog_wait_seconds"]
        for item in records
        if item.get("backlog_wait_seconds") is not None
    ]
    buckets: dict[str, dict[str, int]] = {}
    for item in attempts:
        minute = item.get("requested_at", item["at"])[:16]
        bucket = buckets.setdefault(
            minute, {"http_attempts": 0, "observed_tokens": 0, "errors_429": 0}
        )
        bucket["http_attempts"] += 1
        bucket["errors_429"] += int(item.get("status_code") == 429)
        response_bucket = buckets.setdefault(
            item["at"][:16], {"http_attempts": 0, "observed_tokens": 0, "errors_429": 0}
        )
        response_bucket["observed_tokens"] += (item.get("usage") or {}).get("total_tokens") or 0
    return {
        **run,
        "succeeded": statuses["succeeded"],
        "failed": statuses["failed"],
        "pending": max(0, expected - len(records)),
        "elapsed_seconds": elapsed,
        "completed_records_per_minute": statuses["succeeded"] * 60 / elapsed if elapsed else 0,
        "http_attempts": len(attempts),
        "observed_rate_limits": [
            {"requests_per_minute": requests, "tokens_per_minute": tokens}
            for requests, tokens in observed_limits
        ],
        "errors_429": rate_errors + batch_record_errors,
        "batch_record_429": batch_record_errors,
        "control_plane_429": sum(
            item.get("status_code") == 429
            for item in events
            if item["stage"] == "batch_control_error"
        ),
        "errors_by_code": dict(
            Counter(
                str(item.get("status_code") or item.get("error"))
                for item in attempts
                if item.get("status_code") != 200
            )
        ),
        "retry_wait_seconds": durations.get("retry_wait", 0),
        "router_wait_seconds": durations.get("router_wait", 0),
        "stage_aggregate_seconds": dict(durations),
        "stage_timing_note": "Aggregates may overlap; never add them to derive wall-clock duration.",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "usage_reports": sum(
            item.get("prompt_tokens") is not None and item.get("completion_tokens") is not None
            for item in usage_items
        ),
        "usage_complete": bool(usage_items)
        and len(input_values) == len(output_values) == len(usage_items)
        and (run["mode"] != "batch" or len(records) == expected),
        "cached_tokens": cache_tokens,
        "reasoning_tokens": reasoning,
        "http_200_latency_p50_seconds": percentile(successful_latencies, 0.5),
        "http_200_latency_mean_seconds": mean(successful_latencies),
        "http_200_latency_measured_count": len(successful_latencies),
        "http_attempt_latency_mean_seconds": mean(attempt_latencies),
        "http_attempt_latency_measured_count": len(attempt_latencies),
        "http_200_latency_p95_seconds": percentile(successful_latencies, 0.95),
        "successful_record_elapsed_mean_seconds": sum(good_durations) / len(good_durations)
        if good_durations
        else None,
        "all_record_elapsed_mean_seconds": sum(record_durations) / len(record_durations)
        if record_durations
        else None,
        "record_elapsed_measured_count": len(record_durations),
        "successful_record_elapsed_measured_count": len(good_durations),
        "record_elapsed_p95_seconds": percentile(record_durations, 0.95),
        "queue_to_completion_mean_seconds": mean(queue_durations),
        "queue_to_completion_p95_seconds": percentile(queue_durations, 0.95),
        "queue_to_completion_measured_count": len(queue_durations),
        "backlog_wait_mean_seconds": mean(initial_waits),
        "http_attempted_record_count": len({item.get("record_id") for item in attempts}),
        "priced_usage_estimate_usd": priced_cost,
        "cost_note": "Configured retail rates applied to reported usage, not an Azure invoice or full infrastructure cost.",
        "label_accuracy": sum(correctness) / len(correctness) if correctness else None,
        "accuracy_denominator": len(correctness),
        "quality": quality,
        "semantic_status": (
            "passed"
            if quality["passed_records"] == expected
            else "needs_review"
            if quality["evaluated_records"] == expected
            else "not_fully_evaluated"
        )
        if quality
        else None,
        "per_minute": buckets,
        "jobs": jobs,
        "retryable_failed_records_count": sum(
            item["status"] == "failed" and is_retryable(item) for item in records
        ),
        "safe_failed_records": [
            {key: item.get(key) for key in ("id", "status", "error", "status_code", "retryable")}
            for item in records
            if item["status"] == "failed"
        ][:100],
        "safe_events": [
            {
                key: item.get(key)
                for key in (
                    "at",
                    "stage",
                    "record_id",
                    "partition_id",
                    "status_code",
                    "error",
                    "duration_seconds",
                )
            }
            for item in events
            if item.get("status_code") not in {None, 200} or item["stage"] == "retry_wait"
        ][-100:],
        "inference_latency_source": "unavailable_per_record"
        if run["mode"] == "batch"
        else "client_http_attempt",
    }


def summarize_run(store: State, run_id: str) -> dict[str, Any]:
    return summarize_documents(
        store.get_run(run_id),
        store.items("records", run_id),
        store.items("events", run_id),
        store.items("jobs", run_id),
    )


def export_results(store: State, run_id: str, directory: Path) -> dict[str, Any]:
    directory.mkdir(parents=True, exist_ok=True)
    summary = summarize_run(store, run_id)
    (directory / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    results = store.items("records", run_id)
    with (directory / "results.jsonl").open("w", encoding="utf-8", newline="\n") as stream:
        for result in results:
            stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    (directory / "results.csv").write_text(results_csv(results), encoding="utf-8")
    if summary["quality"] is not None:
        (directory / "analysis.csv").write_text(analysis_csv(results), encoding="utf-8")
    return summary


def results_csv(results: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["id", "status", "error", "result"])
    writer.writeheader()
    for result in results:
        writer.writerow(
            {
                "id": result["id"],
                "status": result["status"],
                "error": result.get("error", ""),
                "result": json.dumps(result.get("result"), ensure_ascii=False),
            }
        )
    return stream.getvalue()


def analysis_csv(results: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["id", *FIELDS])
    writer.writeheader()
    for row in results:
        result = row.get("result") if row["status"] == "succeeded" else {}
        values = {key: (result or {}).get(key, "") for key in FIELDS}
        for key in ("secondary_routes", "improvements"):
            if isinstance(values[key], list):
                values[key] = " | ".join(values[key])
        for key, value in values.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                values[key] = "'" + value
        writer.writerow({"id": row["id"], **values})
    return stream.getvalue()
