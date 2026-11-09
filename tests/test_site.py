import copy
import importlib.util
import json
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
    assert "90.34 s" in document and "14.38 s" in document
    assert "All 39 responses were schema-valid. All three acceptance gates failed." in document
    assert "data:image/png;base64," in document
    assert "connect-src 'none'" in document
    assert len(list((output / "data").glob("*.json"))) == 4
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
    text = (output / "index.html").read_text()
    assert "&lt;img onerror=alert(1)&gt;" in text
    assert "<img onerror=alert(1)>" not in text
