from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from filelock import FileLock
from pydantic import BaseModel, ConfigDict, Field

from .complex_workload import V2_PROMPT_HASH, configure, generate_complex
from .config import Settings
from .load_testing import Scenario, Stage, _save, quality_gate
from .pipeline import run_pipeline
from .prepare import estimate_cost, estimate_input_tokens, input_fingerprint
from .provider import AzureProvider
from .reporting import analysis_csv, summarize_run
from .state import State
from .synthetic import read_records, write_records
from .telemetry import elapsed_between, utc_now


class ReproductionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="gpt5-mini-client-groups", pattern=r"^[a-z0-9-]+$")
    count: int = Field(default=1700, ge=5, le=1700)
    seed: int = 42
    groups: int = Field(default=5, ge=5, le=5)
    cooldown_seconds: int = Field(default=65, ge=60, le=3600)
    stage_timeout_seconds: int = Field(default=36000, ge=60, le=36000)
    max_estimated_inference_usd: float = Field(default=150, gt=0, le=150, allow_inf_nan=False)
    expected_tpm: int = Field(default=150000, ge=1000, le=300000)
    expected_rpm: int = Field(default=150, ge=1)


def reproduction_settings(
    base: Settings, spec: ReproductionSpec
) -> tuple[Settings, dict[str, Any]]:
    if base.model_name != "gpt-5-mini" or base.model_version != "2025-08-07":
        raise ValueError("V3 requires a separate gpt-5-mini 2025-08-07 private configuration")
    if base.region not in {
        "eastus",
        "eastus2",
        "centralus",
        "northcentralus",
        "southcentralus",
        "westus",
        "westus2",
        "westus3",
        "westcentralus",
    }:
        raise ValueError("V3 requires an explicitly configured US resource region")
    if base.reasoning_effort != "low":
        raise ValueError("V3 preserves v2's low reasoning effort")
    if spec.count % spec.groups:
        raise ValueError("This reproduction uses five equal contiguous groups")
    settings, profile = configure(base)
    if profile["prompt_bundle_sha256"] != V2_PROMPT_HASH:
        raise ValueError("V3 must use the unchanged v2 Markdown/schema bundle")
    settings = Settings.model_validate(
        {
            **settings.model_dump(),
            "concurrency": 5,
            "rpm": spec.expected_rpm,
            "tpm": spec.expected_tpm,
            "max_attempts": 3,
            "max_http_attempts": spec.count * 3,
            "run_timeout_seconds": spec.stage_timeout_seconds,
            "max_run_cost_usd": min(75, spec.max_estimated_inference_usd / 2),
            "observe_service_throttling": True,
            "observation_profile": "full-reproduction",
        }
    )
    return settings, {
        **profile,
        "experiment": "v3-gpt5-mini-client-groups",
        "transport": "online-chat-completions",
        "batch_api_used": False,
        "groups": spec.groups,
        "records_per_group": spec.count // spec.groups,
        "pacing": "unpaced_initial_attempts_with_bounded_service_guided_retries",
    }


def plan_reproduction(spec: ReproductionSpec, base: Settings) -> dict[str, Any]:
    settings, profile = reproduction_settings(base, spec)
    records = generate_complex(spec.count, spec.seed)
    estimate = estimate_cost(records, settings, batch=False)
    ceiling = estimate["estimated_inference_usd"] * settings.max_attempts * 2
    if ceiling > spec.max_estimated_inference_usd:
        raise ValueError("Worst-case token estimate exceeds the approved reproduction allowance")
    tokens = [estimate_input_tokens(record, settings) for record in records]
    return {
        "status": "planned_not_executed",
        "specification": spec.model_dump(),
        "workload_profile": profile,
        "model": settings.public_metadata(),
        "modes": ["sequential", "client-batch"],
        "estimated_input_token_range": [min(tokens), max(tokens)],
        "first_attempt_estimate_per_mode_usd": estimate["estimated_inference_usd"],
        "conservative_all_attempts_estimate_usd": ceiling,
        "note": f"{spec.expected_tpm:,} TPM and three attempts are explicit test assumptions, not verified production settings. This is not the Azure Batch API.",
    }


class VerifiedProvider:
    def __init__(self, provider: Any, spec: ReproductionSpec):
        self.provider, self.spec = provider, spec

    def complete(self, record: Any) -> tuple[dict[str, Any], dict[str, str]]:
        body, headers = self.provider.complete(record)
        actual = {key.lower(): str(value) for key, value in headers.items()}
        if actual.get("x-ratelimit-limit-tokens") != str(self.spec.expected_tpm) or (
            actual.get("x-ratelimit-limit-requests") != str(self.spec.expected_rpm)
        ):
            raise ValueError(
                "Observed service TPM/RPM differs from the v3 scenario; stop and inspect before continuing"
            )
        return body, headers


def reproduction_status(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    state_path = Path(value["state_path"])
    if state_path.exists():
        store = State(state_path, readonly=True)
        try:
            known = {run["id"] for run in store.list_runs()}
            for stage in value["runs"]:
                if stage["run_id"] in known:
                    summary = summarize_run(store, stage["run_id"])
                    stage.update(status=summary["status"], summary=summary)
        finally:
            store.close()
    value["elapsed_seconds_including_waits"] = elapsed_between(
        value["started_at"], value.get("ended_at")
    )
    return dict(value)


def execute_reproduction(
    spec: ReproductionSpec,
    base: Settings,
    output: Path,
    *,
    resume: Path | None = None,
    provider_factory: Callable[[Settings], Any] = AzureProvider,
    on_created: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    settings, profile = reproduction_settings(base, spec)
    if resume:
        path = resume.resolve()
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            value["settings_fingerprint"] != settings.fingerprint()
            or value["specification"] != spec.model_dump()
        ):
            raise ValueError("Resume requires identical saved settings and scenario")
        if value["status"] in {"completed", "completed_with_findings"}:
            return reproduction_status(path)
        directory = path.parent
        records = read_records(directory / "input.jsonl")
        if input_fingerprint(records, settings) != value["dataset_fingerprint"]:
            raise ValueError("Saved reproduction input changed; do not compare different datasets")
    else:
        plan = plan_reproduction(spec, base)
        directory = (output / f"{spec.name}-{uuid.uuid4().hex[:12]}").resolve()
        directory.mkdir(parents=True, exist_ok=False)
        path = directory / "experiment.json"
        records = generate_complex(spec.count, spec.seed)
        write_records(directory / "input.jsonl", records)
        (directory / "config.json").write_text(settings.model_dump_json(indent=2), encoding="utf-8")
        (directory / "prompt.md").write_text(settings.prompt + "\n", encoding="utf-8")
        value = {
            **plan,
            "status": "pending",
            "started_at": utc_now(),
            "ended_at": None,
            "settings_fingerprint": settings.fingerprint(),
            "dataset_fingerprint": input_fingerprint(records, settings),
            "state_path": str(directory / "state.sqlite3"),
            "report_path": str(path),
            "runs": [
                {"mode": mode, "run_id": uuid.uuid4().hex, "status": "pending"}
                for mode in ("sequential", "client-batch")
            ],
        }
        _save(path, value)
    if on_created is not None:
        on_created(path)
    finished = False
    with FileLock(str(path.with_suffix(".lock")), timeout=0):
        try:
            value["status"] = "running"
            _save(path, value)
            for index, stage in enumerate(value["runs"]):
                if stage["status"] in {"succeeded", "partial_failure", "needs_attention"}:
                    continue
                if index:
                    time.sleep(spec.cooldown_seconds)
                stage["status"] = "running"
                _save(path, value)
                stage_settings = Settings.model_validate(
                    {
                        **settings.model_dump(),
                        "concurrency": 1 if stage["mode"] == "sequential" else spec.groups,
                    }
                )
                provider = provider_factory(stage_settings)
                try:
                    report = run_pipeline(
                        records,
                        stage_settings,
                        stage["mode"],
                        Path(value["state_path"]),
                        directory / "runs",
                        run_id=stage["run_id"],
                        provider=VerifiedProvider(provider, spec),
                        workload_profile=profile,
                    )
                finally:
                    provider.close()
                gate = quality_gate(
                    report,
                    Scenario(
                        name="v3-quality",
                        count=spec.count,
                        stages=[Stage(name="online", mode="parallel")],
                    ),
                )
                stage.update(status=report["status"], summary=report, gate=gate)
                _save(path, value)
            value.update(
                status="completed"
                if all(s["gate"]["status"] == "passed" for s in value["runs"])
                else "completed_with_findings",
                ended_at=utc_now(),
            )
            finished = True
        finally:
            if not finished:
                value["status"] = "interrupted"
                value["note"] = (
                    "Resume this same experiment after investigating. In-flight online results are not blindly reissued."
                )
            value["elapsed_seconds_including_waits"] = elapsed_between(
                value["started_at"], value.get("ended_at")
            )
            _save(path, value)
    return dict(value)


def resume_reproduction(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    saved = Settings.model_validate_json((path.parent / "config.json").read_text(encoding="utf-8"))
    return execute_reproduction(
        ReproductionSpec.model_validate(value["specification"]),
        saved,
        path.parent.parent,
        resume=path,
    )


def export_reproduction(path: Path, destination: Path) -> dict[str, Any]:
    value = reproduction_status(path)
    destination.mkdir(parents=True, exist_ok=True)
    permitted = (
        "mode",
        "status",
        "record_count",
        "succeeded",
        "failed",
        "pending",
        "elapsed_seconds",
        "completed_records_per_minute",
        "http_attempts",
        "errors_429",
        "retry_wait_seconds",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "http_200_latency_p50_seconds",
        "http_200_latency_p95_seconds",
        "successful_record_elapsed_mean_seconds",
        "all_record_elapsed_mean_seconds",
        "record_elapsed_measured_count",
        "observed_rate_limits",
        "priced_usage_estimate_usd",
        "quality",
        "semantic_status",
    )
    public: dict[str, Any] = {
        "observed_at": utc_now(),
        "started_at": value["started_at"],
        "execution_status": value["status"],
        "specification": value["specification"],
        "workload_profile": value["workload_profile"],
        "configured_model": value["model"],
        "conservative_all_attempts_estimate_usd": value["conservative_all_attempts_estimate_usd"],
        "elapsed_seconds_including_waits": value["elapsed_seconds_including_waits"],
        "results_final": value["status"] in {"completed", "completed_with_findings"},
        "runs": [],
    }
    state_path = Path(value["state_path"])
    store = State(state_path, readonly=True) if state_path.exists() else None
    try:
        for stage in value["runs"]:
            report = stage.get("summary", {})
            public["runs"].append(
                {
                    "mode": stage["mode"],
                    "status": stage["status"],
                    **{key: report.get(key) for key in permitted if key not in {"mode", "status"}},
                    "record_count": report.get("record_count", value["specification"]["count"]),
                    "gate": stage.get("gate"),
                    "client_groups": [
                        {key: job.get(key) for key in ("id", "kind", "status", "request_counts")}
                        for job in report.get("jobs", [])
                        if job.get("kind") == "client_group"
                    ],
                }
            )
            if store is not None and public["results_final"]:
                records = store.items("records", stage["run_id"])
                rows = [
                    {
                        key: item.get(key)
                        for key in (
                            "id",
                            "status",
                            "error",
                            "status_code",
                            "attempts",
                            "elapsed_seconds",
                            "group_id",
                            "result",
                            "quality",
                        )
                    }
                    for item in records
                ]
                (destination / f"{stage['mode']}-results.jsonl").write_text(
                    "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                    encoding="utf-8",
                )
                (destination / f"{stage['mode']}-analysis.csv").write_text(
                    analysis_csv(records), encoding="utf-8"
                )
        _save(destination / "summary.json", public)
    finally:
        if store is not None:
            store.close()
    return public
