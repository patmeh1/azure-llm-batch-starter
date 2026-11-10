import copy
import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
module_spec = importlib.util.spec_from_file_location(
    "site_builder", ROOT / "scripts" / "build-site.py"
)
site = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(site)


@pytest.fixture
def site_root(tmp_path):
    root = tmp_path / "source"
    for folder in ("docs", "prompts", "site", "scenarios", "infra"):
        shutil.copytree(ROOT / folder, root / folder)
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, root / name)
    return root


def test_build_contains_real_evidence_and_only_allowlisted_assets(site_root):
    output = site.build_site(site_root)
    document = (output / "index.html").read_text()
    assert "@@" not in document
    assert "90.34 s" in document
    assert "14.38 s" in (output / "comparison.html").read_text()
    version_two = (output / "v2.html").read_text()
    assert "All 39 responses were schema-valid. All three acceptance gates failed." in version_two
    assert "data:image/png;base64," in (output / "resources.html").read_text()
    assert "connect-src 'none'" in document
    assert {path.name for path in output.glob("*.html")} == set(site.PAGES)
    titles = set()
    for path in output.glob("*.html"):
        page = path.read_text()
        assert len(re.findall(r'<a\b[^>]*\baria-current="page"', page)) == 1
        assert page.count("<h1") == 1
        assert f'href="{path.name}" aria-current="page"' in page
        assert f'azure-llm-batch-starter/{path.name}"' in page
        titles.add(re.search(r"<title>(.*?)</title>", page)[1])
        assert "@@" not in page
    assert len(titles) == len(site.PAGES)
    assert len(list((output / "data").glob("*.json"))) == 6
    assert len(list((output / "prompts").glob("*.md"))) == 4
    assert not (output / ".azure").exists()
    assert not (output / "outputs").exists()
    site.build_site(site_root)
    assert (output / "index.html").exists()


def test_pending_v3_has_no_final_duration_or_invented_zeroes():
    value = json.loads((ROOT / "docs" / "v3-results.json").read_text())
    value = copy.deepcopy(value)
    value.update(results_final=False, execution_status="running")
    for row in value["runs"]:
        row.update(status="pending", succeeded=None, failed=None, pending=None, errors_429=None)
    result = site.render_v3(value)
    assert result["V3_FINAL"] == "false"
    assert "Final comparison results are not available" in result["V3_NOTICE"]
    assert result["V3_ROWS"].count("Not final") == 2
    assert "&mdash; / 1,700" in result["V3_ROWS"]
    assert "Final quality and latency comparisons are withheld" in result["V3_QUALITY"]


def test_terminal_v3_switches_to_final_metrics_without_rewriting_the_page():
    value = json.loads((ROOT / "docs" / "v3-results.json").read_text())
    value = copy.deepcopy(value)
    value.update(results_final=True, execution_status="completed_with_findings")
    for row in value["runs"]:
        row.update(
            status="succeeded",
            succeeded=1700,
            failed=0,
            pending=0,
            errors_429=10,
            elapsed_seconds=120,
            semantic_status="needs_review",
            quality={
                "field_accuracy": {"primary_route": 0.8, "confidence": 0.7},
                "passed_records": 300,
            },
        )
    result = site.render_v3(value)
    assert result["V3_FINAL"] == "true"
    assert "Both modes are terminal" in result["V3_NOTICE"]
    assert result["V3_ROWS"].count("120.00 s") == 2
    assert "Not final" not in result["V3_ROWS"]
    assert "300 / 1,700" in result["V3_QUALITY"]


def test_inconsistent_final_flag_is_rejected():
    value = json.loads((ROOT / "docs" / "v3-results.json").read_text())
    value.update(results_final=True, execution_status="running")
    with pytest.raises(ValueError, match="contradicts"):
        site.render_v3(value)


@pytest.mark.parametrize(
    "value",
    [
        {"endpoint": "redacted"},
        {"debug": "https://example.openai.azure.com"},
        {"debug": r"C:\private\file.json"},
        {"debug": "00000000-0000-0000-0000-000000000001"},
        {"cost": float("nan")},
    ],
)
def test_private_or_invalid_evidence_is_rejected(value):
    with pytest.raises(ValueError):
        site.public_only(value)


def test_public_repository_links_are_not_mistaken_for_windows_paths():
    site.public_only({"source": site.REPO})


def test_unexpected_output_file_is_not_deleted_or_published(site_root):
    output = site_root / "_site"
    output.mkdir()
    private = output / "unrelated.txt"
    private.write_text("Preserve this file.")
    with pytest.raises(ValueError, match="Unexpected content"):
        site.build_site(site_root)
    assert private.read_text() == "Preserve this file."


def test_escaped_scenario_text_cannot_inject_markup(site_root):
    source = site_root / "docs" / "live-results.json"
    value = json.loads(source.read_text())
    value["experiments"][-1]["scenario"] = "<img onerror=alert(1)>"
    source.write_text(json.dumps(value), encoding="utf-8")
    output = site.build_site(site_root)
    text = (output / "v1.html").read_text()
    assert "&lt;img onerror=alert(1)&gt;" in text
    assert "<img onerror=alert(1)>" not in text


def test_comparison_table_explains_model_identity_and_confounded_variables(site_root):
    output = site.build_site(site_root)
    page = (output / "comparison.html").read_text()
    assert page.count("2026-03-17") >= 4
    assert page.count("2025-08-07") >= 2
    assert "GPT-5.4 mini" in page and "GPT-5 mini" in page
    for label in (
        "Exact GPT model / build",
        "Prompt workload",
        "Input volume",
        "Dataset size",
        "Output contract",
        "Processing modes",
        "Service allocation",
        "Client pacing / retries",
        "Generation settings",
        "Azure SKU / geography",
        "Actual Azure Batch API",
        "Full observed duration",
        "Schema-valid processing",
        "HTTP 429 attempts",
        "Semantic accuracy",
        "Semantic acceptance gate",
        "Reported model-usage estimate",
    ):
        assert label in page
    assert "Do not attribute the differences to the GPT model alone." in page
    assert "1/13 in each mode." in page
    assert "145&ndash;153" in page
    assert "Final comparison withheld" in page
    assert "V4 / MATCHED QUOTA" in page
    assert "150K TPM / 150 RPM in each arm" in page
    assert 'colspan="5"' in page
    assert "Three deployments" in page


def test_final_comparison_does_not_keep_stale_incomplete_claims(site_root):
    source = site_root / "docs" / "v3-results.json"
    value = json.loads(source.read_text())
    value.update(results_final=True, execution_status="completed_with_findings")
    for stage in value["runs"]:
        stage.update(
            status="succeeded",
            succeeded=1700,
            failed=0,
            pending=0,
            elapsed_seconds=1234,
            errors_429=5,
            priced_usage_estimate_usd=1.5,
            quality={
                "field_accuracy": {"primary_route": 0.9, "confidence": 0.8},
                "passed_records": 1000,
            },
            semantic_status="needs_review",
        )
    source.write_text(json.dumps(value))
    output = site.build_site(site_root)
    comparison = (output / "comparison.html").read_text()
    assert "1,234.00 s" in comparison
    assert "Final comparison withheld" not in comparison
    assert "The incomplete evidence cannot establish" not in comparison
    assert "Both modes are terminal" in (output / "v3.html").read_text()


def test_cross_page_fragments_are_validated(site_root):
    template = site_root / "site" / "pages" / "overview.html"
    template.write_text(template.read_text() + '<a href="resources.html#missing">Broken</a>')
    with pytest.raises(ValueError, match="cross-page section"):
        site.build_site(site_root)


def test_model_change_cannot_silently_relabel_the_comparison(site_root):
    source = site_root / "docs" / "complex-v2-results.json"
    value = json.loads(source.read_text())
    value["model"]["name"] = "gpt-5-mini"
    source.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="model/generation"):
        site.build_site(site_root)


def test_interrupted_v3_does_not_claim_a_running_job():
    value = json.loads((ROOT / "docs" / "v3-results.json").read_text())
    value.update(
        results_final=False,
        execution_status="interrupted",
        last_activity_at="2026-11-09T22:05:28+00:00",
    )
    result = site.render_v3(value)
    assert "Run interrupted" in result["V3_STATUS"]
    assert "no active runner was observed" in result["V3_NOTICE"]
    assert "Nov 09, 2026 at 22:05 UTC" in result["V3_FRESHNESS"]


def test_v4_pending_evidence_withholds_final_metrics():
    value = json.loads((ROOT / "docs" / "v4-results.json").read_text())
    value.update(
        results_final=False,
        execution_status="running",
        stages=[],
        reported_model_usage_estimate_usd=None,
    )
    result = site.render_v4(value)
    assert result["V4_FINAL"] == "false"
    assert result["V4_ROWS"].count("Not final") == 2
    assert "&mdash; / 65" in result["V4_ROWS"]
    assert result["V4_WALL"] == "Not final"
    assert result["V4_COST"] == "Not available"
    assert "not measured, not zero" in result["V4_NOTICE"]


@pytest.mark.parametrize("field", ["allocated_tpm", "allocated_rpm"])
def test_v4_cannot_claim_matched_quota_after_allocation_changes(field):
    value = json.loads((ROOT / "docs" / "v4-results.json").read_text())
    value["stages"][1]["target_metrics"][0][field] *= 2
    with pytest.raises(ValueError, match="matched aggregate quota"):
        site.render_v4(value)


def test_v4_requires_reconciled_target_metrics_and_both_terminal_arms():
    value = json.loads((ROOT / "docs" / "v4-results.json").read_text())
    altered = copy.deepcopy(value)
    altered["stages"][0]["target_metrics"][0]["http_attempts"] += 1
    with pytest.raises(ValueError, match="attempt counts do not reconcile"):
        site.render_v4(altered)
    value["stages"].pop()
    with pytest.raises(ValueError, match="both arms"):
        site.render_v4(value)


def test_v4_cannot_present_partial_failure_as_processing_success():
    value = json.loads((ROOT / "docs" / "v4-results.json").read_text())
    value["processing_status"] = "succeeded"
    with pytest.raises(ValueError, match="processing status contradicts"):
        site.render_v4(value)


def test_v4_does_not_present_a_faster_failed_run_as_a_speedup():
    value = json.loads((ROOT / "docs" / "v4-results.json").read_text())
    value["stages"][0].update(succeeded=64, failed=1, elapsed_seconds=1000)
    value["stages"][1]["elapsed_seconds"] = 1
    result = site.render_v4(value)
    assert "shorter failed run is not a scaling improvement" in result["V4_VERDICT"]
    assert "x the validated-record throughput" not in result["V4_VERDICT"]


def test_v4_cannot_silently_change_model_or_prompt(site_root):
    path = site_root / "docs" / "v4-results.json"
    value = json.loads(path.read_text())
    altered = copy.deepcopy(value)
    altered["model"]["model_name"] = "gpt-5.4-mini"
    with pytest.raises(ValueError, match="pilot narrative"):
        site.render_v4(altered)
    value["workload_profile"]["prompt_bundle_sha256"] = "not-the-v2-prompt"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="v2/v3/v4 bundle hashes"):
        site.build_site(site_root)
