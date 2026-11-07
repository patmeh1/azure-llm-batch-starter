from __future__ import annotations

import json
import random
from pathlib import Path

import tiktoken
from pydantic import BaseModel, ConfigDict, Field

TICKETS = {
    "shipping": "My parcel has not arrived. Please check the delivery tracking and shipping status.",
    "billing": "The invoice includes a duplicate charge. Please correct the payment and billing total.",
    "access": "I cannot sign in to my account. Please help reset access to the login portal.",
    "product": "The item stopped working after installation. I need product troubleshooting.",
    "other": "I would like to share general feedback and a suggestion for your team.",
}
FILLER = (
    "Synthetic reference note: a fictional catalog contains neutral descriptions of shapes, "
    "colors, materials, and seasons. This background is not a request for assistance. "
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,80}$")
    text: str = Field(min_length=1)
    expected_label: str | None = None


def generate_dataset(count: int, seed: int = 42, reference_tokens: int = 0) -> list[Record]:
    if count < 1 or count > 100_000 or reference_tokens < 0:
        raise ValueError("count must be 1..100000 and reference_tokens nonnegative")
    if count * reference_tokens > 10_000_000:
        raise ValueError(
            "Synthetic reference text exceeds the bounded generator limit; split the dataset"
        )
    rng = random.Random(seed)
    enc = tiktoken.get_encoding("o200k_base")
    unit = enc.encode(FILLER)
    # Token boundaries change when text repeats; repeating encoded units undercounts after re-encoding.
    increment = len(enc.encode(FILLER * 2)) - len(unit)
    encoded = enc.encode(FILLER * (reference_tokens // increment + 2)) if reference_tokens else []
    reference = enc.decode(encoded[:reference_tokens])
    records = []
    labels = list(TICKETS)
    for index in range(count):
        label = labels[index % len(labels)]
        suffix = rng.choice(["Please advise.", "Thank you.", "A response would be appreciated."])
        text = f"REQUEST\n{TICKETS[label]} {suffix}"
        if reference:
            text = f"REFERENCE\n{reference}\n{text}"
        records.append(Record(id=f"record-{index:06d}", text=text, expected_label=label))
    return records


def write_records(path: Path, records: list[Record]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(record.model_dump_json() + "\n")


def read_records(path: Path) -> list[Record]:
    records: list[Record] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8-sig") as stream:
        for index, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"Blank JSONL record at line {index}")
            try:
                record = Record.model_validate(json.loads(line))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"Invalid record at line {index}: {type(exc).__name__}") from exc
            if record.id in seen:
                raise ValueError(f"Duplicate record ID: {record.id}")
            seen.add(record.id)
            records.append(record)
    if not records:
        raise ValueError("Input file has no records")
    return records
