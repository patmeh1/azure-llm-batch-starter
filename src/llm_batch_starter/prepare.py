from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import tiktoken
from jsonschema import Draft202012Validator

from .config import Settings
from .quality import validate_expectations
from .synthetic import Record, generate_dataset


@dataclass(frozen=True)
class Partition:
    id: str
    records: list[Record]
    payload: bytes
    estimated_tokens: int


def request_body(record: Record, settings: Settings, *, batch: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": settings.batch_deployment if batch else settings.online_deployment,
        "messages": [
            {"role": "system", "content": settings.prompt},
            {"role": "user", "content": record.text},
        ],
        "max_completion_tokens": settings.max_output_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "record_result",
                "strict": True,
                "schema": settings.output_schema,
            },
        },
    }
    if settings.reasoning_effort is not None:
        body["reasoning_effort"] = settings.reasoning_effort
    return body


def estimate_input_tokens(record: Record, settings: Settings) -> int:
    body = request_body(record, settings, batch=False)
    text = json.dumps(
        {"messages": body["messages"], "response_format": body["response_format"]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(tiktoken.get_encoding("o200k_base").encode(text))


def synthetic_target_padding(target: int, settings: Settings, seed: int = 42) -> int:
    if target <= 0:
        return 0
    if target + settings.max_output_tokens > settings.context_limit:
        raise ValueError("Synthetic target exceeds the configured context limit")
    padding = max(0, target - estimate_input_tokens(generate_dataset(1, seed)[0], settings))
    for _ in range(8):
        record = generate_dataset(1, seed, reference_tokens=padding)[0]
        difference = target - estimate_input_tokens(record, settings)
        if abs(difference) <= 2 or (padding == 0 and difference < 0):
            return padding
        padding = max(0, padding + difference)
    raise ValueError("Synthetic token target could not be calibrated")


def input_fingerprint(records: list[Record], settings: Settings) -> str:
    digest = hashlib.sha256(settings.fingerprint().encode())
    for record in records:
        digest.update(record.model_dump_json().encode())
        digest.update(b"\n")
    return digest.hexdigest()


def estimate_cost(records: list[Record], settings: Settings, *, batch: bool) -> dict[str, float]:
    tokens = sum(estimate_input_tokens(record, settings) for record in records)
    output_budget = len(records) * settings.max_output_tokens
    cost = (
        tokens * settings.input_price_per_million
        + output_budget * settings.output_price_per_million
    ) / 1_000_000
    if batch:
        cost *= settings.batch_discount
    return {
        "estimated_input_tokens": float(tokens),
        "maximum_generated_tokens": float(output_budget),
        "estimated_inference_usd": cost,
    }


def prepare_requests(records: list[Record], settings: Settings) -> list[Partition]:
    Draft202012Validator.check_schema(settings.output_schema)
    validate_expectations(records, settings.output_schema)
    ids = [r.id for r in records]
    if not records or len(ids) != len(set(ids)):
        raise ValueError("Records must be nonempty and have unique IDs")
    partitions: list[Partition] = []
    pending: list[Record] = []
    lines: list[bytes] = []
    size = 0
    tokens = 0
    for record in records:
        token_count = estimate_input_tokens(record, settings)
        if token_count + settings.max_output_tokens > settings.context_limit:
            raise ValueError(f"Record {record.id} exceeds the configured context limit")
        if token_count > settings.max_enqueued_tokens:
            raise ValueError(f"Record {record.id} exceeds the enqueued-token budget")
        line = (
            json.dumps(
                {
                    "custom_id": record.id,
                    "method": "POST",
                    "url": "/v1/chat/completions",
                    "body": request_body(record, settings, batch=True),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n"
        ).encode()
        if len(line) > settings.max_batch_bytes:
            raise ValueError(f"Record {record.id} exceeds the batch file size limit")
        if pending and (
            len(pending) >= settings.records_per_batch
            or size + len(line) > settings.max_batch_bytes
            or tokens + token_count > settings.max_enqueued_tokens
        ):
            partitions.append(
                Partition(f"part-{len(partitions):04d}", pending, b"".join(lines), tokens)
            )
            pending, lines, size, tokens = [], [], 0, 0
        pending.append(record)
        lines.append(line)
        size += len(line)
        tokens += token_count
    if pending:
        partitions.append(
            Partition(f"part-{len(partitions):04d}", pending, b"".join(lines), tokens)
        )
    return partitions
