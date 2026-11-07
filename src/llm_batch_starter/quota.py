from __future__ import annotations

from typing import Any


def update_reservations(
    state: dict[str, int], operation: str, value: dict[str, Any]
) -> tuple[dict[str, int], bool]:
    updated = dict(state)
    key = str(value["key"])
    if operation == "release":
        updated.pop(key, None)
        return updated, True
    if operation != "acquire":
        raise ValueError("Unsupported quota coordinator operation")
    tokens, maximum, slots = (
        int(value["tokens"]),
        int(value["max_tokens"]),
        int(value["max_active"]),
    )
    if tokens <= 0 or maximum <= 0 or slots <= 0 or tokens > maximum:
        raise ValueError("Invalid token reservation")
    if key in updated:
        if updated[key] != tokens:
            raise ValueError("Existing reservation has a different token fingerprint")
        return updated, True
    if len(updated) >= slots or sum(updated.values()) + tokens > maximum:
        return updated, False
    updated[key] = tokens
    return updated, True
