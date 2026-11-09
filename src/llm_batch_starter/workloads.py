from __future__ import annotations

from typing import Any

from .complex_workload import PROFILE, configure, generate_complex
from .config import Settings
from .synthetic import Record


def configure_profile(settings: Settings, profile: str) -> tuple[Settings, dict[str, Any]]:
    if profile == "simple":
        return settings, {"name": "simple"}
    if profile == PROFILE:
        return configure(settings)
    raise ValueError("profile must be simple or complex-v2")


def validate_profile_records(records: list[Record], profile: str) -> None:
    if profile != PROFILE and any(record.expected_result is not None for record in records):
        raise ValueError("Complex gold-labeled records require --profile complex-v2")


def complex_records(count: int, seed: int, target_input_tokens: int = 0) -> list[Record]:
    if target_input_tokens:
        raise ValueError(
            "Complex prompts use the actual active catalog, not filler padding; omit token-target options"
        )
    return generate_complex(count, seed)
