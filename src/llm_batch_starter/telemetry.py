from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def elapsed_between(start: str, end: str | None = None) -> float:
    first = datetime.fromisoformat(start)
    last = datetime.fromisoformat(end) if end else datetime.now(UTC)
    return max(0, (last - first).total_seconds())


def event(
    stage: str,
    *,
    duration_seconds: float | None = None,
    record_id: str | None = None,
    status_code: int | None = None,
    **metadata: Any,
) -> dict[str, Any]:
    return {
        "event_id": uuid.uuid4().hex,
        "at": utc_now(),
        "stage": stage,
        "duration_seconds": duration_seconds,
        "record_id": record_id,
        "status_code": status_code,
        **metadata,
    }


@contextmanager
def timed_stage(
    sink: Callable[[dict[str, Any]], None], stage: str, **metadata: Any
) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        sink(event(stage, duration_seconds=time.perf_counter() - start, **metadata))


def safe_error(exc: BaseException) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    headers = getattr(getattr(exc, "response", None), "headers", {})
    permitted = {
        key: str(value)
        for key, value in headers.items()
        if key.lower().startswith("x-ratelimit-")
        or key.lower() in {"retry-after", "retry-after-ms", "apim-request-id", "x-request-id"}
    }
    return {"type": type(exc).__name__, "status_code": status, "headers": permitted}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
