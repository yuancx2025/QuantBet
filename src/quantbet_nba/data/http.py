"""Polite HTTP fetching and an atomic raw store that records when each response was fetched.

Raw responses are kept unmodified, gzipped, with their request. A file exists only if its fetch
finished, so an interrupted pull resumes safely and partial pagination never looks complete.
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)


def _retryable(error: BaseException) -> bool:
    if isinstance(error, httpx.HTTPStatusError):
        return error.response.status_code == 429 or error.response.status_code >= 500
    return isinstance(error, httpx.TransportError)


class RateLimiter:
    """Spaces requests evenly across threads."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


class Fetcher:
    def __init__(self, per_second: float) -> None:
        self._limiter = RateLimiter(per_second)
        self._client = httpx.Client(headers={"User-Agent": "quantbet-research"}, timeout=60)
        self.calls = 0

    @retry(
        retry=retry_if_exception(_retryable),
        stop=stop_after_attempt(6),
        wait=wait_exponential_jitter(initial=1, max=60),
        reraise=True,
    )
    def get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._limiter.wait()
        response = self._client.get(url, params=params)
        self.calls += 1
        response.raise_for_status()
        return response.json()


def write_raw(path: Path, request: dict[str, Any], data: Any) -> None:
    """Store a complete response atomically, along with its request and fetch time."""
    payload = {
        "fetched_at": dt.datetime.now(dt.UTC).isoformat(),
        "request": request,
        "data": data,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as handle:
        json.dump(payload, handle)
    os.replace(temporary, path)


def read_raw(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        return json.load(handle)
