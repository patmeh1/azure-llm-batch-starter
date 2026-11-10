from __future__ import annotations

import copy
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from pydantic import Field

from .complex_workload import generate_complex
from .config import Settings
from .multi_deployment import MultiDeploymentSpec, plan_multi, v4_settings
from .prepare import estimate_cost
from .synthetic import Record
from .telemetry import utc_now

ADAPTIVE_ARMS = ("reactive", "adaptive")
WARMUP_COUNT = 5
ADAPTIVE_STAGES = tuple(stage for arm in ADAPTIVE_ARMS for stage in (f"{arm}-warmup", arm))


class ModelTarget(TypedDict):
    deployment: str
    tpm: int
    rpm: int


ADAPTIVE_TARGETS: dict[str, ModelTarget] = {
    "target-1": {"deployment": "gpt5-mini-online", "tpm": 100000, "rpm": 100},
    **{
        f"target-{index + 1}": {
            "deployment": f"gpt5-mini-v4-{index}",
            "tpm": 50000,
            "rpm": 50,
        }
        for index in range(1, 4)
    },
    "target-5": {"deployment": "gpt5-mini-v5-5", "tpm": 50000, "rpm": 50},
}


class AdaptiveSpec(MultiDeploymentSpec):
    count: int = Field(default=520, ge=5, le=520)
    max_estimated_inference_usd: float = Field(default=75, gt=0, le=200, allow_inf_nan=False)
    headroom: float = Field(default=0.95, ge=0.5, le=1, allow_inf_nan=False)


def adaptive_settings(base: Settings) -> tuple[Settings, dict[str, Any]]:
    settings, profile = v4_settings(base)
    return settings, {
        **profile,
        "experiment": "v5-adaptive",
        "active_aggregate_tpm_per_arm": 300000,
        "active_aggregate_rpm_per_arm": 300,
        "routing": "shared_queue_reactive_vs_quota_aware",
        "warmup_records_per_arm": WARMUP_COUNT,
    }


def warmup_records(spec: MultiDeploymentSpec) -> list[Record]:
    return [
        record.model_copy(update={"id": f"warmup-{record.id}"})
        for record in generate_complex(WARMUP_COUNT, spec.seed + 1)
    ]


def adaptive_plan(spec: AdaptiveSpec, base: Settings) -> dict[str, Any]:
    settings, profile = adaptive_settings(base)
    result = plan_multi(spec, base)
    warmup_ceiling = (
        estimate_cost(warmup_records(spec), settings, batch=False)["estimated_inference_usd"]
        * spec.max_attempts
    )
    ceiling = result["conservative_all_attempts_estimate_usd"] + 2 * warmup_ceiling
    if ceiling > spec.max_estimated_inference_usd:
        raise ValueError("V5 benchmark plus warm-ups exceed the approved model-usage guard")
    if (
        result["estimated_input_token_range"][1] + settings.max_output_tokens
        > 50000 * spec.headroom
    ):
        raise ValueError("A request cannot fit the smallest target with the chosen headroom")
    result.update(
        specification=spec.model_dump(),
        workload_profile=profile,
        arms=[{"name": arm, "targets": adaptive_public_targets()} for arm in ADAPTIVE_ARMS],
        conservative_all_attempts_estimate_usd=ceiling,
        warmup_cost_ceiling_per_arm=warmup_ceiling,
        expected_benchmark_records=spec.count * 2,
        expected_warmup_records=WARMUP_COUNT * 2,
        max_http_attempts=(spec.count + WARMUP_COUNT) * spec.max_attempts * 2,
        averages_note="Measured HTTP latency, ready-to-terminal record time, and queued-to-terminal lifetime are separate; job time/count is throughput, not latency.",
    )
    return result


def adaptive_public_targets() -> list[dict[str, Any]]:
    return [
        {"alias": alias, "allocated_tpm": value["tpm"], "allocated_rpm": value["rpm"]}
        for alias, value in ADAPTIVE_TARGETS.items()
    ]


def _stamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Admission timestamps must be timezone aware")
    return parsed.timestamp()


def _iso(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat()


def _finite(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Admission reservations must be finite numbers")
    result = float(value)
    if not math.isfinite(result) or result < 0 or positive and result == 0:
        raise ValueError("Admission reservations must be finite nonnegative numbers")
    return result


def _refill(target: dict[str, Any], now: float, margin: float) -> None:
    if target["limit_until"] and now >= target["limit_until"]:
        until = target["limit_until"]
        before = max(0, until - target["updated"])
        target["tokens"] = min(
            target["token_limit"] * margin,
            target["tokens"] + before * target["token_limit"] * margin / 60,
        )
        target["requests"] = min(
            max(1, target["request_limit"] * margin),
            target["requests"] + before * target["request_limit"] * margin / 60,
        )
        target["updated"] = max(target["updated"], until)
        target["token_limit"] = target["configured_tpm"]
        target["request_limit"] = target["configured_rpm"]
        target["limit_until"] = 0
    elapsed = max(0, now - target["updated"])
    target["tokens"] = min(
        target["token_limit"] * margin,
        target["tokens"] + elapsed * target["token_limit"] * margin / 60,
    )
    target["requests"] = min(
        max(1, target["request_limit"] * margin),
        target["requests"] + elapsed * target["request_limit"] * margin / 60,
    )
    target["updated"] = now


def update_admission(
    state: dict[str, Any], operation: str, value: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Deterministic durable entity: queue, one in-flight request per target, and budgets."""
    state = copy.deepcopy(state)
    now = _stamp(value["now"])
    owner = value["owner"]
    if operation == "begin":
        spec = AdaptiveSpec.model_validate(value["specification"])
        if state:
            if state["owner"] != owner or state["stage"] != value["stage"]:
                raise ValueError("Admission state belongs to another experiment/stage")
            return state, {"ready": True}
        stage = value["stage"]
        if stage not in ADAPTIVE_STAGES:
            raise ValueError("Unsupported V5 stage")
        warming = stage.endswith("-warmup")
        indexes = (
            list(range(spec.count, spec.count + WARMUP_COUNT))
            if warming
            else list(range(spec.count))
        )
        tokens = [_finite(n, positive=True) for n in value["request_tokens"]]
        costs = [_finite(n) for n in value["request_costs"]]
        if len(tokens) != spec.count + WARMUP_COUNT or len(costs) != len(tokens):
            raise ValueError("Admission records do not match the prepared manifest")
        warmup_budget = sum(costs[spec.count :]) * spec.max_attempts
        state = {
            "owner": owner,
            "stage": stage,
            "warming": warming,
            "paced": stage.startswith("adaptive"),
            "specification": spec.model_dump(),
            "queue": [[index, 1, None] for index in indexes],
            "initial_ready": {},
            "waits": {},
            "grants": {},
            "request_tokens": tokens,
            "request_costs": costs,
            "pointer": 0,
            "completed": 0,
            "expected": len(indexes),
            "attempts": 0,
            "reserved_usd": 0.0,
            "attempt_limit": len(indexes) * spec.max_attempts,
            "cost_limit": warmup_budget
            if warming
            else spec.max_estimated_inference_usd / 2 - warmup_budget,
            "deadline": now + (300 if warming else spec.stage_timeout_seconds),
            "invalid_budget_headers": 0,
            "feedback_updates": 0,
            "targets": {
                alias: {
                    "token_limit": target["tpm"],
                    "request_limit": target["rpm"],
                    "configured_tpm": target["tpm"],
                    "configured_rpm": target["rpm"],
                    "limit_until": 0.0,
                    "tokens": target["tpm"] * spec.headroom,
                    "requests": target["rpm"] * spec.headroom,
                    "updated": now,
                    "cooldown": now,
                    "next_request": now,
                    "busy": False,
                    "latency": 0.0,
                    "last_used": now,
                }
                for alias, target in ADAPTIVE_TARGETS.items()
            },
        }
        return state, {"ready": True}
    if state.get("owner") != owner:
        raise ValueError("Admission entity is not owned by this experiment")
    spec = AdaptiveSpec.model_validate(state["specification"])
    if operation == "snapshot":
        return state, {
            key: state[key]
            for key in (
                "stage",
                "attempts",
                "reserved_usd",
                "completed",
                "expected",
                "invalid_budget_headers",
                "feedback_updates",
            )
        }
    key = value["key"]
    if operation == "ack":
        grant = state["grants"].get(key)
        if grant is None:
            raise ValueError("Cannot acknowledge an unreserved request")
        if grant["acked"]:
            return state, {"acknowledged": True}
        response = value["response"]
        if not isinstance(response.get("terminal"), bool):
            raise ValueError("Missing terminal outcome in admission acknowledgement")
        alias = grant.get("target")
        if alias:
            target = state["targets"][alias]
            _refill(target, now, spec.headroom)
            target["busy"] = False
            headers = response.get("budget_headers", {})
            for kind, header_kind, maximum in (
                ("tokens", "tokens", ADAPTIVE_TARGETS[alias]["tpm"]),
                ("requests", "requests", ADAPTIVE_TARGETS[alias]["rpm"]),
            ):
                limit_key = "token_limit" if kind == "tokens" else "request_limit"
                for prefix in ("limit", "remaining"):
                    raw = headers.get(f"x-ratelimit-{prefix}-{header_kind}")
                    if raw is None:
                        continue
                    try:
                        measured = float(raw)
                        if (
                            not math.isfinite(measured)
                            or measured < 0
                            or prefix == "limit"
                            and measured == 0
                        ):
                            raise ValueError("Invalid budget header")
                    except (ValueError, TypeError):
                        state["invalid_budget_headers"] += 1
                        continue
                    if prefix == "limit":
                        target[limit_key] = min(maximum, measured)
                        target["limit_until"] = now + 60
                    else:
                        # One in-flight call per target avoids out-of-order budget feedback.
                        target[kind] = min(target[limit_key], measured) * spec.headroom
                        state["feedback_updates"] += 1
                capacity = target[limit_key] * spec.headroom
                target[kind] = min(
                    target[kind], max(1, capacity) if kind == "requests" else capacity
                )
            if response.get("retryable"):
                end = _stamp(response["completed_at"])
                target["cooldown"] = max(target["cooldown"], end + _finite(response["retry_after"]))
            if (
                response.get("status_code") == 200
                and response.get("http_duration_seconds") is not None
            ):
                latency = _finite(response["http_duration_seconds"])
                target["latency"] = (
                    latency if not target["latency"] else 0.8 * target["latency"] + 0.2 * latency
                )
        grant["acked"] = True
        if response["terminal"]:
            state["completed"] += 1
        else:
            if grant.get("terminal_error") or grant["attempt"] >= spec.max_attempts:
                raise ValueError("A terminal/budget-limited grant cannot be retried")
            state["queue"].insert(0, [grant["index"], grant["attempt"] + 1, now])
        return state, {"acknowledged": True}
    if operation != "claim":
        raise ValueError("Unknown admission operation")
    if key in state["grants"]:
        return state, {k: v for k, v in state["grants"][key].items() if k != "acked"}
    if not state["queue"]:
        return state, {"done": True}
    index, attempt, ready = state["queue"][0]
    if ready is None:
        ready = now
        state["queue"][0][2] = ready
    state["initial_ready"].setdefault(str(index), ready)
    error = (
        "cancelled_before_attempt"
        if value.get("cancelled")
        else "admission_deadline"
        if now >= state["deadline"]
        else "attempt_or_cost_budget_exhausted"
        if (
            state["attempts"] >= state["attempt_limit"]
            or state["reserved_usd"] + state["request_costs"][index] > state["cost_limit"] + 1e-9
        )
        else None
    )
    aliases = list(ADAPTIVE_TARGETS)
    available, future = [], []
    if not error:
        for offset in range(len(aliases)):
            position = (state["pointer"] + offset) % len(aliases)
            alias = aliases[position]
            if state["warming"] and position != index - spec.count:
                continue
            target = state["targets"][alias]
            _refill(target, now, spec.headroom)
            if target["busy"]:
                future.append(now + 5)
                continue
            when = max(now, target["cooldown"])
            if state["paced"]:
                when = max(
                    when,
                    target["next_request"],
                    now
                    + max(0, state["request_tokens"][index] - target["tokens"])
                    * 60
                    / (target["token_limit"] * spec.headroom),
                    now
                    + max(0, 1 - target["requests"])
                    * 60
                    / (target["request_limit"] * spec.headroom),
                )
                if state["request_tokens"][index] > target["token_limit"] * spec.headroom:
                    when = max(when, target["limit_until"] or now + 60)
            if when <= now + 1e-6:
                available.append((target["latency"], -target["tokens"], offset, position, alias))
            else:
                future.append(when)
        if not available:
            return state, {"wait_until": _iso(min([state["deadline"], now + 30, *future]))}
    state["queue"].pop(0)
    state["waits"][str(index)] = state["waits"].get(str(index), 0.0) + max(0, now - ready)
    grant = {
        "index": index,
        "attempt": attempt,
        "ready_at": _iso(state["initial_ready"][str(index)]),
        "router_wait_seconds": state["waits"][str(index)],
    }
    if error:
        grant["terminal_error"] = error
    else:
        selected = min(available) if state["paced"] else min(available, key=lambda row: row[2])
        _, _, _, position, alias = selected
        target = state["targets"][alias]
        state["pointer"] = (position + 1) % len(aliases)
        target.update(
            busy=True,
            last_used=now,
            next_request=now + 60 / (target["request_limit"] * spec.headroom),
            tokens=max(0, target["tokens"] - state["request_tokens"][index]),
            requests=max(0, target["requests"] - 1),
        )
        state["attempts"] += 1
        state["reserved_usd"] += state["request_costs"][index]
        grant["target"] = alias
    state["grants"][key] = {**grant, "acked": False}
    return state, grant


PUBLIC_ADAPTIVE_METRICS = (
    "arm",
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
    "http_attempted_record_count",
    "http_200_latency_mean_seconds",
    "http_200_latency_p50_seconds",
    "http_200_latency_p95_seconds",
    "http_200_latency_measured_count",
    "http_attempt_latency_mean_seconds",
    "http_attempt_latency_measured_count",
    "successful_record_elapsed_mean_seconds",
    "successful_record_elapsed_measured_count",
    "all_record_elapsed_mean_seconds",
    "record_elapsed_measured_count",
    "record_elapsed_p95_seconds",
    "backlog_wait_mean_seconds",
    "queue_to_completion_mean_seconds",
    "queue_to_completion_p95_seconds",
    "queue_to_completion_measured_count",
    "priced_usage_estimate_usd",
    "quality",
    "semantic_status",
    "gate",
    "target_metrics",
    "admission",
)


def export_adaptive(report_path: Path, destination: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("workload_profile", {}).get("experiment") != "v5-adaptive":
        raise ValueError("Expected an actual V5 operational report")
    stages = report.get("multi_deployment_stages", [])
    final = len(stages) == 2 and all(stage.get("pending") == 0 for stage in stages)
    if final and sum(stage["record_count"] for stage in stages) != report["record_count"]:
        raise ValueError("V5 stage counts do not reconcile")
    result = {
        "application_version": "0.5.0",
        "observed_at": utc_now(),
        "results_final": final,
        "execution_status": "completed" if final else report["status"],
        "processing_status": report["status"],
        "acceptance_status": report.get("acceptance_status"),
        "started_at": report.get("started_at"),
        "ended_at": report.get("ended_at"),
        "model": report["metadata"],
        "workload_profile": report["workload_profile"],
        "specification": report.get("specification"),
        "active_aggregate_tpm_per_arm": 300000,
        "active_aggregate_rpm_per_arm": 300,
        "allocated_deployments_total": 5,
        "conservative_all_attempts_estimate_usd": report.get(
            "conservative_all_attempts_estimate_usd"
        ),
        "end_to_end_elapsed_seconds": report.get("end_to_end_elapsed_seconds"),
        "reported_model_usage_estimate_usd": report.get(
            "total_usage_estimate_including_warmups_usd"
        ),
        "warmup_usage_estimate_usd": report.get("warmup_usage_estimate_usd"),
        "router_reconciliation_required": report.get("router_reconciliation_required"),
        "topology_snapshot": report.get("topology_snapshot"),
        "benchmark_valid": report.get("benchmark_valid"),
        "stages": [{key: stage.get(key) for key in PUBLIC_ADAPTIVE_METRICS} for stage in stages],
        "warmups": [
            {key: stage.get(key) for key in PUBLIC_ADAPTIVE_METRICS}
            for stage in report.get("warmup_stages", [])
        ],
        "cost_note": "Reported token usage including warm-ups at configured rates, not an invoice; infrastructure and unreported usage are excluded.",
        "latency_note": "HTTP means measure attempts. Record processing starts when a record first becomes ready at the queue head; queue-to-completion starts at arm enqueue. Means are not job elapsed/count.",
        "limitations": [
            "One paired experiment; no sustained throughput or zero-429 guarantee.",
            "Both arms share the same 300K allocation; historical V4 used 150K per arm.",
            "Warm-ups, service conditions and cache warmth do not establish randomized experimental equivalence.",
            "Unchanged gold checks remain independent of successful processing.",
        ],
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "summary.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
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
    return result
