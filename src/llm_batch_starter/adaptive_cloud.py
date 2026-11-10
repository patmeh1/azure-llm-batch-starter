from __future__ import annotations

import json
import threading
from typing import Any

from .adaptive import (
    ADAPTIVE_ARMS,
    ADAPTIVE_STAGES,
    ADAPTIVE_TARGETS,
    WARMUP_COUNT,
    AdaptiveSpec,
    adaptive_plan,
    adaptive_public_targets,
    adaptive_settings,
    warmup_records,
)
from .config import Settings
from .multi_cloud import MultiActivities
from .multi_deployment import MultiDeploymentSpec
from .provider import AzureProvider
from .synthetic import Record
from .telemetry import canonical_json, elapsed_between, utc_now
from .topology import verify_topology


class AdaptiveActivities(MultiActivities):
    spec_model = AdaptiveSpec
    arms = ADAPTIVE_STAGES
    comparison_arms = ADAPTIVE_ARMS
    experiment_mode = "adaptive-pool"
    manifest_name = "v5-manifest.json"

    def __init__(
        self,
        settings: Settings | None = None,
        store: Any = None,
        provider_factory: Any = AzureProvider,
        topology_check: Any = verify_topology,
    ):
        super().__init__(settings, store, provider_factory)
        self.topology_check = topology_check
        self._lock = threading.RLock()
        self._providers: dict[str, Any] = {}
        self._starts: dict[str, str] = {}

    def configure_settings(self, settings: Settings) -> tuple[Settings, dict[str, Any]]:
        return adaptive_settings(settings)

    def make_plan(self, spec: MultiDeploymentSpec) -> dict[str, Any]:
        return adaptive_plan(AdaptiveSpec.model_validate(spec.model_dump()), self.settings)

    def generate_records(self, spec: MultiDeploymentSpec) -> list[Record]:
        return [*super().generate_records(spec), *warmup_records(spec)]

    def arm_count(self, spec: MultiDeploymentSpec, arm: str) -> int:
        return WARMUP_COUNT if arm.endswith("-warmup") else spec.count

    def arm_timeout(self, spec: MultiDeploymentSpec, arm: str) -> int:
        return 300 if arm.endswith("-warmup") else spec.stage_timeout_seconds

    def target_map(self, arm: str) -> dict[str, dict[str, Any]]:
        return {alias: dict(target) for alias, target in ADAPTIVE_TARGETS.items()}

    def public_target_map(self, arm: str) -> list[dict[str, Any]]:
        return adaptive_public_targets()

    def manifest_extra(self, options: dict[str, Any]) -> dict[str, Any]:
        settings = Settings.model_validate(
            {
                **self.settings.model_dump(),
                "model_resource_id": options["model_resource_id"],
            }
        )
        return {
            "model_resource_id": settings.model_resource_id,
            "topology_snapshot": self.topology_check(settings, ADAPTIVE_TARGETS),
        }

    def _manifest(self, root: str) -> dict[str, Any]:
        with self._lock:
            value = super()._manifest(root)
            # Manifests are immutable; bound process-local caching across experiments.
            if len(self.manifests) > 4:
                oldest = next(key for key in self.manifests if key != root)
                del self.manifests[oldest]
                self._starts = {
                    key: value
                    for key, value in self._starts.items()
                    if not key.startswith(oldest + "-")
                }
            return value

    def begin_arm(self, root: str, arm: str) -> dict[str, Any]:
        manifest = self._manifest(root)
        settings = Settings.model_validate(
            {
                **self.settings.model_dump(),
                "model_resource_id": manifest["model_resource_id"],
            }
        )
        self.topology_check(settings, ADAPTIVE_TARGETS)
        result = super().begin_arm(root, arm)
        with self._lock:
            self._starts[self._stage_id(root, arm)] = result["started_at"]
        return result

    def provider_for(self, settings: Settings) -> Any:
        with self._lock:
            if settings.online_deployment not in self._providers:
                self._providers[settings.online_deployment] = self.provider_factory(settings)
            return self._providers[settings.online_deployment]

    def release_provider(self, provider: Any) -> None:
        # Cached clients are closed with the owning activity service, not per request.
        return None

    def close(self) -> None:
        with self._lock:
            for provider in self._providers.values():
                provider.close()
            self._providers.clear()
        super().close()

    def timing_fields(self, stage_id: str, start: str, end: str) -> dict[str, Any]:
        with self._lock:
            if stage_id not in self._starts:
                self._starts[stage_id] = self.store.get_run(stage_id)["started_at"]
            queued = self._starts[stage_id]
        completed = utc_now()
        return {
            **super().timing_fields(stage_id, start, completed),
            "response_completed_at": end,
            "queued_at": queued,
            "backlog_wait_seconds": elapsed_between(queued, start),
            "queue_to_completion_seconds": elapsed_between(queued, completed),
        }

    def control(self, root: str) -> dict[str, Any]:
        return {"cancelled": bool(self.store.get_item("control", root, "cancel"))}

    def cancel(self, root: str) -> dict[str, Any]:
        self.store.save(
            "control",
            root,
            "cancel",
            {
                "requested": True,
                "reason": "warmup_processing_failed",
            },
        )
        return {"cancelled": True}

    def save_admission(self, root: str, arm: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        self.store.put_blob(
            f"{self._stage_id(root, arm)}/admission.json",
            canonical_json(snapshot).encode(),
            overwrite=True,
        )
        return {"saved": True}

    def finalize_arm(self, root: str, arm: str) -> dict[str, Any]:
        result = super().finalize_arm(root, arm)
        report = result["report"]
        report["admission"] = json.loads(
            self.store.get_blob(f"{self._stage_id(root, arm)}/admission.json")
        )
        self.store.put_blob(
            f"{self._stage_id(root, arm)}/summary.json",
            canonical_json(report).encode(),
            overwrite=True,
        )
        return result

    def final_extra(self, root: str, summary: dict[str, Any]) -> dict[str, Any]:
        warmups = [
            json.loads(self.store.get_blob(f"{self._stage_id(root, arm + '-warmup')}/summary.json"))
            for arm in self.comparison_arms
        ]
        costs = [
            row["priced_usage_estimate_usd"]
            for row in warmups
            if row["priced_usage_estimate_usd"] is not None
        ]
        total_costs = [*costs]
        if summary["priced_usage_estimate_usd"] is not None:
            total_costs.append(summary["priced_usage_estimate_usd"])
        ambiguous = summary["router_reconciliation_required"] or any(
            row.get("error") == "ambiguous_online_result"
            for arm in self.comparison_arms
            for row in self.store.items("records", self._stage_id(root, arm + "-warmup"))
        )
        return {
            "warmup_stages": warmups,
            "warmup_usage_estimate_usd": sum(costs) if costs else None,
            "total_usage_estimate_including_warmups_usd": sum(total_costs) if total_costs else None,
            "router_reconciliation_required": ambiguous,
            "benchmark_valid": all(row["succeeded"] == WARMUP_COUNT for row in warmups),
            "topology_snapshot": self._manifest(root)["topology_snapshot"],
        }
