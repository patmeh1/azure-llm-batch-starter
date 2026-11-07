from __future__ import annotations

import random
import threading
import time
from collections import deque
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime


def retry_delay(headers: Mapping[str, str], attempt: int, *, now: datetime | None = None) -> float:
    normalized = {key.lower(): value for key, value in headers.items()}
    if "retry-after-ms" in normalized:
        try:
            return max(0, float(normalized["retry-after-ms"]) / 1000)
        except ValueError:
            pass
    value = normalized.get("retry-after")
    if value:
        try:
            return max(0, float(value))
        except ValueError:
            try:
                date = parsedate_to_datetime(value)
                return max(0, (date - (now or datetime.now(UTC))).total_seconds())
            except (ValueError, TypeError, OverflowError):
                pass
    return random.uniform(0.5, min(30, 2**attempt))


class AdmissionLimiter:
    """Conservative rolling admission estimates; not a replica of Azure enforcement."""

    def __init__(self, rpm: int, tpm: int, *, window_seconds: float = 60):
        self.rpm, self.tpm, self.window = rpm, tpm, window_seconds
        self.history: deque[tuple[float, int]] = deque()
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.attempts = 0

    def acquire(
        self, tokens: int, deadline: float, max_attempts: int, *, observe: bool = False
    ) -> float:
        if tokens > self.tpm and not observe:
            raise ValueError("One request exceeds the configured TPM; increase it or use Batch")
        started = time.monotonic()
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TimeoutError("Admission deadline reached")
            with self.lock:
                while self.history and self.history[0][0] <= now - self.window:
                    self.history.popleft()
                if self.attempts >= max_attempts:
                    raise TimeoutError("Run HTTP attempt limit reached")
                delay = 0.0
                if not observe:
                    delay = max(0, self.next_request - now)
                    if (
                        len(self.history) >= self.rpm
                        or sum(t for _, t in self.history) + tokens > self.tpm
                    ):
                        delay = max(delay, self.history[0][0] + self.window - now)
                if delay <= 0:
                    self.history.append((now, tokens))
                    self.attempts += 1
                    self.next_request = now + self.window / self.rpm
                    return time.monotonic() - started
            if now + delay >= deadline:
                raise TimeoutError("Insufficient time remains for rate-limit admission")
            time.sleep(min(delay, 1.0))
