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
DATA_FILES = {
    "v1-results.json": "live-results.json",
    "v2-results.json": "complex-v2-results.json",
    "v3-results.json": "v3-results.json",
    "v3-partial-output-sample.json": "v3-partial-output-sample.json",
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
        notice = (
            f"<strong>{html.escape(label)}. Final comparison results are not available.</strong>"
            "<p>The sequential stage runs first; five fixed groups follow. Partial successes and 429 counts "
            "are progress observations, not final latency, throughput, or quality results.</p>"
        )
        caption = (
            "Progress only. Dashes mean not yet measured, not zero. Final durations are withheld."
        )
        quality_html = '<p class="small">Final quality and latency comparisons are withheld until both stages reconcile.</p>'
    progress = measured_done * 100 / (expected_total * 2)
    progress_html = (
        '<div class="card" style="margin-block:1.25rem"><div class="status-line">'
        f"<strong>{count(measured_done)} / {count(expected_total * 2)} records have terminal outcomes</strong>"
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
                    "Main environment and additive v3 model template",
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
            if not (output / unquote(url.split("#")[0])).is_file():
                raise ValueError(f"Missing site asset: {url}")
        elif url.startswith((REPO + "/blob/main/", REPO + "/tree/main/")):
            relative = url.split("/main/", 1)[1].split("#")[0]
            if not (root / unquote(relative)).exists():
                raise ValueError(f"Broken repository link: {url}")


def build_site(root: Path = ROOT, output: Path | None = None) -> Path:
    output = output or root / "_site"
    allowed = {
        "index.html",
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
    v1, v2, v3 = (
        documents[name] for name in ("v1-results.json", "v2-results.json", "v3-results.json")
    )
    if (
        v3["workload_profile"]["prompt_bundle_sha256"]
        != v2["workload_profile"]["prompt_bundle_sha256"]
    ):
        raise ValueError(
            "The page's unchanged-prompt comparison requires identical v2/v3 bundle hashes"
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
    }
    template = (root / "site" / "index.html").read_text(encoding="utf-8")
    document = re.sub(r"@@([A-Z0-9_]+)@@", lambda match: replacements[match[1]], template)
    if "@@" in document:
        raise ValueError("Unresolved site template marker")
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
    validate_links(document, output, root)
    (output / "index.html").write_text(document, encoding="utf-8", newline="\n")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    return output


if __name__ == "__main__":
    destination = build_site()
    print(
        f"Built {destination.name}: static HTML, allowlisted public evidence, Markdown prompts, and one simulated screenshot."
    )
