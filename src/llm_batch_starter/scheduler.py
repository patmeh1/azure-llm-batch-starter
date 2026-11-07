from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import DEFAULT_PROMPT, DEFAULT_SCHEMA, WORKFLOW_VERSION, Settings, credential
from .prepare import estimate_cost
from .reporting import summarize_documents
from .state import State
from .synthetic import generate_dataset
from .telemetry import elapsed_between, utc_now


def scheduler_timestamp(value: datetime) -> str:
    return (
        value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    ).isoformat()


class SchedulerAccess:
    """Authorized control/measurement access; never proxies raw Storage documents."""

    def __init__(self, settings: Settings, state_path: Path = Path("outputs/state.sqlite3")):
        try:
            from durabletask.azuremanaged.client import DurableTaskSchedulerClient
        except ImportError:
            raise RuntimeError(
                "Install the project's [cloud] dependencies for scheduler access"
            ) from None

        endpoint = urlparse(settings.scheduler_endpoint)
        if (
            endpoint.scheme != "https"
            or not (endpoint.hostname or "").endswith(".durabletask.io")
            or endpoint.username
            or not settings.task_hub
        ):
            raise ValueError("Set the deployed DTS_ENDPOINT and TASKHUB_NAME")
        self.settings = settings
        self.identity = credential(settings)
        self.client = DurableTaskSchedulerClient(
            host_address=endpoint.netloc,
            taskhub=settings.task_hub,
            token_credential=self.identity,
        )
        self.registry = State(state_path)
        self.history_truncated = False

    def close(self) -> None:
        self.registry.close()
        self.client.close()
        self.identity.close()

    def start_synthetic(self, count: int, seed: int, reference_tokens: int = 0) -> str:
        if not 1 <= count <= 10000 or not 0 <= reference_tokens <= 120000:
            raise ValueError(
                "Synthetic cloud limits: 1..10000 records, at most 120000 reference tokens"
            )
        if self.settings.prompt != DEFAULT_PROMPT or self.settings.output_schema != DEFAULT_SCHEMA:
            raise ValueError(
                "Scheduler demo uses the deployed synthetic task; use queue transport for custom input"
            )
        if self.settings.max_output_tokens != 1024 or self.settings.reasoning_effort != "low":
            raise ValueError("Scheduler synthetic demo uses deployed generation defaults")
        records = generate_dataset(count, seed=seed, reference_tokens=reference_tokens)
        if (
            estimate_cost(records, self.settings, batch=True)["estimated_inference_usd"]
            > self.settings.max_run_cost_usd
        ):
            raise ValueError("Synthetic input exceeds the client-side inference estimate budget")
        run_id = uuid.uuid4().hex
        self.registry.save_run(
            {
                "id": run_id,
                "mode": "batch",
                "status": "submission_pending",
                "source": "live-scheduler",
                "record_count": count,
                "started_at": utc_now(),
                "ended_at": None,
                "metadata": self.settings.public_metadata(),
            }
        )
        self.client.schedule_new_orchestration(
            "synthetic_workflow",
            input={"count": count, "seed": seed, "reference_tokens": reference_tokens},
            instance_id=run_id,
            tags={"application": "llm-batch-starter"},
            version=WORKFLOW_VERSION,
        )
        return run_id

    def get_run(self, run_id: str) -> dict[str, Any]:
        state = self.client.get_orchestration_state(run_id)
        if state is None:
            raise ValueError(f"No scheduler instance found for {run_id}; do not blindly resubmit")
        if state.name not in {"synthetic_workflow", "retry_workflow", "recovery_workflow"}:
            raise ValueError("This instance is not a supported application root")
        payload = json.loads(state.serialized_input or "{}")
        if not isinstance(payload, dict) or not isinstance(payload.get("count"), int):
            raise ValueError("Invalid synthetic orchestration input metadata")
        runtime = state.runtime_status.name.lower()
        result = (
            json.loads(state.serialized_output)
            if runtime == "completed" and state.serialized_output
            else None
        )
        status = "pending" if runtime in {"pending", "suspended"} else "running"
        terminal = runtime in {"completed", "failed", "terminated", "canceled"}
        if runtime in {"failed", "terminated", "canceled"}:
            status = "needs_attention"
        if runtime == "completed":
            status = (
                result.get("status", "needs_attention")
                if isinstance(result, dict)
                else "needs_attention"
            )
        run = {
            "id": run_id,
            "mode": "batch",
            "source": "live-scheduler",
            "status": status,
            "runtime_status": runtime,
            "record_count": payload.get("count", 0),
            "parent_run_id": payload.get("parent_run_id"),
            "artifact_run_id": payload.get("artifact_run_id", run_id),
            "started_at": scheduler_timestamp(state.created_at),
            "ended_at": scheduler_timestamp(state.last_updated_at) if terminal else None,
            "failure_type": getattr(state.failure_details, "error_type", None)
            or ("orchestration_failed" if runtime == "failed" else None),
            "metadata": result.get("report", {}).get("metadata", self.settings.public_metadata())
            if isinstance(result, dict)
            else self.settings.public_metadata(),
        }
        if isinstance(result, dict):
            run["summary"] = {key: value for key, value in result.items() if key != "report"}
        self.registry.save_run(run)
        return run

    def list_runs(self) -> list[dict[str, Any]]:
        known = {
            run["id"] for run in self.registry.list_runs() if run.get("source") == "live-scheduler"
        }
        page = self.client.list_instance_ids(page_size=100)
        self.history_truncated = bool(page.continuation_token)
        known.update(page.items)
        runs = []
        for instance_id in sorted(known):
            state = self.client.get_orchestration_state(instance_id, fetch_payloads=False)
            if state is not None and state.name in {
                "synthetic_workflow",
                "retry_workflow",
                "recovery_workflow",
            }:
                runs.append(self.get_run(instance_id))
        return runs

    def detail(self, run_id: str) -> dict[str, Any]:
        state = self.client.get_orchestration_state(run_id)
        if state is None:
            raise ValueError("Unknown scheduler instance")
        result = (
            json.loads(state.serialized_output)
            if state.runtime_status.name.lower() == "completed" and state.serialized_output
            else None
        )
        orchestration_elapsed = elapsed_between(
            scheduler_timestamp(state.created_at),
            scheduler_timestamp(state.last_updated_at)
            if state.runtime_status.name.lower()
            in {"completed", "failed", "terminated", "canceled"}
            else None,
        )
        if isinstance(result, dict) and isinstance(result.get("report"), dict):
            report = dict(result["report"])
            payload = json.loads(state.serialized_input or "{}")
            report["artifact_run_id"] = payload.get("artifact_run_id", run_id)
            report["id"] = run_id
            report["parent_run_id"] = payload.get("parent_run_id")
            report["orchestration_elapsed_seconds"] = orchestration_elapsed
            total_elapsed = max(report["elapsed_seconds"], orchestration_elapsed)
            report["end_to_end_elapsed_seconds"] = total_elapsed
            report["end_to_end_completed_records_per_minute"] = (
                report["succeeded"] * 60 / total_elapsed if total_elapsed else 0
            )
            return report
        jobs = []
        processor = (
            state
            if state.name == "recovery_workflow"
            else self.client.get_orchestration_state(f"{run_id}-processor")
        )
        if processor and processor.serialized_custom_status:
            progress = json.loads(processor.serialized_custom_status)
            for part in progress.get("parts", []):
                child = self.client.get_orchestration_state(f"{run_id}-{part['id']}")
                custom = json.loads(child.serialized_custom_status or "{}") if child else {}
                jobs.append(
                    {
                        "id": part["id"],
                        "status": custom.get("status", "waiting"),
                        "created_at": scheduler_timestamp(child.created_at) if child else None,
                        "estimated_tokens": part["tokens"],
                        "request_counts": custom.get("request_counts"),
                    }
                )
        report = summarize_documents(self.get_run(run_id), [], [], jobs)
        for key in (
            "succeeded",
            "failed",
            "pending",
            "completed_records_per_minute",
            "http_attempts",
            "errors_429",
            "batch_record_429",
            "control_plane_429",
            "retry_wait_seconds",
        ):
            report[key] = None
        report["metrics_availability"] = "awaiting_final_reconciliation"
        report["orchestration_elapsed_seconds"] = orchestration_elapsed
        return report

    def request_cancel(self, run_id: str) -> str:
        run = self.get_run(run_id)
        return self.client.schedule_new_orchestration(
            "cancel_workflow",
            input={"run_id": run["artifact_run_id"]},
            instance_id=f"cancel-{run_id}-{uuid.uuid4().hex[:8]}",
            version=WORKFLOW_VERSION,
        )

    def items(self, table: str, run_id: str) -> list[dict[str, Any]]:
        detail = self.detail(run_id)
        if table == "jobs":
            return list(detail.get("jobs", []))
        if table == "events":
            return list(detail.get("safe_events", []))
        if table == "records":
            return list(detail.get("safe_failed_records", []))
        return []

    def retry_failed(self, parent_run_id: str) -> str:
        report = self.detail(parent_run_id)
        count = report.get("retryable_failed_records_count", 0)
        if not count:
            raise ValueError(
                "No confirmed retryable failures; unresolved and permanent failures need review"
            )
        run_id = uuid.uuid4().hex
        self.registry.save_run(
            {
                "id": run_id,
                "mode": "batch",
                "status": "submission_pending",
                "source": "live-scheduler",
                "record_count": count,
                "started_at": utc_now(),
                "ended_at": None,
                "metadata": self.settings.public_metadata(),
                "parent_run_id": parent_run_id,
            }
        )
        self.client.schedule_new_orchestration(
            "retry_workflow",
            input={
                "parent_run_id": parent_run_id,
                "parent_artifact_run_id": report.get("artifact_run_id", parent_run_id),
                "count": count,
            },
            instance_id=run_id,
            version=WORKFLOW_VERSION,
        )
        return run_id

    def recover(self, parent_run_id: str) -> str:
        parent = self.get_run(parent_run_id)
        if parent["runtime_status"] not in {"failed", "terminated", "canceled"}:
            raise ValueError("Recovery requires an investigated, terminal orchestration failure")
        run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"llm-batch-starter:recovery:{parent_run_id}").hex
        if self.client.get_orchestration_state(run_id) is not None:
            return run_id
        self.registry.save_run(
            {
                "id": run_id,
                "mode": "batch",
                "status": "submission_pending",
                "source": "live-scheduler",
                "record_count": parent["record_count"],
                "started_at": utc_now(),
                "ended_at": None,
                "parent_run_id": parent_run_id,
                "artifact_run_id": parent["artifact_run_id"],
                "metadata": parent["metadata"],
            }
        )
        self.client.schedule_new_orchestration(
            "recovery_workflow",
            input={
                "parent_run_id": parent_run_id,
                "artifact_run_id": parent["artifact_run_id"],
                "count": parent["record_count"],
            },
            instance_id=run_id,
            version=WORKFLOW_VERSION,
        )
        return run_id
