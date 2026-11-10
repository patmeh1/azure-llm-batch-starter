from __future__ import annotations

import copy
import json
import math
import random
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .complex_workload import V2_PROMPT_HASH, configure, generate_complex
from .config import Settings
from .prepare import estimate_cost, estimate_input_tokens
from .telemetry import utc_now

CONTROL = {"control": {"deployment": "gpt5-mini-online", "tpm": 150000, "rpm": 150}}
POOL = {
    f"pool-{index}": {"deployment": f"gpt5-mini-v4-{index}", "tpm": 50000, "rpm": 50}
    for index in range(1, 4)
}
ARMS = ("single", "pool")


class MultiDeploymentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int = Field(default=65, ge=5, le=1700)
    seed: int = 42
    workers: Literal[5] = 5
    max_attempts: Literal[4] = 4
    cooldown_seconds: int = Field(default=65, ge=60, le=3600)
    stage_timeout_seconds: int = Field(default=7200, ge=300, le=43200)
    max_estimated_inference_usd: float = Field(default=10, gt=0, le=200, allow_inf_nan=False)

    @model_validator(mode="after")
    def equal_groups(self) -> MultiDeploymentSpec:
        if self.count % self.workers:
            raise ValueError("V4 uses five equal sequential-per-worker groups")
        return self


def v4_settings(base: Settings) -> tuple[Settings, dict[str, Any]]:
    settings = Settings.model_validate(
        {
            **base.model_dump(),
            "model_name": "gpt-5-mini",
            "model_version": "2025-08-07",
            "online_deployment": str(CONTROL["control"]["deployment"]),
            "reasoning_effort": "low",
            "concurrency": 5,
            "request_timeout_seconds": 90,
            "input_price_per_million": 0.275,
            "cached_input_price_per_million": 0.0275,
            "output_price_per_million": 2.2,
        }
    )
    if settings.region != "southcentralus":
        raise ValueError(
            "The fixed v4 experiment requires the approved South Central US configuration"
        )
    settings, profile = configure(settings)
    if profile["prompt_bundle_sha256"] != V2_PROMPT_HASH:
        raise ValueError("V4 must preserve the measured complex-v2 prompt/schema bundle")
    return settings, {
        **profile,
        "experiment": "v4-matched-quota",
        "batch_api_used": False,
        "active_aggregate_tpm_per_arm": 150000,
        "active_aggregate_rpm_per_arm": 150,
        "routing": "round_robin_eligible_targets_with_shared_per_target_cooldown",
        "scale_up_state": "not_observable_from_http_429",
    }


def generation_metadata(settings: Settings) -> dict[str, Any]:
    return {
        key: value
        for key, value in settings.public_metadata().items()
        if key
        not in {
            "rpm",
            "tpm",
            "observe_service_throttling",
            "observation_profile",
            "max_run_cost_usd",
            "batch_discount",
        }
    }


def targets(arm: str) -> dict[str, dict[str, Any]]:
    if arm not in ARMS:
        raise ValueError("Unknown v4 experiment arm")
    return copy.deepcopy(CONTROL if arm == "single" else POOL)


def public_targets(arm: str) -> list[dict[str, Any]]:
    return [
        {"alias": name, "allocated_tpm": value["tpm"], "allocated_rpm": value["rpm"]}
        for name, value in targets(arm).items()
    ]


def plan_multi(spec: MultiDeploymentSpec, base: Settings) -> dict[str, Any]:
    settings, profile = v4_settings(base)
    records = generate_complex(spec.count, spec.seed)
    estimate = estimate_cost(records, settings, batch=False)
    ceiling = estimate["estimated_inference_usd"] * spec.max_attempts * len(ARMS)
    if ceiling > spec.max_estimated_inference_usd:
        raise ValueError("V4 conservative attempt estimate exceeds the scenario model-usage budget")
    token_counts = [estimate_input_tokens(record, settings) for record in records]
    if max(token_counts) + settings.max_output_tokens > 50000:
        raise ValueError("One request exceeds the smallest target's conservative token allowance")
    return {
        "status": "planned_not_executed",
        "specification": spec.model_dump(),
        "workload_profile": profile,
        "generation": generation_metadata(settings),
        "estimated_input_token_range": [min(token_counts), max(token_counts)],
        "conservative_all_attempts_estimate_usd": ceiling,
        "arms": [{"name": arm, "targets": public_targets(arm)} for arm in ARMS],
        "retry_policy": "4 total attempts across all targets; honor Retry-After; 30-second exponential fallback plus jitter",
        "cost_note": "Conservative token-usage estimate, not an invoice or an infrastructure budget cap.",
    }


def retry_seconds(headers: dict[str, str], attempt: int, now: datetime | None = None) -> float:
    normalized = {key.lower(): str(value) for key, value in headers.items()}
    for key, divisor in (("retry-after-ms", 1000), ("retry-after", 1)):
        if key not in normalized:
            continue
        try:
            delay = float(normalized[key]) / divisor
        except ValueError:
            if key != "retry-after":
                continue
            try:
                date = parsedate_to_datetime(normalized[key])
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                delay = (date - (now or datetime.now(UTC))).total_seconds()
            except (ValueError, TypeError, OverflowError):
                continue
        if math.isfinite(delay) and delay >= 0:
            return delay
    return min(120, 30 * 2 ** (attempt - 1)) + random.uniform(0, 3)


def error_indication(body: Any) -> str:
    text = json.dumps(body, default=str).lower()
    if any(fragment in text for fragment in ("high demand", "temporarily unable", "capacity")):
        return "capacity_indication"
    if any(
        fragment in text for fragment in ("rate limit", "rate_limit", "token rate", "requests to")
    ):
        return "rate_limit_indication"
    return "unspecified"


def update_router(
    state: dict[str, Any], operation: str, value: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    state = copy.deepcopy(state)
    owner = value["owner"]
    if operation == "acquire":
        if state and state.get("owner") != owner:
            return state, {"acquired": False}
        return state or {"owner": owner}, {"acquired": True}
    if state.get("owner") != owner:
        raise ValueError("V4 router is owned by another or no experiment")
    if operation == "release":
        return {}, {"released": True}
    if operation == "begin":
        spec = MultiDeploymentSpec.model_validate(value["specification"])
        arm = value["arm"]
        if state.get("arm") == arm:
            return state, {"ready": True}
        if arm == "single" and state.get("arm"):
            raise ValueError("Cannot restart an earlier v4 stage and reset budgets")
        if arm == "pool" and state.get("arm") != "single":
            raise ValueError("The pool arm must follow the single-deployment control")
        state = {
            "owner": owner,
            "arm": arm,
            "pointer": 0,
            "cooldown": {alias: value["now"] for alias in targets(arm)},
            "assignments": {},
            "attempts": 0,
            "reserved_usd": 0.0,
            "attempt_limit": spec.count * spec.max_attempts,
            "cost_limit": spec.max_estimated_inference_usd / 2,
            "deadline": (
                datetime.fromisoformat(value["now"]) + timedelta(seconds=spec.stage_timeout_seconds)
            ).isoformat(),
        }
        return state, {"ready": True}
    if value["arm"] != state.get("arm"):
        raise ValueError("V4 router arm differs from the caller")
    if operation == "cooldown":
        assignment = state["assignments"].get(value["key"])
        if assignment is None:
            raise ValueError("Cannot cool down an unreserved target")
        until = value["until"]
        if datetime.fromisoformat(until) > datetime.fromisoformat(state["cooldown"][assignment]):
            state["cooldown"][assignment] = until
        return state, {"updated": True}
    if operation != "route":
        raise ValueError("Unsupported v4 router operation")
    key = value["key"]
    if key in state["assignments"]:
        return state, {"target": state["assignments"][key]}
    now = datetime.fromisoformat(value["now"])
    if now >= datetime.fromisoformat(state["deadline"]):
        return state, {"terminal_error": "admission_deadline"}
    cost = value["estimated_cost_usd"]
    if (
        isinstance(cost, bool)
        or not isinstance(cost, (int, float))
        or not math.isfinite(cost)
        or cost < 0
    ):
        raise ValueError("Invalid v4 attempt reservation")
    if (
        state["attempts"] >= state["attempt_limit"]
        or state["reserved_usd"] + cost > state["cost_limit"]
    ):
        return state, {"terminal_error": "attempt_or_cost_budget_exhausted"}
    aliases = list(state["cooldown"])
    for offset in range(len(aliases)):
        index = (state["pointer"] + offset) % len(aliases)
        alias = aliases[index]
        if datetime.fromisoformat(state["cooldown"][alias]) <= now:
            state["pointer"] = (index + 1) % len(aliases)
            state["attempts"] += 1
            state["reserved_usd"] += cost
            state["assignments"][key] = alias
            return state, {"target": alias}
    earliest = min(datetime.fromisoformat(value) for value in state["cooldown"].values())
    return state, {
        "wait_until": min(earliest, datetime.fromisoformat(state["deadline"])).isoformat()
    }


def export_multi(report_path: Path, destination: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("workload_profile", {}).get("experiment") != "v4-matched-quota":
        raise ValueError("Export requires an actual v4 operational report")
    keys = (
        "status",
        "started_at",
        "ended_at",
        "record_count",
        "succeeded",
        "failed",
        "pending",
        "elapsed_seconds",
        "completed_records_per_minute",
        "http_attempts",
        "errors_429",
        "errors_by_code",
        "router_wait_seconds",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "usage_reports",
        "usage_complete",
        "successful_record_elapsed_mean_seconds",
        "all_record_elapsed_mean_seconds",
        "http_200_latency_p50_seconds",
        "http_200_latency_p95_seconds",
        "observed_rate_limits",
        "priced_usage_estimate_usd",
        "quality",
        "semantic_status",
        "gate",
        "target_metrics",
    )
    stages = report.get("multi_deployment_stages", [])
    final = len(stages) == 2 and all(stage.get("pending") == 0 for stage in stages)
    if final and sum(stage["record_count"] for stage in stages) != report["record_count"]:
        raise ValueError("V4 export record counts do not reconcile")
    value = {
        "observed_at": utc_now(),
        "application_version": "0.4.0",
        "results_final": final,
        "execution_status": "completed" if final else report["status"],
        "processing_status": report["status"],
        "acceptance_status": report.get("acceptance_status"),
        "router_reconciliation_required": report.get("router_reconciliation_required"),
        "started_at": report.get("started_at"),
        "ended_at": report.get("ended_at"),
        "specification": report.get("specification"),
        "workload_profile": report["workload_profile"],
        "model": report["metadata"],
        "active_aggregate_tpm_per_arm": 150000,
        "active_aggregate_rpm_per_arm": 150,
        "allocated_deployments_total": 4,
        "conservative_all_attempts_estimate_usd": report.get(
            "conservative_all_attempts_estimate_usd"
        ),
        "end_to_end_elapsed_seconds": report.get(
            "end_to_end_elapsed_seconds", report.get("elapsed_seconds")
        ),
        "reported_model_usage_estimate_usd": report.get("priced_usage_estimate_usd"),
        "cost_note": "Configured retail rates applied to reported usage, not an invoice; infrastructure is excluded.",
        "stages": [
            {"arm": stage["arm"], **{key: stage.get(key) for key in keys}} for stage in stages
        ],
        "limitations": [
            "One matched-quota pilot, not a sustained-capacity guarantee or proof of scale-up behavior.",
            "429 symptoms do not uniquely identify token, request, or backend capacity causes.",
            "The single and pool arms run sequentially; cache and service conditions can differ.",
            "Three deployments divide quota; they do not multiply the subscription allocation.",
            "Unchanged synthetic prompts are not a production workload or a semantic correctness guarantee.",
        ],
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    if final:
        samples = {
            "results_final": True,
            "scope": "First three synthetic record outcomes per arm, including failures; not the full dataset.",
            "model": "gpt-5-mini",
            "model_version": "2025-08-07",
            "arms": report["synthetic_output_samples"],
        }
        (destination / "output-samples.json").write_text(
            json.dumps(samples, indent=2) + "\n", encoding="utf-8"
        )
    return value
