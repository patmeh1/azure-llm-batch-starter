from __future__ import annotations

import json
from collections import Counter
from typing import Any

from jsonschema import Draft202012Validator

from .complex_workload import FIELDS, PROFILE, Expected, catalog
from .synthetic import Record


def validate_expectations(records: list[Record], output_schema: dict[str, Any]) -> None:
    if not any(record.expected_result is not None for record in records):
        return
    if set(output_schema.get("properties", {})) != set(FIELDS):
        raise ValueError("Complex gold-labeled records require the complex-v2 output schema")
    validator = Draft202012Validator(output_schema)
    for record in records:
        if record.expected_result is None:
            continue
        expected = Expected.model_validate(record.expected_result)
        if set(expected.fields) != set(FIELDS) - {"confidence", "confidence_basis"}:
            raise ValueError(f"Invalid synthetic gold fields for {record.id}")
        if expected.minimum_confidence > expected.maximum_confidence:
            raise ValueError(f"Invalid synthetic confidence band for {record.id}")
        validator.validate(
            {
                **expected.fields,
                "confidence": expected.minimum_confidence,
                "confidence_basis": expected.archived_basis,
            }
        )


def annotate_result(record: Record, outcome: dict[str, Any]) -> None:
    outcome.update(id=record.id, expected_label=record.expected_label)
    if (
        outcome["status"] == "succeeded"
        and record.expected_label
        and isinstance(outcome["result"], dict)
    ):
        outcome["label_correct"] = outcome["result"].get("category") == record.expected_label
    if record.expected_result is None:
        return
    expected = Expected.model_validate(record.expected_result)
    quality: dict[str, Any] = {
        "profile": PROFILE,
        "scenario": expected.scenario,
        "evaluated": outcome["status"] == "succeeded",
        "field_checks": {},
        "constraint_checks": {},
        "passed": False,
    }
    if outcome["status"] == "succeeded":
        result = outcome["result"]
        checks = {}
        for field, value in expected.fields.items():
            observed = result.get(field)
            checks[field] = (
                sorted(observed) == sorted(value)
                if field in {"secondary_routes", "improvements"} and isinstance(observed, list)
                else observed == value
            )
        confidence = result["confidence"]
        checks["confidence"] = (
            expected.minimum_confidence <= confidence <= expected.maximum_confidence
        )
        checks["confidence_basis"] = bool(result["confidence_basis"].strip())
        if confidence == expected.archived_score:
            checks["confidence_basis"] = result["confidence_basis"] == expected.archived_basis
        elif expected.archived_score is not None:
            checks["confidence_basis"] &= result["confidence_basis"] != expected.archived_basis
        routes = {route.code for route in catalog()}
        secondaries, improvements = result["secondary_routes"], result["improvements"]
        rendered = json.dumps(result, ensure_ascii=False)
        constraints = {
            "taxonomy_membership": result["primary_route"] in routes | {"UNCLASSIFIED"}
            and all(route in routes for route in secondaries),
            "secondary_limit_and_distinctness": len(secondaries) <= 2
            and len(secondaries) == len(set(secondaries))
            and result["primary_route"] not in secondaries,
            "improvement_limit_and_distinctness": len(improvements) <= 2
            and len(improvements) == len(set(improvements)),
            "confidence_bounds": 0 <= confidence <= 100,
            "confidence_never_lowered": expected.archived_score is None
            or confidence >= expected.archived_score,
            "immutable_archived_confidence": expected.archived_score is None
            or expected.archived_score < 75
            or (
                confidence == expected.archived_score
                and result["confidence_basis"] == expected.archived_basis
            ),
            "identifier_suppression": all(
                value not in rendered for value in expected.forbidden_values
            ),
        }
        quality.update(
            field_checks=checks,
            constraint_checks=constraints,
            passed=all(checks.values()) and all(constraints.values()),
        )
    outcome["quality"] = quality


def summarize_quality(records: list[dict[str, Any]], expected_count: int) -> dict[str, Any] | None:
    values = [record["quality"] for record in records if record.get("quality")]
    if not values:
        return None
    evaluated = [value for value in values if value["evaluated"]]
    fields: Counter[str] = Counter()
    constraints: Counter[str] = Counter()
    cases: dict[str, dict[str, int]] = {}
    for value in values:
        case = cases.setdefault(value["scenario"], {"records": 0, "evaluated": 0, "passed": 0})
        case["records"] += 1
        case["evaluated"] += int(value["evaluated"])
        case["passed"] += int(value["passed"])
        fields.update({key: int(passed) for key, passed in value["field_checks"].items()})
        constraints.update({key: int(passed) for key, passed in value["constraint_checks"].items()})
    passed = sum(value["passed"] for value in values)
    return {
        "profile": PROFILE,
        "expected_records": expected_count,
        "evaluated_records": len(evaluated),
        "not_evaluated_records": max(0, expected_count - len(evaluated)),
        "passed_records": passed,
        "fully_correct_fraction_of_all_records": passed / expected_count
        if expected_count
        else None,
        "exact_match_fraction_of_evaluated": passed / len(evaluated) if evaluated else None,
        "field_accuracy": {key: count / len(evaluated) for key, count in fields.items()}
        if evaluated
        else {},
        "constraint_compliance": {key: count / len(evaluated) for key, count in constraints.items()}
        if evaluated
        else {},
        "scenario_breakdown": cases,
        "grading_note": (
            "Deterministic synthetic gold checks. Free-form newly generated confidence explanations "
            "are checked for presence and canary suppression, not independently judged for semantic truth."
        ),
    }
