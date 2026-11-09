from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .complex_workload import PROFILE, generate_complex
from .config import Settings, load_settings
from .pipeline import run_pipeline
from .prepare import estimate_cost, estimate_input_tokens, prepare_requests
from .scheduler import SchedulerAccess
from .synthetic import generate_dataset
from .telemetry import elapsed_between, utc_now
from .workloads import configure_profile


class Stage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9-]+$")
    mode: Literal["sequential", "parallel", "batch"]
    concurrency: int = Field(default=1, ge=1, le=16)


class Scenario(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z0-9-]+$")
    profile: Literal["simple", "complex-v2"] = PROFILE
    count: int = Field(default=13, ge=1, le=10000)
    seed: int = 42
    stages: list[Stage] = Field(min_length=1, max_length=10)
    cooldown_seconds: int = Field(default=65, ge=0, le=3600)
    timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    max_estimated_inference_usd: float = Field(default=5, gt=0, allow_inf_nan=False)
    minimum_primary_accuracy: float = Field(default=0.8, ge=0, le=1, allow_inf_nan=False)
    minimum_confidence_compliance: float = Field(default=1, ge=0, le=1, allow_inf_nan=False)


def plan_scenario(scenario: Scenario, settings: Settings) -> dict[str, Any]:
    if len({stage.name for stage in scenario.stages}) != len(scenario.stages):
        raise ValueError("Load-stage names must be unique")
    configured, profile = configure_profile(settings, scenario.profile)
    if configured.observe_service_throttling:
        raise ValueError(
            "Load matrices are paced; use the bounded stress script for explicit unpaced observations"
        )
    records = (
        generate_complex(scenario.count, scenario.seed)
        if scenario.profile == PROFILE
        else generate_dataset(scenario.count, scenario.seed)
    )
    partitions = prepare_requests(records, configured)
    tokens = [estimate_input_tokens(record, configured) for record in records]
    estimates = []
    for stage in scenario.stages:
        estimate = estimate_cost(records, configured, batch=stage.mode == "batch")
        ceiling = estimate["estimated_inference_usd"] * (
            1 if stage.mode == "batch" else configured.max_attempts
        )
        if stage.mode != "batch" and max(tokens) + configured.max_output_tokens > configured.tpm:
            raise ValueError(
                "One complex request exceeds client TPM including its output allowance"
            )
        if estimate["estimated_inference_usd"] > configured.max_run_cost_usd:
            raise ValueError("A load stage exceeds max_run_cost_usd")
        estimates.append(
            {
                **stage.model_dump(),
                **estimate,
                "attempt_ceiling_estimate_usd": ceiling,
                "transport": "scheduler" if stage.mode == "batch" else "online",
            }
        )
    total = sum(row["attempt_ceiling_estimate_usd"] for row in estimates)
    if total > scenario.max_estimated_inference_usd:
        raise ValueError(
            "Load matrix exceeds its inference estimate budget; reduce the scenario or review a new budget"
        )
    return {
        "status": "planned_not_executed",
        "scenario": scenario.model_dump(),
        "workload_profile": profile,
        "generation": configured.public_metadata(),
        "estimated_input_token_range": [min(tokens), max(tokens)],
        "batch_partitions": len(partitions),
        "stages": estimates,
        "maximum_planned_inference_estimate_usd": total,
        "cost_note": "Conservative token estimate including configured online attempts, not an invoice or infrastructure spending cap.",
    }


def quality_gate(summary: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    if summary["status"] in {"running", "pending", "queued", "submission_pending"}:
        return {"status": "pending", "checks": {}}
    checks = {"all_records_processed": summary["status"] == "succeeded"}
    if scenario.profile == PROFILE:
        quality = summary.get("quality") or {}
        fields = quality.get("field_accuracy", {})
        constraints = quality.get("constraint_compliance", {})
        checks.update(
            all_records_evaluated=quality.get("evaluated_records") == scenario.count,
            primary_accuracy=fields.get("primary_route", 0) >= scenario.minimum_primary_accuracy,
            confidence_compliance=fields.get("confidence", 0)
            >= scenario.minimum_confidence_compliance,
            archived_confidence_preserved=constraints.get("immutable_archived_confidence", 0) == 1,
            no_identifier_canaries=constraints.get("identifier_suppression", 0) == 1,
        )
    return {"status": "passed" if all(checks.values()) else "needs_review", "checks": checks}


def _save(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2), encoding="utf-8")
    temporary.replace(path)


def _finish_matrix(value: dict[str, Any], scenario: Scenario) -> None:
    gates = [row.get("gate", {"status": "pending"})["status"] for row in value["runs"]]
    value["status"] = (
        "pending"
        if len(gates) < len(scenario.stages) or "pending" in gates
        else "passed"
        if all(gate == "passed" for gate in gates)
        else "needs_review"
    )
    if value["status"] != "pending" and value.get("ended_at") is None:
        value["ended_at"] = utc_now()
    value["elapsed_seconds_including_cooldowns"] = elapsed_between(
        value["started_at"], value.get("ended_at")
    )


def execute_scenario(scenario: Scenario, settings: Settings, output: Path) -> dict[str, Any]:
    plan = plan_scenario(scenario, settings)
    configured, profile = configure_profile(settings, scenario.profile)
    records = (
        generate_complex(scenario.count, scenario.seed)
        if scenario.profile == PROFILE
        else generate_dataset(scenario.count, scenario.seed)
    )
    directory = output / f"{scenario.name}-{uuid.uuid4().hex[:12]}"
    directory.mkdir(parents=True, exist_ok=False)
    configured_path = directory / "config.json"
    configured_path.write_text(configured.model_dump_json(indent=2), encoding="utf-8")
    path = directory / "matrix.json"
    result: dict[str, Any] = {
        **plan,
        "status": "running",
        "started_at": utc_now(),
        "ended_at": None,
        "config_path": str(configured_path.resolve()),
        "runs": [],
    }
    _save(path, result)
    finished = False
    try:
        for index, stage in enumerate(scenario.stages):
            if index:
                time.sleep(scenario.cooldown_seconds)
            run_id = uuid.uuid4().hex
            row: dict[str, Any] = {
                **stage.model_dump(),
                "run_id": run_id,
                "status": "starting",
                "transport": "scheduler" if stage.mode == "batch" else "online",
            }
            result["runs"].append(row)
            _save(path, result)
            stage_settings = Settings.model_validate(
                {
                    **configured.model_dump(),
                    "concurrency": stage.concurrency,
                    "run_timeout_seconds": scenario.timeout_seconds,
                    "max_http_attempts": scenario.count * configured.max_attempts,
                }
            )
            if stage.mode == "batch":
                access = SchedulerAccess(stage_settings)
                try:
                    if scenario.profile == PROFILE:
                        access.start_complex(scenario.count, scenario.seed, run_id=run_id)
                    else:
                        access.start_synthetic(scenario.count, scenario.seed, run_id=run_id)
                    row.update(status="pending", transport="scheduler")
                    _save(path, result)
                    deadline = time.monotonic() + scenario.timeout_seconds
                    while True:
                        run = access.get_run(run_id)
                        if run["status"] not in {"queued", "running", "pending"}:
                            summary = access.detail(run_id)
                            break
                        if time.monotonic() >= deadline:
                            summary = access.detail(run_id)
                            break
                        time.sleep(min(30, max(0, deadline - time.monotonic())))
                finally:
                    access.close()
            else:
                summary = run_pipeline(
                    records,
                    stage_settings,
                    stage.mode,
                    Path("outputs/state.sqlite3"),
                    Path("outputs/runs"),
                    run_id=run_id,
                    workload_profile=profile,
                )
                row["transport"] = "online"
            row.update(
                status=summary["status"], summary=summary, gate=quality_gate(summary, scenario)
            )
            _save(path, result)
        _finish_matrix(result, scenario)
        finished = True
    finally:
        if not finished:
            result["status"] = "interrupted"
            result["note"] = (
                "Inspect the persisted run IDs before recovery. Never repeat an ambiguous paid submission."
            )
        result["report_path"] = str(path.resolve())
        _save(path, result)
    return result


def refresh_scenario(path: Path) -> dict[str, Any]:
    result = json.loads(path.read_text(encoding="utf-8"))
    scenario = Scenario.model_validate(result["scenario"])
    access = SchedulerAccess(load_settings(Path(result["config_path"])))
    try:
        for row in result["runs"]:
            if row.get("transport") == "scheduler":
                summary = access.detail(row["run_id"])
                row.update(
                    status=summary["status"], summary=summary, gate=quality_gate(summary, scenario)
                )
        _finish_matrix(result, scenario)
        _save(path, result)
        return dict(result)
    finally:
        access.close()
