from __future__ import annotations

import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from llm_batch_starter.cli import app
from llm_batch_starter.complex_workload import (
    FIELDS,
    SCENARIOS,
    Expected,
    catalog,
    configure,
    generate_complex,
    render_catalog,
    schema,
)
from llm_batch_starter.config import DEFAULT_PROMPT, Settings
from llm_batch_starter.load_testing import Scenario, _finish_matrix, plan_scenario, quality_gate
from llm_batch_starter.pipeline import run_pipeline
from llm_batch_starter.prepare import (
    estimate_input_tokens,
    input_fingerprint,
    prepare_requests,
    request_body,
)
from llm_batch_starter.prompt_assets import read_prompt
from llm_batch_starter.quality import annotate_result
from llm_batch_starter.reconcile import validate_completion
from llm_batch_starter.reporting import analysis_csv
from llm_batch_starter.synthetic import Record, generate_dataset


def gold_result(record):
    expected = Expected.model_validate(record.expected_result)
    confidence = expected.minimum_confidence
    return {
        **copy.deepcopy(expected.fields),
        "confidence": confidence,
        "confidence_basis": expected.archived_basis
        if confidence == expected.archived_score
        else "The direct request and established evidence support this assessment.",
    }


def completion(record):
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"content": json.dumps(gold_result(record))}}
        ],
        "usage": {"prompt_tokens": 32000, "completion_tokens": 300, "total_tokens": 32300},
    }


def test_real_markdown_contains_all_active_routes_and_examples():
    text = read_prompt("complex-taxonomy-v2.md")
    assert len(catalog()) == len({route.code for route in catalog()}) == 425
    assert text == render_catalog().strip()
    assert text.count("Example A:") == text.count("Example B:") == 425
    settings, metadata = configure(Settings())
    assert text in settings.prompt
    assert "Do not ignore the catalog as padding" in settings.prompt
    assert metadata["taxonomy_routes"] == 425
    assert len(metadata["prompt_bundle_sha256"]) == 64
    assert DEFAULT_PROMPT == read_prompt("simple-v1.md")


def test_complex_schema_uses_one_shared_route_enum():
    value = schema()
    assert list(value["properties"]) == list(FIELDS)
    assert len(value["$defs"]["route"]["enum"]) == 426
    assert value["properties"]["primary_route"] == {"$ref": "#/$defs/route"}
    assert value["properties"]["secondary_routes"]["items"] == {"$ref": "#/$defs/route"}
    assert sum(len(str(item)) for item in value["$defs"]["route"]["enum"]) < 7500


def test_case_mix_and_generation_are_deterministic_without_gold_leakage():
    records = generate_complex(13)
    assert records == generate_complex(13)
    assert {record.expected_result["scenario"] for record in records} == set(SCENARIOS)
    settings, _ = configure(Settings())
    for record in records:
        body = json.dumps(request_body(record, settings, batch=False))
        assert "expected_result" not in body
        assert "minimum_confidence" not in body
        assert "archived_candidate" in body
    assert 30000 < estimate_input_tokens(records[0], settings) < 40000


def test_explicit_coverage_dataset_reaches_every_route():
    records = generate_complex(425, case_family="direct_need")
    assert {record.expected_result["fields"]["primary_route"] for record in records} == {
        route.code for route in catalog()
    }


def test_legacy_record_serialization_and_fingerprint_are_preserved():
    settings = Settings()
    records = generate_dataset(3) + [Record(id="unicode", text="caf\u00e9")]
    digest = hashlib.sha256(settings.fingerprint().encode())
    for record in records:
        legacy = {"id": record.id, "text": record.text, "expected_label": record.expected_label}
        serialized = json.dumps(legacy, ensure_ascii=False, separators=(",", ":"))
        assert record.model_dump_json() == serialized
        digest.update(serialized.encode())
        digest.update(b"\n")
    assert input_fingerprint(records, settings) == digest.hexdigest()


@pytest.mark.parametrize("family", SCENARIOS)
def test_gold_contracts_and_quality_checks_are_consistent(family):
    record = generate_complex(1, case_family=family)[0]
    outcome = validate_completion(completion(record), schema())
    annotate_result(record, outcome)
    assert outcome["status"] == "succeeded"
    assert outcome["quality"]["passed"]


@pytest.mark.parametrize(
    "fault", ["wrong_route", "lower_confidence", "canary", "too_many_secondary"]
)
def test_schema_success_does_not_mask_semantic_or_rule_failure(fault):
    record = generate_complex(1, case_family="stale_candidate")[0]
    result = gold_result(record)
    if fault == "wrong_route":
        result["primary_route"] = next(
            r.code for r in catalog() if r.code != result["primary_route"]
        )
    elif fault == "lower_confidence":
        result["confidence"] -= 1
    elif fault == "canary":
        result["confidence_basis"] += record.expected_result["forbidden_values"][0]
    else:
        result["secondary_routes"] = [r.code for r in catalog()[:3]]
    outcome = validate_completion(
        {
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(result)}}],
        },
        schema(),
    )
    assert outcome["status"] == "succeeded"
    annotate_result(record, outcome)
    assert not outcome["quality"]["passed"]


def test_complex_records_reject_the_simple_schema_before_inference():
    with pytest.raises(ValueError, match="complex-v2 output schema"):
        prepare_requests(generate_complex(1), Settings())


@pytest.mark.parametrize("mode", ["sequential", "parallel", "batch"])
def test_complex_pipeline_scores_results_and_exports_twelve_columns(tmp_path, mode):
    records = generate_complex(13)

    class Provider:
        def complete(self, record):
            return completion(record), {}

        def upload(self, payload, filename):
            self.ids = [json.loads(line)["custom_id"] for line in payload.splitlines()]
            return "file-test"

        def submit(self, file_id, submission_key):
            return {"id": "batch-test", "status": "validating"}

        def poll(self, job_id):
            return {"id": job_id, "status": "completed", "output_file_id": "result-test"}

        def download(self, file_id):
            by_id = {record.id: record for record in records}
            return "\n".join(
                json.dumps(
                    {
                        "custom_id": rid,
                        "response": {"status_code": 200, "body": completion(by_id[rid])},
                    }
                )
                for rid in reversed(self.ids)
            )

    settings, metadata = configure(
        Settings(
            rpm=100000,
            tpm=10_000_000,
            coordination_dir=str(tmp_path / "locks"),
        )
    )
    report = run_pipeline(
        records,
        settings,
        mode,
        tmp_path / "state.sqlite3",
        tmp_path / "runs",
        provider=Provider(),
        workload_profile=metadata,
    )
    assert report["succeeded"] == report["quality"]["passed_records"] == 13
    assert report["semantic_status"] == "passed"
    assert report["quality"]["fully_correct_fraction_of_all_records"] == 1
    directory = tmp_path / "runs" / report["id"]
    with (directory / "analysis.csv").open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames == ["id", *FIELDS]
        assert len(list(reader)) == 13
    assert (directory / "prompt.md").read_text().strip() == settings.prompt


def test_load_plan_contains_complex_tokens_and_retry_budget_without_execution():
    specification = Scenario.model_validate_json(
        (Path(__file__).parents[1] / "scenarios" / "complex-v2-smoke.json").read_text()
    )
    plan = plan_scenario(specification, Settings())
    assert plan["status"] == "planned_not_executed"
    assert plan["estimated_input_token_range"][0] > 30000
    assert (
        plan["maximum_planned_inference_estimate_usd"] < specification.max_estimated_inference_usd
    )
    with pytest.raises(ValueError, match="matrix exceeds"):
        plan_scenario(
            specification.model_copy(update={"max_estimated_inference_usd": 0.01}), Settings()
        )


def test_load_cli_defaults_to_dry_run(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A preview must not submit or authenticate")

    monkeypatch.setattr("llm_batch_starter.load_testing.execute_scenario", forbidden)
    result = CliRunner().invoke(
        app,
        [
            "load-test",
            "--scenario",
            str(Path(__file__).parents[1] / "scenarios" / "complex-v2-smoke.json"),
        ],
    )
    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "planned_not_executed"


def test_load_quality_gate_is_not_just_http_success():
    scenario = Scenario(name="test", stages=[{"name": "one", "mode": "parallel"}])
    summary = {
        "status": "succeeded",
        "quality": {
            "evaluated_records": 13,
            "field_accuracy": {"primary_route": 1, "confidence": 0.9},
            "constraint_compliance": {
                "immutable_archived_confidence": 1,
                "identifier_suppression": 1,
            },
        },
    }
    assert quality_gate(summary, scenario)["status"] == "needs_review"


def test_refresh_does_not_extend_completed_matrix_timing():
    scenario = Scenario(name="test", stages=[{"name": "one", "mode": "parallel"}])
    value = {
        "started_at": "2026-01-01T10:00:00+00:00",
        "ended_at": "2026-01-01T10:01:00+00:00",
        "runs": [{"gate": {"status": "passed"}}],
    }
    _finish_matrix(value, scenario)
    assert value["elapsed_seconds_including_cooldowns"] == 60


def test_flat_analysis_csv_does_not_execute_model_text_as_a_formula():
    rows = [{"id": "test", "status": "succeeded", "result": {"confidence_basis": "=1+1"}}]
    content = list(csv.DictReader(analysis_csv(rows).splitlines()))
    assert content[0]["confidence_basis"] == "'=1+1"
