"""Polite HTTP client shared by harvesters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from importlib.metadata import PackageNotFoundError, version
from threading import Lock, Semaphore
from typing import Any
from urllib.parse import urlsplit

import httpx

from tariff_catalogue.harvest.common.report import RunReport

_RETRY_STATUSES = {429, 500, 502, 503, 504}
_MAX_ERROR_BODY_BYTES = 2048


class HarvestHTTPError(Exception):
    """An unsuccessful HTTP response from a harvest source."""

    def __init__(self, url: str, status: int, body: str) -> None:
        self.url = url
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status} for {url}: {body}")


class _HostState:
    def __init__(self, max_concurrency: int) -> None:
        self.semaphore = Semaphore(max_concurrency)
        self.rate_lock = Lock()
        self.next_request_at = 0.0


class PoliteClient:
    """An HTTPX client with per-host concurrency and retry limits."""

    def __init__(
        self,
        *,
        max_concurrency_per_host: int = 4,
        min_interval: float = 0.25,
        max_attempts: int = 6,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        client: httpx.Client | None = None,
        report: RunReport | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if max_concurrency_per_host < 1:
            raise ValueError("max_concurrency_per_host must be at least 1")
        if min_interval < 0:
            raise ValueError("min_interval cannot be negative")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if base_delay < 0 or max_delay < 0:
            raise ValueError("retry delays cannot be negative")

        owner = os.getenv("GITHUB_REPOSITORY_OWNER", "owner").strip() or "owner"
        try:
            package_version = version("tariff_catalogue")
        except PackageNotFoundError:
            package_version = "0.1.0"
        self.user_agent = (
            f"tariff-catalogue/{package_version} (+https://github.com/{owner}/tariff-catalogue)"
        )
        self.max_concurrency_per_host = max_concurrency_per_host
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._client = client if client is not None else httpx.Client()
        self._report = report
        self._sleep = sleep or time.sleep
        self._hosts: dict[str, _HostState] = {}
        self._hosts_lock = Lock()

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_json(
        self, url: str, headers: Mapping[str, str] | None = None
    ) -> tuple[Any, dict[str, Any]]:
        """Fetch JSON and return it with response metadata."""
        started_at = time.monotonic()
        host = urlsplit(url).hostname
        if host is None:
            raise ValueError(f"URL has no host: {url}")
        state = self._host_state(host)

        request_headers = httpx.Headers(headers)
        request_headers["User-Agent"] = self.user_agent

        try:
            for attempt in range(self.max_attempts):
                response: httpx.Response | None = None
                try:
                    with self._request_slot(state):
                        if self._report is not None:
                            self._report.record_request()
                        response = self._client.get(url, headers=request_headers)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if attempt + 1 == self.max_attempts:
                        raise
                    self._record_retry()
                    self._sleep(self._backoff(attempt))
                    continue

                if response.status_code in _RETRY_STATUSES:
                    if attempt + 1 < self.max_attempts:
                        self._record_retry()
                        self._sleep(self._backoff(attempt, response.headers.get("Retry-After")))
                        continue

                if not 200 <= response.status_code < 300:
                    error = HarvestHTTPError(
                        str(response.url),
                        response.status_code,
                        response.content[:_MAX_ERROR_BODY_BYTES].decode("utf-8", errors="replace"),
                    )
                    raise error

                raw_body = response.content
                try:
                    data = json.loads(raw_body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    raise

                elapsed = time.monotonic() - started_at
                meta = {
                    "status": response.status_code,
                    "final_url": str(response.url),
                    "headers": dict(response.headers.items()),
                    "elapsed": elapsed,
                    "sha256": hashlib.sha256(raw_body).hexdigest(),
                    "raw_body": raw_body,
                }
                return data, meta
        except Exception as exc:
            self._record_failure(exc)
            raise
        finally:
            if self._report is not None:
                self._report.record_duration("http", time.monotonic() - started_at)

        raise RuntimeError("HTTP request exhausted without a response")

    def _host_state(self, host: str) -> _HostState:
        with self._hosts_lock:
            if host not in self._hosts:
                self._hosts[host] = _HostState(self.max_concurrency_per_host)
            return self._hosts[host]

    def _request_slot(self, state: _HostState) -> _RequestSlot:
        return _RequestSlot(state, self.min_interval, self._sleep)

    def _backoff(self, attempt: int, retry_after: str | None = None) -> float:
        ceiling = min(self.max_delay, self.base_delay * (2**attempt))
        delay = random.uniform(0.0, ceiling)
        if retry_after is not None:
            retry_delay = _retry_after_seconds(retry_after)
            if retry_delay is not None:
                delay = max(delay, retry_delay)
        return delay

    def _record_retry(self) -> None:
        if self._report is not None:
            self._report.record_retry()

    def _record_failure(self, error: Exception | str) -> None:
        if self._report is not None:
            self._report.record_failure(error)

    def clear_failure(self, error: Exception | str) -> None:
        if self._report is not None:
            self._report.clear_failure(error)


class _RequestSlot:
    def __init__(
        self, state: _HostState, min_interval: float, sleep: Callable[[float], None]
    ) -> None:
        self._state = state
        self._min_interval = min_interval
        self._sleep = sleep

    def __enter__(self) -> None:
        self._state.semaphore.acquire()
        try:
            with self._state.rate_lock:
                now = time.monotonic()
                if self._state.next_request_at > now:
                    self._sleep(self._state.next_request_at - now)
                    now = time.monotonic()
                self._state.next_request_at = now + self._min_interval
        except Exception:
            self._state.semaphore.release()
            raise

    def __exit__(self, *_: object) -> None:
        self._state.semaphore.release()


def _retry_after_seconds(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)
