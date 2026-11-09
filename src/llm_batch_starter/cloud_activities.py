from __future__ import annotations

import json
import time
from typing import Any

import openai

from .azure_state import AzureState, create_cloud_run
from .batch import KNOWN, TERMINAL
from .config import Settings, load_settings
from .prepare import prepare_requests
from .provider import AzureProvider
from .reconcile import is_retryable, reconcile_results
from .reporting import analysis_csv, results_csv, summarize_documents
from .retry import retry_delay
from .synthetic import Record
from .telemetry import canonical_json, event, safe_error, utc_now

IMMUTABLE_CLOUD_FIELDS = {
    "endpoint",
    "auth_mode",
    "managed_identity_client_id",
    "storage_account",
    "online_deployment",
    "batch_deployment",
    "model_name",
    "model_version",
    "region",
    "coordination_dir",
    "container",
    "queue_name",
    "table_name",
    "max_enqueued_tokens",
    "max_active_batches",
    "scheduler_endpoint",
    "task_hub",
}


class CloudActivities:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or load_settings(auth_mode="managed_identity")
        self.store = AzureState(self.settings)
        self.provider = AzureProvider(self.settings)

    def close(self) -> None:
        self.provider.close()
        self.store.close()

    def manifest(self, run_id: str) -> tuple[list[Record], Settings]:
        value = json.loads(self.store.get_blob(f"{run_id}/manifest.json"))
        changes = {
            key: val for key, val in value["settings"].items() if key not in IMMUTABLE_CLOUD_FIELDS
        }
        settings = Settings.model_validate({**self.settings.model_dump(), **changes})
        return [Record.model_validate(r) for r in value["records"]], settings

    def prepare(self, run_id: str) -> dict[str, Any]:
        records, settings = self.manifest(run_id)
        parts = prepare_requests(records, settings)
        for part in parts:
            if self.store.get_item("jobs", run_id, part.id):
                continue
            blob = f"{run_id}/inputs/{part.id}.jsonl"
            self.store.put_blob(blob, part.payload, overwrite=True)
            self.store.save(
                "jobs",
                run_id,
                part.id,
                {
                    "id": part.id,
                    "status": "prepared",
                    "input_blob": blob,
                    "record_ids": [r.id for r in part.records],
                    "estimated_tokens": part.estimated_tokens,
                    "submission_key": f"{run_id}-{part.id}",
                    "created_at": utc_now(),
                },
            )
        run = self.store.get_run(run_id)
        run["status"] = "running"
        self.store.save_run(run)
        return {
            "parts": [{"id": p.id, "tokens": p.estimated_tokens} for p in parts],
            "max_enqueued_tokens": self.settings.max_enqueued_tokens,
            "max_active_batches": self.settings.max_active_batches,
            "poll_seconds": settings.poll_seconds,
            "max_poll_seconds": settings.max_poll_seconds,
            "cloud_timeout_seconds": settings.cloud_timeout_seconds,
        }

    def prepare_retry(self, parent_run_id: str, run_id: str) -> dict[str, Any]:
        parent = self.store.get_run(parent_run_id)
        if parent["status"] in {"queued", "running", "pending"}:
            raise ValueError("Reconcile the parent run before retrying records")
        records, settings = self.manifest(parent_run_id)
        eligible = {
            item["id"]
            for item in self.store.items("records", parent_run_id)
            if item["status"] == "failed" and is_retryable(item)
        }
        if not eligible:
            raise ValueError(
                "No confirmed retryable failures; refusals and ambiguous results require review"
            )
        selected = [record for record in records if record.id in eligible]
        run = create_cloud_run(
            selected,
            settings,
            run_id=run_id,
            enqueue=False,
            parent_run_id=parent_run_id,
            workload_profile=parent.get("workload_profile"),
        )
        return {"run_id": run["id"], "record_count": len(selected)}

    def step(self, run_id: str, part_id: str) -> dict[str, Any]:
        job = self.store.get_item("jobs", run_id, part_id)
        if not job:
            raise ValueError("Missing partition metadata")
        if job["status"] in {"collected", "cancelled_before_submission", "blocked"}:
            return {
                "terminal": True,
                "release": job["status"] != "blocked" or not job.get("remote_id"),
                "status": job["status"],
            }
        start = time.monotonic()
        control = self.store.get_item("control", run_id, "cancel") or job.get("cancel_requested")
        if control and not job.get("remote_id") and job["status"] == "prepared":
            job.update(status="cancelled_before_submission")
            self.store.save("jobs", run_id, part_id, job)
            return {"terminal": True, "release": True, "status": job["status"]}
        _, settings = self.manifest(run_id)
        self.provider.settings = settings
        try:
            if not job.get("remote_id"):
                if job["status"] in {"submitting", "ambiguous"}:
                    remote = self.provider.find_submission(job["submission_key"])
                    if remote is None:
                        job["status"] = "ambiguous"
                        self.store.save("jobs", run_id, part_id, job)
                        return {"terminal": True, "release": False, "status": "ambiguous"}
                else:
                    if not job.get("file_id"):
                        job["file_id"] = self.provider.upload(
                            self.store.get_blob(job["input_blob"]), job["submission_key"] + ".jsonl"
                        )
                        self.store.save("jobs", run_id, part_id, job)
                    job["status"] = "submitting"
                    self.store.save("jobs", run_id, part_id, job)
                    remote = self.provider.submit(job["file_id"], job["submission_key"])
                job.update(remote_id=remote["id"], status=remote["status"], submitted_at=utc_now())
                self.store.save("jobs", run_id, part_id, job)
            remote = self.provider.poll(job["remote_id"])
            if remote["status"] not in KNOWN:
                raise ValueError("Unknown remote Batch status")
            if control and remote["status"] not in TERMINAL | {"cancelling"}:
                remote = self.provider.cancel(job["remote_id"])
            job.update(
                status=remote["status"],
                last_polled_at=utc_now(),
                request_counts=remote.get("request_counts"),
                service_timestamps={
                    key: value for key, value in remote.items() if key.endswith("_at")
                },
            )
            self.store.save("jobs", run_id, part_id, job)
            if remote["status"] in TERMINAL:
                self.collect(run_id, job, remote, settings)
                return {"terminal": True, "release": True, "status": remote["status"]}
            return {
                "terminal": False,
                "status": remote["status"],
                "request_counts": remote.get("request_counts"),
            }
        except (openai.APIStatusError, openai.APIConnectionError) as exc:
            error = safe_error(exc)
            self.store.add_event(
                run_id,
                event(
                    "batch_control_error",
                    status_code=error["status_code"],
                    partition_id=part_id,
                    error=error["type"],
                    headers=error["headers"],
                ),
            )
            if job["status"] == "submitting":
                job["status"] = "prepared" if error["status_code"] == 429 else "ambiguous"
                self.store.save("jobs", run_id, part_id, job)
            if error["status_code"] in {400, 401, 403, 404, 422}:
                job.update(status="blocked", error=error["type"])
                self.store.save("jobs", run_id, part_id, job)
                return {
                    "terminal": True,
                    "release": not bool(job.get("remote_id")),
                    "status": "blocked",
                }
            return {
                "terminal": False,
                "status": job["status"],
                "retry_after": retry_delay(error["headers"], 1),
            }
        finally:
            self.store.add_event(
                run_id,
                event(
                    "batch_step",
                    duration_seconds=time.monotonic() - start,
                    partition_id=part_id,
                ),
            )

    def admission_cancelled(self, run_id: str, part_id: str) -> bool:
        job = self.store.get_item("jobs", run_id, part_id)
        if not job:
            raise ValueError("Missing partition metadata")
        if job["status"] == "cancelled_before_submission":
            return True
        control = self.store.get_item("control", run_id, "cancel") or job.get("cancel_requested")
        if control and job["status"] == "prepared" and not job.get("remote_id"):
            job["status"] = "cancelled_before_submission"
            self.store.save("jobs", run_id, part_id, job)
            return True
        return False

    def request_cancel(self, run_id: str, part_id: str) -> None:
        job = self.store.get_item("jobs", run_id, part_id)
        if not job:
            raise ValueError("Missing partition metadata")
        job["cancel_requested"] = True
        self.store.save("jobs", run_id, part_id, job)

    def collect(
        self,
        run_id: str,
        job: dict[str, Any],
        remote: dict[str, Any],
        settings: Settings,
    ) -> None:
        all_records, _ = self.manifest(run_id)
        expected = set(job["record_ids"])
        records = [r for r in all_records if r.id in expected]
        output = (
            self.provider.download(remote["output_file_id"]) if remote.get("output_file_id") else ""
        )
        errors = (
            self.provider.download(remote["error_file_id"]) if remote.get("error_file_id") else ""
        )
        results, issues = reconcile_results(records, output, errors, settings.output_schema)
        self.store.put_blob(
            f"{run_id}/outputs/{job['id']}.jsonl",
            ("\n".join(canonical_json(r) for r in results) + "\n").encode(),
            overwrite=True,
        )
        for result in results:
            self.store.save("records", run_id, result["id"], result)
        job.update(
            status="collected",
            remote_status=remote["status"],
            completed_at=utc_now(),
            service_timestamps={k: v for k, v in remote.items() if k.endswith("_at")},
            reconciliation_issues=issues,
        )
        self.store.save("jobs", run_id, job["id"], job)

    def finalize(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        records = self.store.items("records", run_id)
        jobs = self.store.items("jobs", run_id)
        good = len(records) == run["record_count"] and all(
            r["status"] == "succeeded" for r in records
        )
        good = good and not any(j.get("reconciliation_issues") for j in jobs)
        run.update(
            status="succeeded" if good else "needs_attention",
            ended_at=utc_now(),
        )
        summary = summarize_documents(run, records, self.store.items("events", run_id), jobs)
        self.store.put_blob(
            f"{run_id}/summary.json", canonical_json(summary).encode(), overwrite=True
        )
        self.store.put_blob(
            f"{run_id}/results.jsonl",
            ("\n".join(canonical_json(r) for r in records) + "\n").encode(),
            overwrite=True,
        )
        self.store.put_blob(f"{run_id}/results.csv", results_csv(records).encode(), overwrite=True)
        if summary["quality"] is not None:
            self.store.put_blob(
                f"{run_id}/analysis.csv", analysis_csv(records).encode(), overwrite=True
            )
        run["summary"] = {
            key: summary[key]
            for key in (
                "succeeded",
                "failed",
                "pending",
                "elapsed_seconds",
                "completed_records_per_minute",
                "errors_429",
                "input_tokens",
                "output_tokens",
                "label_accuracy",
                "quality",
                "semantic_status",
            )
        }
        self.store.save_run(run)
        return {"run_id": run_id, "status": run["status"], **run["summary"], "report": summary}
