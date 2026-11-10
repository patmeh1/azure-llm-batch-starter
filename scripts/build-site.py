from __future__ import annotations

import base64
import html
import json
import math
import os
import re
import shutil
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/patmeh1/azure-llm-batch-starter"
PAGES = {
    "index.html": (
        "overview",
        "Overview",
        "Two GPT models, five versions",
        "The story of capacity, prompt complexity, routing, and measured request latency across five synthetic Azure LLM experiments.",
    ),
    "comparison.html": (
        "comparison",
        "Compare versions",
        "V1 vs V2 vs V3 vs V4 vs V5 comparison",
        "Compare exact GPT models, prompt workloads, quotas, pacing, processing modes, accuracy, costs, and evidence status.",
    ),
    "v1.html": (
        "v1",
        "V1 baseline",
        "V1: GPT-5.4 mini baseline",
        "Measured GPT-5.4 mini compact, token-stress, and service-Batch capacity findings.",
    ),
    "v2.html": (
        "v2",
        "V2 complexity",
        "V2: GPT-5.4 mini complex prompts",
        "Complex synthetic prompts processed without 429s, but all three semantic acceptance gates failed.",
    ),
    "v3.html": (
        "v3",
        "V3 GPT-5 mini",
        "V3: GPT-5 mini grouped requests",
        "Timestamped evidence for 1,700 sequential requests versus five client-side groups at 150K TPM.",
    ),
    "v4.html": (
        "v4",
        "V4 multi-deployment",
        "V4: one deployment versus three",
        "GPT-5 mini with unchanged complex prompts: one 150K deployment versus three 50K deployments, executed durably in Azure.",
    ),
    "v5.html": (
        "v5",
        "V5 smart routing",
        "V5: quota-aware routing and average request time",
        "520 records per arm on five GPT-5 mini deployments: matched reactive versus quota-aware scheduling with measured HTTP and record completion averages.",
    ),
    "approach.html": (
        "approach",
        "What works",
        "What works and what to change",
        "Evidence-based guidance on quota, pacing, service Batch, and independent semantic quality evaluation.",
    ),
    "resources.html": (
        "resources",
        "Resources",
        "Guides, prompts, code and downloads",
        "Public prompt files, guides, load scenarios, source code, measured output examples, and releases.",
    ),
    "methodology.html": (
        "methodology",
        "Methodology",
        "How to read the evidence",
        "What is comparable across versions and why these synthetic experiments are not a model leaderboard.",
    ),
}
DATA_FILES = {
    "v1-results.json": "live-results.json",
    "v2-results.json": "complex-v2-results.json",
    "v3-results.json": "v3-results.json",
    "v3-partial-output-sample.json": "v3-partial-output-sample.json",
    "v4-results.json": "v4-results.json",
    "v4-output-samples.json": "v4-output-samples.json",
    "v5-results.json": "v5-results.json",
    "v5-output-samples.json": "v5-output-samples.json",
}
PROMPTS = ("simple-v1.md", "complex-system-v2.md", "complex-taxonomy-v2.md", "complex-user-v2.md")
PRIVATE_KEYS = {
    "endpoint",
    "api_key",
    "authorization",
    "access_token",
    "tenant_id",
    "subscription_id",
    "resource_id",
    "model_resource_id",
    "managed_identity_client_id",
    "storage_account",
    "scheduler_endpoint",
    "config_path",
    "state_path",
    "report_path",
    "directory",
    "remote_id",
    "file_id",
}
PRIVATE_VALUE = re.compile(
    r"(?:(?:^|\s)[A-Za-z]:[\\/]|(?:openai\.azure\.com|cognitiveservices\.azure\.com|"
    r"azurewebsites\.net|blob\.core\.windows\.net|durabletask\.io)|"
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|"
    r"gh[pousr]_[A-Za-z0-9_]{20,}|-----BEGIN .*PRIVATE KEY-----)",
    re.IGNORECASE,
)


def public_only(value: Any, path: str = "evidence") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in PRIVATE_KEYS:
                raise ValueError(f"Private-context key cannot be published: {path}.{key}")
            public_only(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            public_only(child, f"{path}[{index}]")
    elif isinstance(value, str) and PRIVATE_VALUE.search(value):
        raise ValueError(f"Private-context value cannot be published: {path}")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"Nonfinite measurement: {path}")


def number(value: Any, *, optional: bool = True) -> float | None:
    if value is None and optional:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError(f"Invalid nonnegative measurement: {value!r}")
    return float(value)


def count(value: Any) -> str:
    measured = number(value)
    if measured is None:
        return "&mdash;"
    if not measured.is_integer():
        raise ValueError("A record count must be an integer")
    return f"{int(measured):,}"


def percent(value: Any) -> str:
    measured = number(value)
    if measured is None:
        return "Not evaluated"
    if measured > 1:
        raise ValueError("Accuracy must be a fraction in 0..1")
    return f"{measured * 100:.1f}%"


def seconds(value: Any) -> str:
    measured = number(value)
    return "&mdash;" if measured is None else f"{measured:,.2f} s"


def clock(value: Any) -> str:
    measured = number(value)
    if measured is None:
        return "&mdash;"
    minutes, remainder = divmod(round(measured), 60)
    return f"{minutes:,}m {remainder:02d}s"


def money(value: Any, places: int = 4) -> str:
    measured = number(value)
    return "Not available" if measured is None else f"${measured:,.{places}f}"


def utc_label(value: str) -> str:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError("Snapshot timestamp must include its timezone")
    return moment.astimezone(UTC).strftime("%b %d, %Y at %H:%M UTC")


def ratio(row: dict[str, Any]) -> str:
    total = number(row["record_count"], optional=False)
    values = [number(row.get(key)) for key in ("succeeded", "failed", "pending")]
    known_total = sum(value for value in values if value is not None)
    if total is not None and known_total > total:
        raise ValueError("Outcome counts exceed the expected records")
    if (
        all(value is not None for value in values)
        and sum(value for value in values if value is not None) != total
    ):
        raise ValueError("Outcome counts do not reconcile")
    return f"{count(row.get('succeeded'))} / {count(total)}"


def duration_cell(value: Any, maximum: float) -> str:
    measured = number(value, optional=False)
    assert measured is not None
    width = min(100, measured * 100 / maximum) if maximum else 0
    return (
        f'<td class="bar-cell number">{seconds(measured)}'
        f'<div class="bar-track" aria-hidden="true"><div class="bar-fill" style="width:{width:.2f}%"></div></div></td>'
    )


def tag(label: str, kind: str = "") -> str:
    return f'<span class="tag {kind}">{html.escape(label)}</span>'


def render_v3(value: dict[str, Any]) -> dict[str, str]:
    final = value["results_final"]
    if not isinstance(final, bool):
        raise ValueError("V3 requires an explicit results_final boolean")
    terminal = value["execution_status"] in {"completed", "completed_with_findings"}
    if final != terminal:
        raise ValueError("V3 final flag contradicts its execution state")
    states = {
        "running": ("Full run in progress", "incomplete"),
        "pending": ("Not yet started", "incomplete"),
        "interrupted": ("Run interrupted", "attention"),
        "completed": ("Run completed", ""),
        "completed_with_findings": ("Completed with findings", "attention"),
    }
    label, kind = states[value["execution_status"]]
    observed = utc_label(value["observed_at"])
    rows, quality_rows, measured_done = [], [], 0.0
    spec = value["specification"]
    if (
        spec["count"] != 1700
        or spec["groups"] != 5
        or spec["expected_tpm"] != 150000
        or spec["expected_rpm"] != 150
    ):
        raise ValueError("Review the v3 page narrative before changing the recorded scenario")
    if (
        value["configured_model"]["model_name"] != "gpt-5-mini"
        or value["workload_profile"]["batch_api_used"] is not False
    ):
        raise ValueError("V3 must be the recorded GPT-5 mini online grouped experiment")
    expected_total = number(spec["count"], optional=False)
    assert expected_total is not None
    names = {
        "sequential": "Sequential online",
        "client-batch": f"{count(spec['groups'])} client groups of {count(expected_total / spec['groups'])}",
    }
    if {row["mode"] for row in value["runs"]} != set(names) or len(value["runs"]) != 2:
        raise ValueError("V3 must contain exactly the sequential and client-batch stages")
    for row in value["runs"]:
        if row.get("record_count") != expected_total:
            raise ValueError("V3 stage size differs from its scenario")
        ratio(row)
        if final and row.get("pending") != 0:
            raise ValueError("Final v3 evidence must reconcile every expected record")
        if final and row["status"] not in {"succeeded", "partial_failure", "needs_attention"}:
            raise ValueError("Final v3 evidence contains a nonterminal stage")
        if row.get("succeeded") is not None and row.get("failed") is not None:
            measured_done += float(row["succeeded"]) + float(row["failed"])
        state = html.escape(str(row["status"]).replace("_", " "))
        elapsed = seconds(row.get("elapsed_seconds")) if final else "Not final"
        rows.append(
            f'<tr><th scope="row">{html.escape(names[row["mode"]])}</th><td>{state}</td>'
            f'<td class="number">{count(row.get("succeeded"))} / {count(expected_total)}</td>'
            f'<td class="number">{count(row.get("failed"))}</td><td class="number">{count(row.get("pending"))}</td>'
            f'<td class="number">{count(row.get("errors_429"))}</td><td class="number">{elapsed}</td></tr>'
        )
        quality = row.get("quality") or {}
        fields = quality.get("field_accuracy", {})
        quality_rows.append(
            f'<tr><th scope="row">{html.escape(names[row["mode"]])}</th>'
            f"<td>{percent(fields.get('primary_route'))}</td><td>{percent(fields.get('confidence'))}</td>"
            f"<td>{count(quality.get('passed_records'))} / {count(expected_total)}</td>"
            f"<td>{html.escape(str(row.get('semantic_status') or 'Not evaluated').replace('_', ' '))}</td></tr>"
        )
    if final:
        notice = (
            "<strong>Both modes are terminal. Completion is not a quality guarantee.</strong>"
            "<p>Review failed records, semantic checks, and retry time alongside total elapsed time. "
            "These are synthetic observations, not a production performance promise.</p>"
        )
        caption = (
            "Final reconciled observations. 429 attempts are separate from final failed records."
        )
        quality_html = (
            '<div class="table-shell" tabindex="0" role="region" aria-label="V3 final quality results"><table>'
            "<caption>Semantic accuracy is among evaluated responses; all-check counts use all expected records.</caption>"
            '<thead><tr><th scope="col">Mode</th><th scope="col">Primary route</th><th scope="col">Confidence rule</th>'
            '<th scope="col">All checks</th><th scope="col">Semantic status</th></tr></thead><tbody>'
            + "".join(quality_rows)
            + "</tbody></table></div>"
        )
    else:
        execution = (
            "The sequential checkpoint is incomplete and no active runner was observed at the latest check. "
            "The interruption cause is not established; no new paid run was started."
            if value["execution_status"] == "interrupted"
            else "The sequential stage runs first; five fixed groups follow."
        )
        notice = (
            f"<strong>{html.escape(label)}. Final comparison results are not available.</strong>"
            f"<p>{execution} Partial successes and 429 counts "
            "are observations, not final latency, throughput, or quality results.</p>"
        )
        caption = (
            "Progress only. Dashes mean not yet measured, not zero. Final durations are withheld."
        )
        quality_html = '<p class="small">Final quality and latency comparisons are withheld until both stages reconcile.</p>'
    progress = measured_done * 100 / (expected_total * 2)
    progress_html = (
        '<div class="card" style="margin-block:1.25rem"><div class="status-line">'
        f"<strong>{count(measured_done)} / {count(expected_total * 2)} record evaluations have terminal outcomes</strong>"
        f'<span class="small">{progress:.1f}% of the two-mode workload; not an ETA or success rate</span></div>'
        f'<div class="progress-track" role="progressbar" aria-label="V3 records with terminal outcomes" '
        f'aria-valuemin="0" aria-valuemax="{int(expected_total * 2)}" aria-valuenow="{int(measured_done)}">'
        f'<div class="progress-fill" style="width:{progress:.2f}%"></div></div></div>'
    )
    return {
        "V3_FINAL": str(final).lower(),
        "V3_CARD_STATUS": tag(label, kind),
        "V3_STATUS": tag(label, kind),
        "V3_NOTICE": notice,
        "V3_DATETIME": html.escape(value["observed_at"], quote=True),
        "V3_OBSERVED": observed,
        "V3_CAPTION": caption,
        "V3_ROWS": "".join(rows),
        "V3_PROGRESS": progress_html,
        "V3_QUALITY": quality_html,
        "V3_BUDGET": money(value["conservative_all_attempts_estimate_usd"], 2),
        "V3_FRESHNESS": (
            f"Last recorded activity: {utc_label(value['last_activity_at'])}."
            if value.get("last_activity_at")
            else "Snapshot time is not a continuous liveness check."
        ),
    }


def validate_v4(value: dict[str, Any]) -> None:
    if not isinstance(value["results_final"], bool):
        raise ValueError("V4 requires an explicit results_final boolean")
    if value["results_final"] != (value["execution_status"] == "completed"):
        raise ValueError("V4 final flag contradicts its execution state")
    model, spec = value["model"], value["specification"]
    if (
        model["model_name"] != "gpt-5-mini"
        or model["model_version"] != "2025-08-07"
        or model["max_output_tokens"] != 2048
        or model["reasoning_effort"] != "low"
        or value["workload_profile"]["batch_api_used"] is not False
        or spec["count"] != 65
        or spec["workers"] != 5
        or spec["max_attempts"] != 4
        or value["active_aggregate_tpm_per_arm"] != 150000
        or value["active_aggregate_rpm_per_arm"] != 150
    ):
        raise ValueError("Review the v4 matched-quota pilot narrative before changing its setup")
    arms = [stage["arm"] for stage in value["stages"]]
    if len(arms) != len(set(arms)) or set(arms) - {"single", "pool"}:
        raise ValueError("Invalid v4 experiment arms")
    if value["results_final"] and set(arms) != {"single", "pool"}:
        raise ValueError("Final v4 evidence needs both arms")
    if value["results_final"]:
        expected_status = (
            "needs_attention" if any(stage["failed"] for stage in value["stages"]) else "succeeded"
        )
        if value["processing_status"] != expected_status:
            raise ValueError("V4 processing status contradicts its record outcomes")
    for stage in value["stages"]:
        if stage["record_count"] != spec["count"]:
            raise ValueError("V4 stage size differs from the pilot")
        ratio(stage)
        if value["results_final"]:
            if stage["pending"] != 0 or stage["status"] not in {"succeeded", "partial_failure"}:
                raise ValueError("Final v4 evidence has unreconciled records")
            if not number(stage["elapsed_seconds"], optional=False):
                raise ValueError("Final v4 duration must be positive")
        allocations = stage["target_metrics"]
        expected = 1 if stage["arm"] == "single" else 3
        if (
            len(allocations) != expected
            or sum(target["allocated_tpm"] for target in allocations) != 150000
            or sum(target["allocated_rpm"] for target in allocations) != 150
            or any(target["allocated_tpm"] != 150000 // expected for target in allocations)
            or any(target["allocated_rpm"] != 150 // expected for target in allocations)
        ):
            raise ValueError("V4 arms must have matched aggregate quota")
        for field in ("http_attempts", "errors_429"):
            if sum(target[field] for target in allocations) != stage[field]:
                raise ValueError("V4 target attempt counts do not reconcile")


def render_v4(value: dict[str, Any]) -> dict[str, str]:
    validate_v4(value)
    final = value["results_final"]
    spec = value["specification"]
    stages = {stage["arm"]: stage for stage in value["stages"]}
    labels = {"single": "One deployment", "pool": "Three deployments"}
    rows, quality_rows, target_rows, latency_rows, token_rows = [], [], [], [], []
    for arm, label in labels.items():
        stage = stages.get(arm, {"record_count": spec["count"]})
        valid = f"{count(stage.get('succeeded'))} / {count(spec['count'])}"
        elapsed = seconds(stage.get("elapsed_seconds")) if final else "Not final"
        throughput = (
            f"{stage['succeeded'] * 60 / stage['elapsed_seconds']:.2f}" if final else "&mdash;"
        )
        rows.append(
            f'<tr><th scope="row">{label}</th><td class="number">{elapsed}</td>'
            f"<td>{valid}</td><td>{count(stage.get('failed'))}</td>"
            f"<td>{count(stage.get('http_attempts'))}</td><td>{count(stage.get('errors_429'))}</td>"
            f"<td>{throughput}</td><td>{money(stage.get('priced_usage_estimate_usd'))}</td></tr>"
        )
        quality = stage.get("quality") or {}
        fields = quality.get("field_accuracy", {})
        gate = (stage.get("gate") or {}).get("status", "not_evaluated")
        quality_rows.append(
            f'<tr><th scope="row">{label}</th><td>{count(quality.get("evaluated_records"))} / {count(spec["count"])}</td>'
            f"<td>{percent(fields.get('primary_route'))}</td>"
            f"<td>{percent(fields.get('confidence'))}</td><td>{count(quality.get('passed_records'))} / {count(spec['count'])}</td>"
            f"<td>{html.escape(gate.replace('_', ' '))}</td></tr>"
        )
        latency_rows.append(
            f'<tr><th scope="row">{label}</th>'
            + "".join(
                f"<td>{seconds(stage.get(key)) if final else 'Not final'}</td>"
                for key in (
                    "all_record_elapsed_mean_seconds",
                    "successful_record_elapsed_mean_seconds",
                    "http_200_latency_p50_seconds",
                    "http_200_latency_p95_seconds",
                    "router_wait_seconds",
                )
            )
            + "</tr>"
        )
        token_rows.append(
            f'<tr><th scope="row">{label}</th>'
            + "".join(
                f"<td>{count(stage.get(key))}</td>"
                for key in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens")
            )
            + "</tr>"
        )
        for target in stage.get("target_metrics", []):
            limits = (
                "; ".join(
                    f"{html.escape(str(tpm))} TPM / {html.escape(str(rpm))} RPM"
                    for tpm, rpm in target["observed_rate_limits"]
                )
                or "Not returned"
            )
            target_rows.append(
                f'<tr><th scope="row">{html.escape(target["alias"])}</th>'
                f"<td>{count(target['allocated_tpm'])} / {count(target['allocated_rpm'])}</td>"
                f"<td>{count(target['http_attempts'])}</td><td>{count(target['responses_200'])}</td>"
                f"<td>{count(target['errors_429'])}</td><td>{limits}</td></tr>"
            )
    if final:
        single, pool = stages["single"], stages["pool"]
        story = (
            f"<strong>One deployment:</strong> {count(single['succeeded'])}/65 validated in "
            f"{seconds(single['elapsed_seconds'])}, {count(single['errors_429'])} HTTP 429 attempts. "
            f"<strong>Three deployments:</strong> {count(pool['succeeded'])}/65 validated in "
            f"{seconds(pool['elapsed_seconds'])}, {count(pool['errors_429'])} HTTP 429 attempts."
        )
        if (
            (single["failed"] or pool["failed"])
            and pool["succeeded"] > single["succeeded"]
            and pool["errors_429"] < single["errors_429"]
            and pool["elapsed_seconds"] > single["elapsed_seconds"]
        ):
            verdict = (
                "The pool completed more records and logged fewer 429s, but took longer. "
                "Full-record completion was not achieved."
            )
        elif single["failed"] or pool["failed"]:
            verdict = (
                "Not every record succeeded. A shorter failed run is not a scaling improvement."
            )
        elif pool["elapsed_seconds"] < single["elapsed_seconds"]:
            verdict = (
                f"The pool delivered {single['elapsed_seconds'] / pool['elapsed_seconds']:.2f}x "
                "the validated-record throughput in this pilot, not a sustained-capacity guarantee."
            )
        else:
            verdict = (
                f"The pool took {pool['elapsed_seconds'] / single['elapsed_seconds']:.2f}x as long "
                "at the same aggregate quota. Splitting deployments did not improve this pilot."
            )
        notice = (
            "<strong>Both pilot arms are terminal; processing and quality are separate.</strong>"
            f"<p>{story}</p><p>{verdict}</p>"
        )
        if value["router_reconciliation_required"]:
            notice += "<p><strong>Some online outcomes remain ambiguous. The routing lease is held for investigation.</strong></p>"
        status = tag(
            "Pilot completed; review findings"
            if value["acceptance_status"] != "passed"
            else "Pilot completed",
            "attention" if value["acceptance_status"] != "passed" else "",
        )
    else:
        story = "The Azure-hosted pilot is incomplete. Final durations and the single-versus-pool conclusion are withheld."
        verdict = "No final scaling conclusion is available."
        notice = (
            f"<strong>Pilot incomplete.</strong><p>{story} Dashes mean not measured, not zero.</p>"
        )
        status = tag("Pilot incomplete", "incomplete")
    return {
        "V4_FINAL": str(final).lower(),
        "V4_STATUS": status,
        "V4_NOTICE": notice,
        "V4_STORY": story,
        "V4_VERDICT": verdict,
        "V4_ROWS": "".join(rows),
        "V4_QUALITY_ROWS": "".join(quality_rows),
        "V4_TARGET_ROWS": "".join(target_rows),
        "V4_LATENCY_ROWS": "".join(latency_rows),
        "V4_TOKEN_ROWS": "".join(token_rows),
        "V4_DATETIME": html.escape(value["observed_at"], quote=True),
        "V4_OBSERVED": utc_label(value["observed_at"]),
        "V4_BUDGET": money(value.get("conservative_all_attempts_estimate_usd"), 2),
        "V4_COST": money(value.get("reported_model_usage_estimate_usd")),
        "V4_WALL": seconds(value.get("end_to_end_elapsed_seconds")) if final else "Not final",
    }


def validate_v5(value: dict[str, Any]) -> None:
    final = value["results_final"]
    if not isinstance(final, bool) or final != (value["execution_status"] == "completed"):
        raise ValueError("V5 final flag contradicts its execution state")
    spec, model = value["specification"], value["model"]
    if (
        spec["count"] != 520
        or spec["workers"] != 5
        or spec["max_attempts"] != 4
        or model["model_name"] != "gpt-5-mini"
        or model["model_version"] != "2025-08-07"
        or model["max_output_tokens"] != 2048
        or model["reasoning_effort"] != "low"
        or value["active_aggregate_tpm_per_arm"] != 300000
        or value["allocated_deployments_total"] != 5
        or value["workload_profile"]["batch_api_used"] is not False
    ):
        raise ValueError("Review the V5 five-target 520-record narrative before changing its setup")
    arms = [s["arm"] for s in value["stages"]]
    if len(arms) != len(set(arms)) or set(arms) - {"reactive", "adaptive"}:
        raise ValueError("Invalid V5 arm set")
    if final and set(arms) != {"reactive", "adaptive"}:
        raise ValueError("Final V5 evidence requires both benchmark arms")
    if final:
        warmups = value["warmups"]
        if {s["arm"] for s in warmups} != {"reactive-warmup", "adaptive-warmup"} or len(
            warmups
        ) != 2:
            raise ValueError("V5 warm-up accounting is incomplete")
        if value["benchmark_valid"] != all(s["succeeded"] == 5 for s in warmups):
            raise ValueError("V5 warm-up validity contradicts its outcomes")
        expected_status = (
            "needs_attention" if any(s["failed"] for s in value["stages"]) else "succeeded"
        )
        if value["processing_status"] != expected_status:
            raise ValueError("V5 processing status contradicts its outcomes")
    for stage in [*value["stages"], *value.get("warmups", [])]:
        total = 5 if stage["arm"].endswith("-warmup") else 520
        if stage["record_count"] != total:
            raise ValueError("V5 record count differs from its declared stage")
        ratio(stage)
        if final and (
            stage["pending"] != 0 or stage["status"] not in {"succeeded", "partial_failure"}
        ):
            raise ValueError("V5 final stages must reconcile every record")
        targets = stage["target_metrics"]
        expected = {"target-1": 100000, **{f"target-{i}": 50000 for i in range(2, 6)}}
        if len(targets) != 5 or {t["alias"]: t["allocated_tpm"] for t in targets} != expected:
            raise ValueError("V5 requires the same five-target 300K allocation in both arms")
        for key in ("http_attempts", "errors_429"):
            if sum(t[key] for t in targets) != stage[key]:
                raise ValueError("V5 per-target attempt counts do not reconcile")
        if final:
            if stage["http_200_latency_measured_count"] != sum(t["responses_200"] for t in targets):
                raise ValueError("V5 HTTP mean denominator differs from its 200 responses")
            if (
                stage["record_elapsed_measured_count"] != total
                or stage["queue_to_completion_measured_count"] != total
            ):
                raise ValueError("V5 record timing coverage is incomplete")
            if stage["successful_record_elapsed_measured_count"] != stage["succeeded"]:
                raise ValueError("V5 successful-record mean has the wrong denominator")
            for metric, denominator in (
                ("http_200_latency_mean_seconds", "http_200_latency_measured_count"),
                ("http_attempt_latency_mean_seconds", "http_attempt_latency_measured_count"),
                (
                    "successful_record_elapsed_mean_seconds",
                    "successful_record_elapsed_measured_count",
                ),
                ("all_record_elapsed_mean_seconds", "record_elapsed_measured_count"),
                ("queue_to_completion_mean_seconds", "queue_to_completion_measured_count"),
            ):
                if (stage[metric] is None) != (stage[denominator] == 0):
                    raise ValueError("V5 averages must have explicit measured denominators")
                number(stage[metric])
            if not number(stage["elapsed_seconds"], optional=False):
                raise ValueError("V5 final arm duration must be positive")


def render_v5(value: dict[str, Any]) -> dict[str, str]:
    validate_v5(value)
    final = value["results_final"]
    labels = {"reactive": "Reactive round robin", "adaptive": "Quota-aware scheduling"}
    stages = {s["arm"]: s for s in value["stages"]}
    rows, timings, targets, quality_rows, warmups = [], [], [], [], []

    def measured(stage: dict[str, Any], key: str) -> str:
        return seconds(stage.get(key)) if final else "Not final"

    for arm, label in labels.items():
        stage = stages.get(arm, {})
        rate = f"{stage['succeeded'] * 60 / stage['elapsed_seconds']:.2f}" if final else "&mdash;"
        rows.append(
            f'<tr><th scope="row">{label}</th><td>{count(stage.get("succeeded"))} / 520</td>'
            f"<td>{count(stage.get('failed'))}</td><td>{count(stage.get('errors_429'))}</td>"
            f"<td>{measured(stage, 'elapsed_seconds')}</td><td>{rate}</td>"
            f"<td>{measured(stage, 'http_200_latency_mean_seconds')}</td>"
            f"<td>{measured(stage, 'successful_record_elapsed_mean_seconds')}</td>"
            f"<td>{money(stage.get('priced_usage_estimate_usd'))}</td></tr>"
        )
        for metric, denominator, description in (
            (
                "http_200_latency_mean_seconds",
                "http_200_latency_measured_count",
                "Mean successful HTTP call",
            ),
            (
                "http_attempt_latency_mean_seconds",
                "http_attempt_latency_measured_count",
                "Mean all HTTP attempts, including errors",
            ),
            (
                "successful_record_elapsed_mean_seconds",
                "successful_record_elapsed_measured_count",
                "Mean successful record processing",
            ),
            (
                "all_record_elapsed_mean_seconds",
                "record_elapsed_measured_count",
                "Mean all record processing, including failures",
            ),
            (
                "queue_to_completion_mean_seconds",
                "queue_to_completion_measured_count",
                "Mean enqueue-to-record-completion",
            ),
        ):
            timings.append(
                f'<tr><th scope="row">{label}</th><td>{description}</td>'
                f"<td>{measured(stage, metric)}</td><td>{count(stage.get(denominator)) if final else '&mdash;'}</td></tr>"
            )
        for t in stage.get("target_metrics", []):
            limits = (
                "; ".join(
                    f"{html.escape(str(a))} TPM / {html.escape(str(b))} RPM"
                    for a, b in t["observed_rate_limits"]
                )
                or "Not returned"
            )
            targets.append(
                f'<tr><th scope="row">{label}</th><td>{html.escape(t["alias"])}</td>'
                f"<td>{count(t['allocated_tpm'])}</td><td>{count(t['http_attempts'])}</td>"
                f"<td>{count(t['responses_200'])}</td><td>{count(t['errors_429'])}</td>"
                f"<td>{seconds(t.get('http_200_latency_mean_seconds'))}</td><td>{limits}</td></tr>"
            )
        q = stage.get("quality") or {}
        fields = q.get("field_accuracy", {})
        quality_rows.append(
            f'<tr><th scope="row">{label}</th><td>{count(q.get("evaluated_records"))} / 520</td>'
            f"<td>{percent(fields.get('primary_route'))}</td><td>{percent(fields.get('confidence'))}</td>"
            f"<td>{count(q.get('passed_records'))} / 520</td>"
            f"<td>{html.escape((stage.get('gate') or {}).get('status', 'not_evaluated').replace('_', ' '))}</td></tr>"
        )
    for stage in value.get("warmups", []):
        warmups.append(
            f'<tr><th scope="row">{html.escape(stage["arm"])}</th><td>{ratio(stage)}</td>'
            f"<td>{count(stage['http_attempts'])}</td><td>{count(stage['errors_429'])}</td>"
            f"<td>{seconds(stage['elapsed_seconds'])}</td><td>{money(stage['priced_usage_estimate_usd'])}</td></tr>"
        )
    if final:
        baseline, paced = stages["reactive"], stages["adaptive"]
        story = (
            f"<strong>Reactive:</strong> {count(baseline['succeeded'])}/520 validated in {seconds(baseline['elapsed_seconds'])}; "
            f"mean successful HTTP call {seconds(baseline['http_200_latency_mean_seconds'])}. "
            f"<strong>Quota-aware:</strong> {count(paced['succeeded'])}/520 validated in {seconds(paced['elapsed_seconds'])}; "
            f"mean successful HTTP call {seconds(paced['http_200_latency_mean_seconds'])}."
        )
        if not value["benchmark_valid"]:
            verdict = (
                "Warm-up validation failed. This is not a valid complete five-target benchmark."
            )
        elif any(s["http_attempted_record_count"] < 500 for s in stages.values()):
            verdict = "Fewer than 500 records reached inference in an arm; do not claim a full-size benchmark."
        elif baseline["failed"] and not paced["failed"] and baseline["succeeded"]:
            change = 100 * (paced["elapsed_seconds"] / baseline["elapsed_seconds"] - 1)
            direction = "longer" if change >= 0 else "shorter"
            verdict = (
                f"Quota-aware processing completed all 520 records with {count(paced['errors_429'])} observed 429s. "
                f"Mean successful-record processing was {seconds(baseline['successful_record_elapsed_mean_seconds'])} "
                f"versus {seconds(paced['successful_record_elapsed_mean_seconds'])}; full arm time was "
                f"{abs(change):.1f}% {direction}. The reactive arm failed records, so a complete-job speedup is not established."
            )
        elif baseline["failed"] or paced["failed"]:
            verdict = "At least one arm failed records. Compare useful throughput and failures; a full-workload speedup is not established."
        elif paced["elapsed_seconds"] < baseline["elapsed_seconds"]:
            verdict = (
                f"Quota-aware processing completed all 520 records with {baseline['elapsed_seconds'] / paced['elapsed_seconds']:.2f}x "
                "the validated-record throughput in this paired run. This is not a sustained-capacity guarantee."
            )
        else:
            verdict = (
                f"Quota-aware processing took {paced['elapsed_seconds'] / baseline['elapsed_seconds']:.2f}x as long "
                "for the complete workload. No speedup was observed in this paired run."
            )
        notice = f"<strong>Both benchmark arms are terminal.</strong><p>{story}</p><p>{verdict}</p>"
        if value["router_reconciliation_required"]:
            notice += "<p><strong>Uncertain paid outcomes remain. The experiment lease is held for investigation.</strong></p>"
        status = tag("Paired run complete; inspect quality", "attention")
    else:
        story = "The Azure-hosted paired run is incomplete. Final average request times and throughput conclusions are withheld."
        verdict = "No final V5 speedup claim is available."
        notice = f"<strong>Benchmark in progress or awaiting reconciliation.</strong><p>{story}</p>"
        status = tag("Results not final", "incomplete")
    return {
        "V5_FINAL": str(final).lower(),
        "V5_STATUS": status,
        "V5_NOTICE": notice,
        "V5_STORY": story,
        "V5_VERDICT": verdict,
        "V5_ROWS": "".join(rows),
        "V5_TIMING_ROWS": "".join(timings),
        "V5_TARGET_ROWS": "".join(targets),
        "V5_QUALITY_ROWS": "".join(quality_rows),
        "V5_WARMUP_ROWS": "".join(warmups),
        "V5_DATETIME": html.escape(value["observed_at"], quote=True),
        "V5_OBSERVED": utc_label(value["observed_at"]),
        "V5_COST": money(value.get("reported_model_usage_estimate_usd")),
        "V5_WALL": seconds(value.get("end_to_end_elapsed_seconds")) if final else "Not final",
        "V5_GUARD": money(value["specification"]["max_estimated_inference_usd"], 2),
    }


def compare_versions(
    v1: dict[str, Any],
    v2: dict[str, Any],
    v3: dict[str, Any],
    v4: dict[str, Any],
    v5: dict[str, Any],
) -> dict[str, str]:
    validate_v4(v4)
    validate_v5(v5)
    experiments = {row["scenario"]: row for row in v1["experiments"]}
    baseline = [
        experiments["compact-sequential-120rpm"],
        experiments["compact-parallel-120rpm"],
        experiments["cloud-compact-100"],
    ]
    complex_rows = {row["mode"]: row for row in v2["stages"]}
    modes = ("sequential", "parallel", "batch")
    if set(complex_rows) != set(modes):
        raise ValueError("The matched v2 comparison needs all three unique modes")
    model1, model2, model3 = baseline[0]["settings"], v2["model"], v3["configured_model"]
    if any(
        row["settings"]["model_name"] != "gpt-5.4-mini"
        or row["settings"]["model_version"] != "2026-03-17"
        for row in baseline
    ):
        raise ValueError("V1 model identity differs from the comparison story")
    if (
        model3["model_version"] != "2025-08-07"
        or model3["max_output_tokens"] != 2048
        or model3["reasoning_effort"] != "low"
    ):
        raise ValueError("V3 model or generation parameters differ from the comparison story")
    if model2["reasoning_effort"] != "low":
        raise ValueError("V2 reasoning setting differs from the comparison story")
    for row in baseline + list(complex_rows.values()):
        ratio(row)
    if v2["configured_client_limits"] != {"rpm": 120, "tpm": 480000}:
        raise ValueError("V2 pacing differs from the displayed matched experiment")

    def detail(main: str, explanation: str = "") -> str:
        return main + (f'<span class="cell-detail">{explanation}</span>' if explanation else "")

    def lines(items: list[str]) -> str:
        return '<ul class="cell-lines">' + "".join(f"<li>{item}</li>" for item in items) + "</ul>"

    def row(label: str, cells: list[str]) -> str:
        if len(cells) != 3:
            raise ValueError("Historical comparison rows must contain three version values")
        return (
            f'<tr><th scope="row">{html.escape(label)}</th>'
            + "".join(f"<td>{cell}</td>" for cell in [*cells, v4_cells[label], v5_cells[label]])
            + "</tr>"
        )

    def group(title: str) -> str:
        return (
            f'<tr class="group-row"><td colspan="6"><strong>{html.escape(title)}</strong></td></tr>'
        )

    def stage_lines(rows: list[dict[str, Any]], field: str, formatter: Any) -> str:
        labels = ["Sequential", "Parallel, 5 workers", "Service Batch"]
        return lines(
            [
                f"<strong>{label}:</strong> {formatter(result.get(field))}"
                for label, result in zip(labels, rows, strict=True)
            ]
        )

    v2_order = [complex_rows[mode] for mode in modes]
    v3_order = [
        next(row for row in v3["runs"] if row["mode"] == mode)
        for mode in ("sequential", "client-batch")
    ]
    v3_final = v3["results_final"]
    phase = (
        "Completed"
        if v3_final
        else "Interrupted / incomplete"
        if v3["execution_status"] == "interrupted"
        else "In progress / incomplete"
    )
    v3_progress = lines(
        [
            f"<strong>{label}:</strong> {count(result.get('succeeded'))} / {count(result['record_count'])} schema-valid; {html.escape(result['status'].replace('_', ' '))}"
            for label, result in zip(
                ["Sequential", "Five fixed client groups"], v3_order, strict=True
            )
        ]
    )
    v3_429 = lines(
        [
            f"<strong>{label}:</strong> {count(result.get('errors_429'))}"
            for label, result in zip(
                ["Sequential", "Five fixed client groups"], v3_order, strict=True
            )
        ]
    )
    v3_final_time = (
        lines(
            [
                f"<strong>{label}:</strong> {seconds(result.get('elapsed_seconds'))}"
                for label, result in zip(
                    ["Sequential", "Five fixed client groups"], v3_order, strict=True
                )
            ]
        )
        if v3_final
        else detail(
            "<strong>Not available</strong>",
            "No final sequential-versus-grouped duration comparison. Elapsed time of an incomplete checkpoint is not a completed benchmark.",
        )
    )
    v3_quality = (
        lines(
            [
                f"<strong>{label}:</strong> {percent((result.get('quality') or {}).get('field_accuracy', {}).get('primary_route'))} primary-route accuracy"
                for label, result in zip(
                    ["Sequential", "Five fixed client groups"], v3_order, strict=True
                )
            ]
        )
        if v3_final
        else detail(
            "<strong>Final comparison withheld</strong>",
            "Do not rank models using partial quality counts or unstarted grouped work.",
        )
    )
    v3_cost = (
        lines(
            [
                f"<strong>{label}:</strong> {money(result.get('priced_usage_estimate_usd'))}"
                for label, result in zip(
                    ["Sequential", "Five fixed client groups"], v3_order, strict=True
                )
            ]
        )
        if v3_final
        else detail(
            "<strong>Not final</strong>",
            f"Conservative all-attempt plan: {money(v3['conservative_all_attempts_estimate_usd'], 2)}. A planned ceiling is not measured spend.",
        )
    )
    model_cells = [
        f"<strong>GPT-5.4 mini</strong><br><code>{html.escape(model1['model_version'])}</code>",
        f"<strong>GPT-5.4 mini</strong><br><code>{html.escape(model2['version'])}</code>",
        f"<strong>GPT-5 mini</strong><br><code>{html.escape(model3['model_version'])}</code>",
    ]
    prompt_cells = [
        detail(
            "Short classification prompt",
            "Separate high-token stress profile used synthetic reference padding, not the active complex taxonomy.",
        ),
        detail(
            "Active complex-v2 Markdown",
            "425 hierarchical routes, 850 examples, and evidence/confidence rules.",
        ),
        detail(
            "<strong>Identical v2 prompt/schema bundle</strong>",
            "Verified by the same bundle hash; no customer text is used.",
        ),
    ]
    count_cells = [
        detail(
            "100 records in the matched comparison",
            "Separate 6-record token bursts and 1,700 compact-record recovery test.",
        ),
        detail(
            f"{count(v2['dataset']['count'])} records per mode",
            "One seeded case per scenario family; not all 425 routes covered.",
        ),
        detail(
            f"{count(v3['specification']['count'])} records per mode",
            "Same dataset twice; second stage is five contiguous groups of 340.",
        ),
    ]
    mode_cells = [
        "Sequential online; 5-worker parallel online; <strong>actual Azure Batch API</strong>.",
        "Sequential online; 5-worker parallel online; <strong>actual Azure Batch API</strong>.",
        "Sequential online; <strong>5 fixed client-side online groups</strong>. Not Azure Batch API.",
    ]
    service_cells = [
        detail(
            "150K TPM / 150 RPM for compact online",
            "Separate high-token capacity experiment: 150K &rarr; 600K TPM. Service Batch has separate quota.",
        ),
        detail(
            "600K TPM / 600 RPM online",
            "Confirmed response limits. Service Batch has separate enqueued-token quota.",
        ),
        detail(
            f"{count(v3['specification']['expected_tpm'])} TPM / {count(v3['specification']['expected_rpm'])} RPM",
            "One DataZoneStandard deployment shared by all five groups; an explicit test assumption.",
        ),
    ]
    pacing_cells = [
        detail(
            "Shared client admission control",
            "Compact comparison at 120 RPM; additional 60 RPM control. Bounded stress bursts deliberately unpaced.",
        ),
        detail(
            f"{count(v2['configured_client_limits']['tpm'])} TPM / {count(v2['configured_client_limits']['rpm'])} RPM client limits",
            "Paced online workers; 65-second cooldowns between stages.",
        ),
        detail(
            "<strong>Initial attempts deliberately unpaced</strong>",
            "Up to 3 attempts with service-guided backoff; at most 5,100 attempts and 10 hours per mode.",
        ),
    ]
    output_cells = [
        f"{count(model1['max_output_tokens'])} output tokens; low reasoning",
        f"{count(model2['max_output_tokens'])} output tokens; low reasoning",
        f"{count(model3['max_output_tokens'])} output tokens; low reasoning",
    ]
    input_v2 = "&ndash;".join(count(n) for n in v2["estimated_input_token_range"])
    input_v1 = "&ndash;".join(
        count(n) for n in baseline[0]["reported_input_tokens_per_response_range"]
    )
    input_cells = [
        detail(
            f"{input_v1} API-reported tokens/compact record",
            "Separate large-input bursts: approximately 72K; calibrated profile: approximately 74.6K estimated.",
        ),
        detail(
            f"{input_v2} estimated tokens/record",
            "Active taxonomy and rules, not filler. API-reported input is lower.",
        ),
        detail(
            "Approximately 32.3K&ndash;32.6K estimated tokens/record",
            "Same active taxonomy as v2; transcript variation spans 1,700 records.",
        ),
    ]
    v4_stages = {stage["arm"]: stage for stage in v4["stages"]}

    def v4_lines(field: str, formatter: Any) -> str:
        return lines(
            [
                f"<strong>{label}:</strong> {formatter(v4_stages.get(arm, {}).get(field))}"
                for arm, label in (("single", "One deployment"), ("pool", "Three deployments"))
            ]
        )

    v4_cells = {
        "Application release": f'<a href="{REPO}/releases/tag/v0.4.0">0.4.0</a>',
        "Exact GPT model / build": "<strong>GPT-5 mini</strong><br><code>2025-08-07</code>",
        "Question being tested": "Does splitting the same total quota across three deployments improve a fresh five-worker run?",
        "Prompt workload": detail(
            "<strong>Identical v2 prompt/schema bundle</strong>",
            "425 routes, 850 examples, and the same eleven-field rules.",
        ),
        "Input volume": "Approximately 32.3K&ndash;32.6K estimated tokens/record; same complex taxonomy.",
        "Dataset size": detail(
            "65 records per arm",
            "Five repetitions of 13 scenario families. The 1,700-record scenario is provided, not executed.",
        ),
        "Output contract": "The same eleven-field schema and gold-check rules as v2/v3.",
        "Processing modes": "<strong>Five online workers in each arm.</strong> Single deployment versus a three-deployment router; not Azure Batch API.",
        "Service allocation": detail(
            "<strong>150K TPM / 150 RPM in each arm</strong>",
            "Single: 1 &times; 150K. Pool: 3 &times; 50K. Arms run serially; no extra aggregate quota in the pool arm.",
        ),
        "Client pacing / retries": detail(
            "Shared per-target cooldown; round robin among eligible targets",
            "No proactive TPM pacing in this observation test. Four total attempts across targets; service headers first, 30-second exponential fallback plus jitter.",
        ),
        "Generation settings": "2,048 output tokens; low reasoning; 90-second request timeout.",
        "Azure SKU / geography": "DataZoneStandard only; US Data Zone; South Central US resources.",
        "Actual Azure Batch API": "<strong>Not used.</strong> Durable execution hosts online inference; it does not turn requests into a Batch job.",
        "Execution host": "Azure Durable Functions / Scheduler for both arms; private Blob intents/outcomes and Table metadata.",
        "Evidence state": detail(
            tag("Pilot complete" if v4["results_final"] else "Pilot incomplete", "attention"),
            utc_label(v4["observed_at"]),
        ),
        "Full observed duration": v4_lines("elapsed_seconds", seconds)
        if v4["results_final"]
        else "Final comparison withheld.",
        "Schema-valid processing": v4_lines("succeeded", lambda n: count(n) + " / 65"),
        "HTTP 429 attempts": v4_lines("errors_429", count),
        "Semantic accuracy": v4_lines(
            "quality", lambda q: percent((q or {}).get("field_accuracy", {}).get("primary_route"))
        ),
        "Semantic acceptance gate": v4_lines(
            "gate",
            lambda gate: html.escape((gate or {}).get("status", "not_evaluated").replace("_", " ")),
        ),
        "Reported model-usage estimate": v4_lines("priced_usage_estimate_usd", money),
        "Cache and cost caveat": "Single runs first, pool second after 65 seconds. Cache/service conditions may differ; there is no Batch discount. Infrastructure excluded.",
        "What demonstrably worked": "Inspect both completed-record and quality gates. Per-target responses, 429s, token use, and durable waits are recorded.",
        "Main takeaway": "<strong>Deployment count is not aggregate capacity.</strong> A matched test, not a promise of zero 429s.",
        "Main result": render_v4(v4)["V4_VERDICT"],
    }
    v4_cells["Exact GPT model"] = v4_cells["Exact GPT model / build"]
    v5_stages = {stage["arm"]: stage for stage in v5["stages"]}

    def v5_lines(field: str, formatter: Any) -> str:
        return lines(
            [
                f"<strong>{label}:</strong> {formatter(v5_stages.get(arm, {}).get(field))}"
                for arm, label in (("reactive", "Reactive"), ("adaptive", "Quota-aware"))
            ]
        )

    v5_cells = {
        "Application release": f'<a href="{REPO}/releases/tag/v0.5.0">0.5.0</a>',
        "Exact GPT model / build": "<strong>GPT-5 mini</strong><br><code>2025-08-07</code>",
        "Question being tested": "Does proactive admission and capacity-aware selection improve useful throughput versus reactive routing on the SAME five targets?",
        "Prompt workload": "<strong>Identical complex-v2 Markdown/schema.</strong> No prompt reduction or relaxed gold checks.",
        "Input volume": "Approximately 32.3K&ndash;32.6K estimated input tokens/record.",
        "Dataset size": "<strong>520 records per arm</strong>; 40 repetitions of 13 scenario families, plus five separate warm-ups per arm.",
        "Output contract": "The same eleven-field schema; a separate twelve-column application CSV includes the record ID.",
        "Processing modes": "V4-style reactive policy versus quota-aware policy in a <strong>shared V5 durable queue</strong>; five workers in both.",
        "Service allocation": "<strong>300K TPM / 300 RPM in BOTH arms</strong>: one 100K target plus four 50K targets. Historical V4 used 150K per arm.",
        "Client pacing / retries": "Adaptive arm reserves request/token capacity at 95% headroom with bounded header feedback; reactive arm uses cooldowns. Four total attempts per record.",
        "Generation settings": "2,048 output tokens; low reasoning; 90-second HTTP timeout.",
        "Azure SKU / geography": "DataZoneStandard only; US Data Zone; South Central US resources.",
        "Actual Azure Batch API": "<strong>Not used.</strong> Both are online calls; no Batch discount.",
        "Execution host": "Azure Durable Functions / Scheduler; shared queue, process-local connection/manifest reuse, and private paid-intent checkpoints.",
        "Evidence state": detail(
            tag("Paired run complete" if v5["results_final"] else "Results not final", "attention"),
            utc_label(v5["observed_at"]),
        ),
        "Full observed duration": v5_lines("elapsed_seconds", seconds)
        if v5["results_final"]
        else "Not final.",
        "Schema-valid processing": v5_lines("succeeded", lambda n: count(n) + " / 520"),
        "HTTP 429 attempts": v5_lines("errors_429", count),
        "Semantic accuracy": v5_lines(
            "quality", lambda q: percent((q or {}).get("field_accuracy", {}).get("primary_route"))
        ),
        "Semantic acceptance gate": v5_lines(
            "gate",
            lambda gate: html.escape((gate or {}).get("status", "not_evaluated").replace("_", " ")),
        ),
        "Reported model-usage estimate": v5_lines("priced_usage_estimate_usd", money),
        "Cache and cost caveat": "Five warm-ups per arm and 65-second pauses, reported separately. Same pool; reactive runs first. Cache/traffic variability remains.",
        "What demonstrably worked": "Inspect reconciliation, latency means with denominators, per-target behavior, and independent semantic gates.",
        "Main takeaway": "<strong>Measure successful throughput and actual request completion, not just deployment count.</strong>",
        "Main result": render_v5(v5)["V5_VERDICT"],
    }
    v5_cells["Exact GPT model"] = v5_cells["Exact GPT model / build"]
    mean_fields = [
        ("Average successful HTTP call", "http_200_latency_mean_seconds"),
        ("Average successful record processing", "successful_record_elapsed_mean_seconds"),
        ("Average all-record processing", "all_record_elapsed_mean_seconds"),
        ("Average enqueue-to-record completion", "queue_to_completion_mean_seconds"),
    ]
    for label, field in mean_fields:
        v4_cells[label] = (
            v4_lines(field, lambda n: seconds(n) if n is not None else "Not recorded")
            if v4["results_final"]
            else "Not final."
        )
        v5_cells[label] = v5_lines(field, seconds) if v5["results_final"] else "Not final."
    config = [
        group("01 / Model identity and experiment design"),
        row(
            "Application release",
            [
                f'<a href="{REPO}/releases/tag/v{version}">{version}</a>'
                for version in ("0.1.1", "0.2.0", "0.3.0")
            ],
        ),
        row("Exact GPT model / build", model_cells),
        row(
            "Question being tested",
            [
                "How do client pacing, service allocation, and execution mode affect throughput?",
                "Can the same model follow complex taxonomy and evidence rules while processing reliably?",
                "What happens with GPT-5 mini and sequential versus five fixed online streams at 150K TPM?",
            ],
        ),
        row("Prompt workload", prompt_cells),
        row("Input volume", input_cells),
        row("Dataset size", count_cells),
        row(
            "Output contract",
            [
                "3 fields: category, priority, summary.",
                "11 analytical fields; application adds the ID to CSV.",
                "Exactly the same 11-field schema and gold-check rules as v2.",
            ],
        ),
        group("02 / Capacity, execution, and generation"),
        row("Processing modes", mode_cells),
        row("Service allocation", service_cells),
        row("Client pacing / retries", pacing_cells),
        row("Generation settings", output_cells),
        row(
            "Azure SKU / geography",
            [
                "DataZoneStandard + DataZoneBatch; US Data Zone; South Central US resources.",
                "DataZoneStandard + DataZoneBatch; US Data Zone; South Central US resources.",
                "DataZoneStandard only; US Data Zone; South Central US resource.",
            ],
        ),
        row(
            "Actual Azure Batch API",
            [
                "<strong>Tested successfully</strong> on GPT-5.4 mini.",
                "<strong>Tested successfully</strong> on GPT-5.4 mini, including complex prompts.",
                "<strong>Not used.</strong> GPT-5 mini DataZoneBatch was not listed in the checked US catalogs. Pricing meters alone are not availability proof.",
            ],
        ),
        row(
            "Execution host",
            [
                "Local online coordinator; Azure Durable Functions / Scheduler for hosted Batch.",
                "Local online coordinator; Azure Durable Functions / Scheduler for hosted Batch.",
                "Local client runner for both modes; SQLite checkpoints. The model executes in Azure.",
            ],
        ),
    ]
    outcomes = [
        group("03 / Completion and observed performance"),
        row(
            "Evidence state",
            [
                detail(tag("Measured"), html.escape(v1["measured_date_utc"])),
                detail(
                    tag("Measured; quality failed", "attention"),
                    html.escape(v2["measured_date_utc"]),
                ),
                detail(
                    tag(phase, "attention" if not v3_final else ""), utc_label(v3["observed_at"])
                ),
            ],
        ),
        row(
            "Full observed duration",
            [
                stage_lines(baseline, "full_observed_elapsed_seconds", seconds),
                stage_lines(v2_order, "full_observed_elapsed_seconds", seconds),
                v3_final_time,
            ],
        ),
        row(
            "Schema-valid processing",
            [
                stage_lines(baseline, "succeeded", lambda n: count(n) + " / 100"),
                stage_lines(v2_order, "succeeded", lambda n: count(n) + " / 13"),
                v3_progress,
            ],
        ),
        row(
            "HTTP 429 attempts",
            [
                detail(
                    "0 in all three compact-comparison modes",
                    "Separate large-token, 5-worker burst: 3/6 throttled at 150K TPM; 0/6 at 600K TPM.",
                ),
                "0 across all 39 responses in the matched complex comparison.",
                detail(
                    v3_429,
                    "Partial while incomplete. An attempted request can throttle and later recover; 429 count is not final failed-record count.",
                ),
            ],
        ),
        *[
            row(
                label,
                [
                    stage_lines(
                        baseline, field, lambda n: seconds(n) if n is not None else "Not recorded"
                    ),
                    stage_lines(
                        v2_order, field, lambda n: seconds(n) if n is not None else "Not recorded"
                    ),
                    "Not recorded in this published evidence." if v3_final else "Not final.",
                ],
            )
            for label, field in mean_fields
        ],
        group("04 / Quality, costs, and the practical lesson"),
        row(
            "Semantic accuracy",
            [
                stage_lines(baseline, "label_accuracy", percent)
                + '<span class="cell-detail">Simple category label only; not the complex v2/v3 rubric.</span>',
                lines(
                    [
                        f"<strong>{label}:</strong> {percent(r['quality']['field_accuracy']['primary_route'])} primary route"
                        for label, r in zip(
                            ["Sequential", "Parallel", "Service Batch"], v2_order, strict=True
                        )
                    ]
                )
                + '<span class="cell-detail">Every-field/rule pass: 1/13 in each mode.</span>',
                v3_quality,
            ],
        ),
        row(
            "Semantic acceptance gate",
            [
                "No equivalent complex eleven-field gate; do not compare this score directly with v2/v3.",
                "<strong>Failed in all three modes.</strong> Schema success did not imply correct routes, causes, or confidence behavior.",
                "See final per-mode quality report; completion alone is not acceptance."
                if v3_final
                else "<strong>Not final.</strong> Complete the experiment and evaluate each mode before drawing a conclusion.",
            ],
        ),
        row(
            "Reported model-usage estimate",
            [
                stage_lines(baseline, "priced_usage_estimate_usd", money),
                stage_lines(v2_order, "priced_usage_estimate_usd", money),
                v3_cost,
            ],
        ),
        row(
            "Cache and cost caveat",
            [
                "Compact rows had no reported cache hits; large-input profiles had different cache behavior. Infrastructure excluded.",
                "Cached input differed by mode. Sequential ran first; cooldowns do not guarantee a cold cache. Infrastructure excluded.",
                "The grouped mode may inherit a warmed prefix. No Batch API discount applies to client-side groups. Infrastructure excluded.",
            ],
        ),
        row(
            "What demonstrably worked",
            [
                "Paced parallel compact processing and real Batch reconciliation. Recovery retained the original 1,700-record Batch job.",
                "Transport, schema validation, and reconciliation completed; quality checks exposed mistakes rather than hiding them.",
                "The intended model and 150K limits were verified; partial requests and 429s were recorded. Full comparison remains incomplete."
                if not v3_final
                else "Model, allocation, and both stage outcomes are recorded; judge success against the final processing and quality checks.",
            ],
        ),
        row(
            "Main takeaway",
            [
                "<strong>Capacity and pacing come before adding workers.</strong>",
                "<strong>Fewer 429s do not make complex answers correct.</strong>",
                "<strong>Five client groups do not create five model quotas.</strong>",
            ],
        ),
    ]
    summary = "".join(
        [
            row("Exact GPT model", model_cells),
            row("Prompt workload", prompt_cells),
            row("Dataset size", count_cells),
            row("Processing modes", mode_cells),
            row("Service allocation", service_cells),
            row(
                "Main result",
                [
                    "Compact modes completed; larger allocation reduced throttling in a bounded burst.",
                    "All processing completed; all semantic gates failed.",
                    detail(
                        phase,
                        "No final model or grouping speed comparison."
                        if not v3_final
                        else "Read both processing and semantic outcomes.",
                    ),
                ],
            ),
        ]
    )
    head = (
        '<tr><th scope="col">Variable</th>'
        f'<th scope="col"><span>V1 / BASELINE</span>GPT-5.4 mini<small>{html.escape(model1["model_version"])}</small></th>'
        f'<th scope="col"><span>V2 / COMPLEXITY</span>GPT-5.4 mini<small>{html.escape(model2["version"])}</small></th>'
        f'<th scope="col"><span>V3 / GROUPED REQUESTS</span>GPT-5 mini<small>{html.escape(model3["model_version"])}</small></th>'
        '<th scope="col"><span>V4 / MATCHED QUOTA</span>GPT-5 mini<small>2025-08-07</small></th>'
        '<th scope="col"><span>V5 / SMART ROUTING</span>GPT-5 mini<small>2025-08-07</small></th></tr>'
    )
    seq, grouped = v3_order
    story = (
        (
            f"The latest checkpoint records <strong>{count(seq.get('succeeded'))} / {count(seq['record_count'])}</strong> "
            f"schema-valid sequential records and <strong>{count(seq.get('errors_429'))} 429 attempts</strong>. "
            + (
                "The run is interrupted and incomplete; the grouped comparison has no final result."
                if v3["execution_status"] == "interrupted"
                else "The comparison is incomplete; final grouped performance is not established."
            )
        )
        if not v3_final
        else (
            f"Sequential and five-group modes are terminal, with <strong>{count(seq.get('succeeded'))}</strong> and "
            f"<strong>{count(grouped.get('succeeded'))}</strong> schema-valid records. Read the separate quality and retry outcomes before judging success."
        )
    )
    return {
        "MODEL_54_VERSION": html.escape(model2["version"]),
        "MODEL_5_VERSION": html.escape(model3["model_version"]),
        "V1_SEQ_TIME": seconds(baseline[0]["full_observed_elapsed_seconds"]),
        "V1_PAR_TIME": seconds(baseline[1]["full_observed_elapsed_seconds"]),
        "COMPARISON_HEAD": head,
        "SUMMARY_ROWS": summary,
        "CONFIGURATION_ROWS": "".join(config),
        "OUTCOME_ROWS": "".join(outcomes),
        "V3_STORY": story,
        "V3_CONCLUSION": "The incomplete evidence cannot establish final grouped performance or model superiority."
        if not v3_final
        else "The terminal outcomes describe this particular workload and allocation, not a universal model ranking or scaling guarantee.",
    }


def resource_cards() -> str:
    groups = [
        (
            "Get started & download",
            [
                ("Source repository", REPO, "Code, README, and complete source history"),
                (
                    "V1 stable starter",
                    f"{REPO}/releases/tag/v0.1.1",
                    "Original compact/filler baseline",
                ),
                (
                    "V2 complex workload",
                    f"{REPO}/releases/tag/v0.2.0",
                    "Markdown prompts and semantic quality testing",
                ),
                (
                    "V3 GPT-5 mini experiment",
                    f"{REPO}/releases/tag/v0.3.0",
                    "Release status and downloadable assets",
                ),
                (
                    "V4 matched-quota router",
                    f"{REPO}/releases/tag/v0.4.0",
                    "Cloud-hosted single-versus-three deployment pilot",
                ),
                (
                    "V5 quota-aware scheduling",
                    f"{REPO}/releases/tag/v0.5.0",
                    "Five deployments, 520-record arms, and average request timing",
                ),
                (
                    "Azure deployment tutorial",
                    f"{REPO}/blob/main/docs/azure-demo.md",
                    "Sign-in, availability, cost, deployment, and cleanup",
                ),
            ],
        ),
        (
            "Read the evidence",
            [
                (
                    "V1 measured report",
                    f"{REPO}/blob/main/docs/live-validation.md",
                    "Compact, high-token, scale, and recovery findings",
                ),
                (
                    "V2 measured report",
                    f"{REPO}/blob/main/docs/complex-v2-validation.md",
                    "Performance and failed semantic acceptance gates",
                ),
                (
                    "V3 status and results",
                    f"{REPO}/blob/main/docs/v3-results.md",
                    "Timestamped progress versus terminal evidence",
                ),
                (
                    "V3 partial output examples",
                    "data/v3-partial-output-sample.json",
                    "First three completed sequential records only",
                ),
                (
                    "V4 measured report",
                    f"{REPO}/blob/main/docs/v4-results.md",
                    "Matched allocation, per-target observations, and quality",
                ),
                (
                    "V4 complete pilot metrics",
                    "data/v4-results.json",
                    "Public aggregate measurements, not raw cloud logs",
                ),
                (
                    "V4 synthetic output examples",
                    "data/v4-output-samples.json",
                    "First three output records from each arm",
                ),
                (
                    "V5 measured report",
                    f"{REPO}/blob/main/docs/v5-results.md",
                    "Matched policies, measured means, and explicit timing denominators",
                ),
                (
                    "V5 aggregate evidence",
                    "data/v5-results.json",
                    "Benchmark and warm-up accounting kept separate",
                ),
                (
                    "V5 synthetic outcomes",
                    "data/v5-output-samples.json",
                    "Actual examples, not generated substitutes for missing results",
                ),
                (
                    "Measurement definitions",
                    f"{REPO}/blob/main/docs/metrics.md",
                    "Wall time, retry time, token usage, and quality denominators",
                ),
            ],
        ),
        (
            "Inspect the actual prompts",
            [
                ("Simple baseline prompt", "prompts/simple-v1.md", "Original short instructions"),
                (
                    "Complex system instructions",
                    "prompts/complex-system-v2.md",
                    "Evidence, confidence, and eleven-field rules",
                ),
                (
                    "425-route taxonomy",
                    "prompts/complex-taxonomy-v2.md",
                    "Active catalog and 850 examples",
                ),
                (
                    "Complex user instructions",
                    "prompts/complex-user-v2.md",
                    "Per-record task instructions",
                ),
                (
                    "Prompt and gold-label design",
                    f"{REPO}/blob/main/docs/complex-prompts.md",
                    "Synthetic case families, withholding labels, and scoring limits",
                ),
            ],
        ),
        (
            "Run a reproducible scenario",
            [
                (
                    "V2 thirteen-case smoke matrix",
                    f"{REPO}/blob/main/scenarios/complex-v2-smoke.json",
                    "Sequential, parallel, and actual Azure Batch",
                ),
                (
                    "V2 concurrency scenario",
                    f"{REPO}/blob/main/scenarios/complex-v2-concurrency.json",
                    "Larger scenario; not run in the published v2 validation",
                ),
                (
                    "V3 1,700-record scenario",
                    f"{REPO}/blob/main/scenarios/v3-gpt5-mini-1700.json",
                    "Sequential versus fixed five-by-340 online streams",
                ),
                (
                    "Bounded load-testing guide",
                    f"{REPO}/blob/main/docs/load-testing.md",
                    "Quota-aware pacing and explicit stress observation",
                ),
                (
                    "V3 operations guide",
                    f"{REPO}/blob/main/docs/v3-gpt5-mini.md",
                    "Separate deployment, checkpoints, resume, and exports",
                ),
                (
                    "V4 step-by-step guide",
                    f"{REPO}/blob/main/docs/v4-multi-deployment.md",
                    "Deploy, preview, start once, monitor, and export",
                ),
                (
                    "V4 65-record pilot scenario",
                    f"{REPO}/blob/main/scenarios/v4-matched-quota-pilot.json",
                    "Single and pool arms with matched aggregate quota",
                ),
                (
                    "V4 1,700-record scenario",
                    f"{REPO}/blob/main/scenarios/v4-matched-quota-1700.json",
                    "Provided but not executed; review costs before opting in",
                ),
                (
                    "V5 operations guide",
                    f"{REPO}/blob/main/docs/v5-adaptive.md",
                    "Topology checks, scheduling, average definitions, and safe monitoring",
                ),
                (
                    "V5 520-record scenario",
                    f"{REPO}/blob/main/scenarios/v5-adaptive-520.json",
                    "Two matched benchmark arms plus bounded warm-ups",
                ),
            ],
        ),
        (
            "Architecture & operations",
            [
                (
                    "Architecture and diagrams",
                    f"{REPO}/blob/main/docs/architecture.md",
                    "Client, cloud, identity, data flow, and boundaries",
                ),
                (
                    "Editable Excalidraw source",
                    f"{REPO}/blob/main/docs/architecture.excalidraw",
                    "Downloadable architecture source",
                ),
                (
                    "Durable framework and recovery",
                    f"{REPO}/blob/main/docs/orchestration.md",
                    "Official framework reuse and versioned execution",
                ),
                (
                    "Troubleshooting",
                    f"{REPO}/blob/main/docs/troubleshooting.md",
                    "Explicit errors, identity, network, and lifecycle issues",
                ),
                (
                    "Operations playbook",
                    f"{REPO}/blob/main/docs/operations.md",
                    "Resume, reconciliation, cancellation, and retention",
                ),
            ],
        ),
        (
            "Validate & customize",
            [
                (
                    "Tests and validation",
                    f"{REPO}/blob/main/docs/testing.md",
                    "Offline, browser, packaging, and opt-in Azure checks",
                ),
                ("GitHub Actions history", f"{REPO}/actions", "Actual CI and deployment results"),
                (
                    "Dashboard guide",
                    f"{REPO}/blob/main/docs/dashboard.md",
                    "Local and authenticated cloud metadata views",
                ),
                (
                    "Infrastructure templates",
                    f"{REPO}/tree/main/infra",
                    "Main environment and scoped v3/v4/v5 model templates",
                ),
                (
                    "Reusable implementation prompt",
                    f"{REPO}/blob/main/docs/build-prompt.md",
                    "Requirements, safeguards, and version extensions",
                ),
            ],
        ),
        (
            "Official Microsoft references",
            [
                (
                    "Azure OpenAI Batch API",
                    "https://learn.microsoft.com/azure/foundry/openai/how-to/batch",
                    "Service-managed asynchronous processing",
                ),
                (
                    "Quota and throttling",
                    "https://learn.microsoft.com/azure/foundry/openai/how-to/quota",
                    "Allocation and rate-limit concepts",
                ),
                (
                    "Understanding HTTP 429",
                    "https://learn.microsoft.com/microsoft-cloud/dev/dev-proxy/concepts/azure-openai-429-token-rate-limit",
                    "Rate-limit and temporary-capacity indications are not interchangeable",
                ),
                (
                    "Performance and latency",
                    "https://learn.microsoft.com/azure/foundry/openai/how-to/latency",
                    "Factors affecting online response time",
                ),
                (
                    "Durable Task Scheduler",
                    "https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler",
                    "Managed durable execution backend",
                ),
            ],
        ),
    ]
    cards = []
    for heading, items in groups:
        links = "".join(
            f'<li><a href="{html.escape(url, quote=True)}">{html.escape(label)}</a>'
            f"<small>{html.escape(description)}</small></li>"
            for label, url, description in items
        )
        cards.append(
            f'<article class="card"><h3>{html.escape(heading)}</h3><ul class="resource-list">{links}</ul></article>'
        )
    return "".join(cards)


class LinkAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            identifier = str(values["id"])
            if identifier in self.ids:
                raise ValueError(f"Duplicate HTML id: {identifier}")
            self.ids.add(identifier)
        for attribute in ("href", "src"):
            if values.get(attribute):
                self.links.append(str(values[attribute]))


def validate_links(document: str, output: Path, root: Path) -> None:
    audit = LinkAudit()
    audit.feed(document)
    for url in audit.links:
        if url.startswith("#"):
            if url[1:] not in audit.ids:
                raise ValueError(f"Broken section link: {url}")
        elif not urlparse(url).scheme:
            parsed = urlparse(url)
            target = (output / unquote(parsed.path)).resolve()
            if not target.is_relative_to(output.resolve()) or not target.is_file():
                raise ValueError(f"Missing site asset: {url}")
            if parsed.fragment and target.suffix == ".html":
                linked = LinkAudit()
                linked.feed(target.read_text(encoding="utf-8"))
                if unquote(parsed.fragment) not in linked.ids:
                    raise ValueError(f"Broken cross-page section link: {url}")
        elif url.startswith((REPO + "/blob/main/", REPO + "/tree/main/")):
            relative = url.split("/main/", 1)[1].split("#")[0]
            if not (root / unquote(relative)).exists():
                raise ValueError(f"Broken repository link: {url}")


def render_tokens(template: str, values: dict[str, str]) -> str:
    return re.sub(r"@@([A-Z0-9_]+)@@", lambda match: values[match[1]], template)


def build_site(root: Path = ROOT, output: Path | None = None) -> Path:
    output = output or root / "_site"
    allowed = {
        *PAGES,
        ".nojekyll",
        "assets/complex-dashboard-demo.png",
        *(f"data/{name}" for name in DATA_FILES),
        *(f"prompts/{name}" for name in PROMPTS),
    }
    if output.is_symlink():
        raise ValueError("Site output cannot be a symbolic link")
    if output.exists():
        for path in output.rglob("*"):
            if path.is_symlink() or (
                path.is_file() and path.relative_to(output).as_posix() not in allowed
            ):
                raise ValueError("Unexpected content in site output; inspect it before rebuilding")
    documents = {}
    for target, source in DATA_FILES.items():
        document = json.loads((root / "docs" / source).read_text(encoding="utf-8"))
        public_only(document, source)
        documents[target] = document
    v1, v2, v3, v4, v5 = (
        documents[name]
        for name in (
            "v1-results.json",
            "v2-results.json",
            "v3-results.json",
            "v4-results.json",
            "v5-results.json",
        )
    )
    if any(
        value["workload_profile"]["prompt_bundle_sha256"]
        != v2["workload_profile"]["prompt_bundle_sha256"]
        for value in (v3, v4, v5)
    ):
        raise ValueError(
            "The page's unchanged-prompt comparison requires identical v2/v3/v4/v5 bundle hashes"
        )
    experiments = {row["scenario"]: row for row in v1["experiments"]}
    compact = [
        ("Sequential online", experiments["compact-sequential-120rpm"]),
        ("Parallel online, 5 workers", experiments["compact-parallel-120rpm"]),
        ("Service-managed Batch", experiments["cloud-compact-100"]),
    ]
    if any(row["record_count"] != 100 for _, row in compact) or v2["dataset"]["count"] != 13:
        raise ValueError(
            "The page's matched-workload descriptions no longer match their source evidence"
        )
    if v2["acceptance_status"] != "needs_review" or len(v2["stages"]) != 3:
        raise ValueError("Update the v2 narrative before changing its measured acceptance result")
    if (
        v2["model"]["name"] != "gpt-5.4-mini"
        or v2["model"]["version"] != "2026-03-17"
        or v2["model"]["max_output_tokens"] != 2048
    ):
        raise ValueError("V2 model/generation settings differ from the page narrative")
    maximum = max(row["full_observed_elapsed_seconds"] for _, row in compact)
    compact_rows = []
    for name, row in compact:
        compact_rows.append(
            f'<tr><th scope="row">{html.escape(name)}</th>{duration_cell(row["full_observed_elapsed_seconds"], maximum)}'
            f'<td class="number">{ratio(row)}</td><td>{count(row["errors_429"])}</td>'
            f'<td>{percent(row["label_accuracy"])}</td><td class="number">{money(row["priced_usage_estimate_usd"])}</td></tr>'
        )
    capacity_rows = []
    for name, key in (
        ("150K TPM", "large-150k-tpm-concurrency-5"),
        ("600K TPM", "large-600k-tpm-concurrency-5"),
    ):
        row = experiments[key]
        capacity_rows.append(
            f'<tr><th scope="row">{name}</th><td>{ratio(row)}</td><td>{count(row["errors_429"])}</td></tr>'
        )
    scale = experiments["cloud-scale-1700-with-workflow-recovery"]
    if scale["record_count"] != 1700 or len(scale["partitions"]) != 1:
        raise ValueError("The v1 scale/recovery narrative differs from its evidence")
    all_rows = "".join(
        f'<tr><th scope="row">{html.escape(row["scenario"].replace("-", " "))}</th><td>{count(row["record_count"])}</td>'
        f'<td>{ratio(row)}</td><td class="number">{seconds(row["full_observed_elapsed_seconds"])}</td><td>{count(row["errors_429"])}</td></tr>'
        for row in v1["experiments"]
    )
    v2_rows = []
    modes = {
        "sequential": "Sequential online",
        "parallel": "Parallel online, 5 workers",
        "batch": "Azure-hosted Batch",
    }
    maximum = max(row["full_observed_elapsed_seconds"] for row in v2["stages"])
    for row in v2["stages"]:
        quality = row["quality"]
        if (
            quality["passed_records"] != 1
            or row["succeeded"] != 13
            or row["gate"]["status"] != "needs_review"
        ):
            raise ValueError("V2's fixed narrative must be reviewed when its source results change")
        v2_rows.append(
            f'<tr><th scope="row">{modes[row["mode"]]}</th>{duration_cell(row["full_observed_elapsed_seconds"], maximum)}'
            f"<td>{ratio(row)}</td><td>{count(row['errors_429'])}</td>"
            f"<td>{percent(quality['field_accuracy']['primary_route'])}</td>"
            f"<td>{percent(quality['field_accuracy']['confidence'])}</td>"
            f"<td>{count(quality['passed_records'])} / {count(row['record_count'])}</td></tr>"
        )
    sha = os.environ.get("GITHUB_SHA", "")
    source_url = f"{REPO}/tree/{sha}" if re.fullmatch(r"[a-f0-9]{40}", sha) else REPO
    replacements = {
        "REPO": REPO,
        "V1_DATE": html.escape(v1["measured_date_utc"]),
        "V1_COMPACT_ROWS": "".join(compact_rows),
        "V1_INPUT_RANGE": "&ndash;".join(
            count(item) for item in compact[0][1]["reported_input_tokens_per_response_range"]
        ),
        "V1_ALL_ROWS": all_rows,
        "V1_60_SEQ": seconds(experiments["compact-sequential-60rpm"]["elapsed_seconds"]),
        "V1_60_PAR": seconds(experiments["compact-parallel-60rpm"]["elapsed_seconds"]),
        "V1_CAPACITY_ROWS": "".join(capacity_rows),
        "V1_SCALE_SUCCESS": ratio(scale),
        "V1_SCALE_SERVICE": clock(scale["partitions"][0]["service_elapsed_seconds"]),
        "V1_SCALE_WALL": clock(scale["full_observed_elapsed_seconds"]),
        "V1_SCALE_ACCURACY": percent(scale["label_accuracy"]),
        "V2_DATE": html.escape(v2["measured_date_utc"]),
        "V2_ROWS": "".join(v2_rows),
        "V2_INPUT": count(v2["stages"][0]["input_tokens"]),
        "V2_COST": money(v2["reported_model_usage_estimate_usd"], 2),
        "RESOURCE_CARDS": resource_cards(),
        "SOURCE_URL": source_url,
        "DASHBOARD_IMAGE": "data:image/png;base64,"
        + base64.b64encode(
            (root / "docs" / "images" / "complex-dashboard-demo.png").read_bytes()
        ).decode("ascii"),
        "BUILD_TIME": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        **render_v3(v3),
        **render_v4(v4),
        **render_v5(v5),
        **compare_versions(v1, v2, v3, v4, v5),
    }
    layout = (root / "site" / "layout.html").read_text(encoding="utf-8")
    pages = {}
    filenames = list(PAGES)
    for index, (filename, (template_name, _nav_label, title, description)) in enumerate(
        PAGES.items()
    ):
        content = (root / "site" / "pages" / f"{template_name}.html").read_text(encoding="utf-8")
        navigation = "".join(
            f'<a href="{target}"'
            + (' aria-current="page"' if target == filename else "")
            + f">{html.escape(page[1])}</a>"
            for target, page in PAGES.items()
        )
        previous = filenames[index - 1] if index else None
        following = filenames[index + 1] if index + 1 < len(filenames) else "index.html"
        next_links = (
            f'<a href="{previous}">&larr; {html.escape(PAGES[previous][1])}</a>'
            if previous
            else '<a href="resources.html">Try the starter</a>'
        ) + f'<a href="{following}">{html.escape(PAGES[following][1])} &rarr;</a>'
        values = {
            **replacements,
            "PAGE_TITLE": html.escape(title),
            "PAGE_DESCRIPTION": html.escape(description, quote=True),
            "PAGE_FILE": filename,
            "NAVIGATION": navigation,
            "PAGE_NEXT": next_links,
        }
        values["PAGE_CONTENT"] = render_tokens(content, values)
        document = render_tokens(layout, values)
        if "@@" in document:
            raise ValueError(f"Unresolved site template marker in {filename}")
        pages[filename] = document
    output.mkdir(parents=True, exist_ok=True)
    (output / "data").mkdir(exist_ok=True)
    (output / "assets").mkdir(exist_ok=True)
    (output / "prompts").mkdir(exist_ok=True)
    for target, source in DATA_FILES.items():
        shutil.copyfile(root / "docs" / source, output / "data" / target)
    shutil.copyfile(
        root / "docs" / "images" / "complex-dashboard-demo.png",
        output / "assets" / "complex-dashboard-demo.png",
    )
    for name in PROMPTS:
        shutil.copyfile(root / "prompts" / name, output / "prompts" / name)
    for filename, document in pages.items():
        (output / filename).write_text(document, encoding="utf-8", newline="\n")
    for document in pages.values():
        validate_links(document, output, root)
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return output


if __name__ == "__main__":
    destination = build_site()
    print(
        f"Built {destination.name}: {len(PAGES)} real HTML pages, validated cross-page links, public evidence, and Markdown prompts."
    )
