from __future__ import annotations

import json
from collections import Counter
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .quality import annotate_result
from .synthetic import Record


def is_retryable(record: dict[str, Any]) -> bool:
    if "retryable" in record:
        return record["retryable"] is True
    status = record.get("status_code")
    return (
        isinstance(status, int) and (status in {408, 409, 429} or 500 <= status < 600)
    ) or record.get("error") in {"APIConnectionError", "APITimeoutError"}


def validate_completion(body: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        return {"status": "failed", "error": "invalid_response_shape", "retryable": False}
    usage = body.get("usage") or {}
    choices = body.get("choices") or []
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {"status": "failed", "error": "missing_choices", "usage": usage, "retryable": False}
    choice = choices[0]
    message = choice.get("message") or {}
    if not isinstance(message, dict):
        return {
            "status": "failed",
            "error": "invalid_response_shape",
            "usage": usage,
            "retryable": False,
        }
    if message.get("refusal"):
        return {"status": "failed", "error": "refusal", "usage": usage, "retryable": False}
    if choice.get("finish_reason") != "stop":
        return {
            "status": "failed",
            "error": "incomplete_completion",
            "usage": usage,
            "retryable": False,
        }
    try:
        content = json.loads(message.get("content") or "")
        Draft202012Validator(schema).validate(content)
    except (ValueError, TypeError, ValidationError):
        return {
            "status": "failed",
            "error": "invalid_output_schema",
            "usage": usage,
            "retryable": False,
        }
    return {"status": "succeeded", "result": content, "usage": usage}


def reconcile_results(
    records: list[Record], output: str, errors: str, schema: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    expected = {record.id: record for record in records}
    entries: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    issues: list[str] = []
    for source in (output, errors):
        for line in source.splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                item_id = item["custom_id"]
                if not isinstance(item_id, str):
                    raise ValueError("Invalid ID")
            except (ValueError, KeyError, TypeError):
                issues.append("invalid_result_line")
                continue
            if item_id not in expected:
                issues.append("unexpected_result_id")
                continue
            counts[item_id] += 1
            entries[item_id] = item
    results = []
    for record_id, record in expected.items():
        if counts[record_id] == 0:
            outcome: dict[str, Any] = {"status": "failed", "error": "missing_result"}
        elif counts[record_id] > 1:
            outcome = {"status": "failed", "error": "duplicate_result"}
        else:
            item = entries[record_id]
            response = item.get("response") or {}
            if not isinstance(response, dict):
                response = {"status_code": None, "body": None}
            if item.get("error") or response.get("status_code") != 200:
                outcome = {
                    "status": "failed",
                    "error": "remote_record_error",
                    "status_code": response.get("status_code"),
                }
            else:
                outcome = validate_completion(response.get("body") or {}, schema)
        annotate_result(record, outcome)
        outcome.setdefault("retryable", is_retryable(outcome))
        results.append(outcome)
    return results, issues
