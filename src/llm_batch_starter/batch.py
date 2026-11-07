from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Protocol

import openai

from .config import Settings
from .prepare import Partition
from .reconcile import reconcile_results
from .retry import retry_delay
from .state import State
from .telemetry import event, safe_error, utc_now

TERMINAL = {"completed", "failed", "expired", "cancelled"}
KNOWN = TERMINAL | {"validating", "in_progress", "finalizing", "cancelling"}


class BatchProvider(Protocol):
    def upload(self, payload: bytes, filename: str) -> str: ...
    def submit(self, file_id: str, submission_key: str) -> dict[str, Any]: ...
    def poll(self, job_id: str) -> dict[str, Any]: ...
    def find_submission(self, submission_key: str) -> dict[str, Any] | None: ...
    def download(self, file_id: str) -> str: ...
    def cancel(self, job_id: str) -> dict[str, Any]: ...


def register_jobs(partitions: list[Partition], store: State, run_id: str, directory: Path) -> None:
    existing = {job["id"] for job in store.items("jobs", run_id)}
    for partition in partitions:
        if partition.id in existing:
            continue
        path = directory / f"{partition.id}.jsonl"
        path.write_bytes(partition.payload)
        store.save(
            "jobs",
            run_id,
            partition.id,
            {
                "id": partition.id,
                "status": "prepared",
                "record_ids": [record.id for record in partition.records],
                "estimated_tokens": partition.estimated_tokens,
                "submission_key": f"{run_id}-{partition.id}",
                "input_path": str(path),
                "created_at": utc_now(),
            },
        )


def submit_job(
    job: dict[str, Any], provider: BatchProvider, store: State, run_id: str
) -> dict[str, Any]:
    if job.get("remote_id"):
        return job
    if job["status"] in {"submitting", "ambiguous"}:
        match = provider.find_submission(job["submission_key"])
        if match is None:
            job["status"] = "ambiguous"
            store.save("jobs", run_id, job["id"], job)
            raise RuntimeError("Submission outcome is ambiguous; no automatic resubmission")
        remote = match
    else:
        if not job.get("file_id"):
            started = time.monotonic()
            job["file_id"] = provider.upload(
                Path(job["input_path"]).read_bytes(), job["submission_key"] + ".jsonl"
            )
            store.add_event(
                run_id,
                event(
                    "upload", duration_seconds=time.monotonic() - started, partition_id=job["id"]
                ),
            )
            store.save("jobs", run_id, job["id"], job)
        job["status"] = "submitting"
        store.save("jobs", run_id, job["id"], job)
        started = time.monotonic()
        try:
            remote = provider.submit(job["file_id"], job["submission_key"])
        except openai.RateLimitError:
            job["status"] = "prepared"
            store.save("jobs", run_id, job["id"], job)
            raise
        except (openai.APIConnectionError, openai.InternalServerError):
            job["status"] = "ambiguous"
            store.save("jobs", run_id, job["id"], job)
            raise
        store.add_event(
            run_id,
            event(
                "submission", duration_seconds=time.monotonic() - started, partition_id=job["id"]
            ),
        )
    job.update(remote_id=remote["id"], status=remote["status"], submitted_at=utc_now())
    store.save("jobs", run_id, job["id"], job)
    return job


def collect_results(
    job: dict[str, Any],
    remote: dict[str, Any],
    partition: Partition,
    provider: BatchProvider,
    settings: Settings,
    store: State,
    run_id: str,
) -> None:
    started = time.monotonic()
    output = provider.download(remote["output_file_id"]) if remote.get("output_file_id") else ""
    errors = provider.download(remote["error_file_id"]) if remote.get("error_file_id") else ""
    outcomes, issues = reconcile_results(partition.records, output, errors, settings.output_schema)
    for outcome in outcomes:
        store.save("records", run_id, outcome["id"], outcome)
    job.update(
        status="collected",
        remote_status=remote["status"],
        completed_at=utc_now(),
        service_timestamps={key: value for key, value in remote.items() if key.endswith("_at")},
        request_counts=remote.get("request_counts"),
        reconciliation_issues=issues,
    )
    store.save("jobs", run_id, job["id"], job)
    store.add_event(
        run_id,
        event(
            "download_reconcile",
            duration_seconds=time.monotonic() - started,
            partition_id=job["id"],
        ),
    )


def run_batches(
    partitions: list[Partition],
    settings: Settings,
    store: State,
    run_id: str,
    provider: BatchProvider,
    directory: Path,
    *,
    wait: bool = True,
) -> None:
    register_jobs(partitions, store, run_id, directory)
    lookup = {partition.id: partition for partition in partitions}
    deadline = time.monotonic() + settings.run_timeout_seconds
    poll_interval = settings.poll_seconds
    while True:
        jobs = store.items("jobs", run_id)
        active = [j for j in jobs if j.get("remote_id") and j["status"] != "collected"]
        active_tokens = sum(j["estimated_tokens"] for j in active)
        next_delay = poll_interval
        for job in jobs:
            if job["status"] == "collected":
                continue
            try:
                if not job.get("remote_id"):
                    if len(active) >= settings.max_active_batches:
                        continue
                    if active_tokens + job["estimated_tokens"] > settings.max_enqueued_tokens:
                        continue
                    job = submit_job(job, provider, store, run_id)
                    active.append(job)
                    active_tokens += job["estimated_tokens"]
                remote = provider.poll(job["remote_id"])
                if remote["status"] not in KNOWN:
                    raise RuntimeError(f"Unknown Batch status: {remote['status']}")
                job.update(
                    status=remote["status"],
                    last_polled_at=utc_now(),
                    request_counts=remote.get("request_counts"),
                    service_timestamps={
                        key: value for key, value in remote.items() if key.endswith("_at")
                    },
                )
                store.save("jobs", run_id, job["id"], job)
                if remote["status"] in TERMINAL:
                    collect_results(
                        job, remote, lookup[job["id"]], provider, settings, store, run_id
                    )
            except (openai.APIStatusError, openai.APIConnectionError) as exc:
                error = safe_error(exc)
                store.add_event(
                    run_id,
                    event(
                        "batch_control_error",
                        status_code=error["status_code"],
                        partition_id=job["id"],
                        error=error["type"],
                        headers=error["headers"],
                    ),
                )
                if error["status_code"] in {401, 403, 404, 400, 422}:
                    raise RuntimeError(f"Batch operation failed: {error['type']}") from exc
                next_delay = max(next_delay, retry_delay(error["headers"], 1))
        jobs = store.items("jobs", run_id)
        if all(job["status"] == "collected" for job in jobs):
            outcomes = store.items("records", run_id)
            success = (
                len(outcomes) == sum(len(p.records) for p in partitions)
                and all(item["status"] == "succeeded" for item in outcomes)
                and not any(job.get("reconciliation_issues") for job in jobs)
            )
            store.finish(run_id, "succeeded" if success else "partial_failure")
            return
        if not wait or time.monotonic() + next_delay >= deadline:
            run = store.get_run(run_id)
            run["status"] = "pending"
            store.save_run(run)
            return
        time.sleep(next_delay)
        store.add_event(run_id, event("poll_wait", duration_seconds=next_delay))
        poll_interval = min(settings.max_poll_seconds, poll_interval * 1.5)
