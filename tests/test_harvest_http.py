import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from tariff_catalogue.harvest.common.http import HarvestHTTPError, PoliteClient
from tariff_catalogue.harvest.common.report import RunReport


def test_retries_then_returns_response_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("tariff_catalogue.harvest.common.http.random.uniform", lambda a, b: b)
    url = "https://example.com/plans"
    raw_body = b'{"plans": []}'

    with respx.mock:
        route = respx.get(url).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(200, content=raw_body),
            ]
        )
        report = RunReport()
        with PoliteClient(min_interval=0, sleep=delays.append, report=report) as client:
            data, meta = client.get_json(url)

    assert route.call_count == 2
    assert data == {"plans": []}
    assert meta["status"] == 200
    assert meta["final_url"] == url
    assert meta["sha256"] == hashlib.sha256(raw_body).hexdigest()
    assert delays == [1.0]
    assert report.requests == 2
    assert report.retries == 1
    assert report.failures == 0


def test_retry_after_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("tariff_catalogue.harvest.common.http.random.uniform", lambda a, b: 0)
    url = "https://example.com/plans"

    with respx.mock:
        respx.get(url).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "3"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        with PoliteClient(min_interval=0, sleep=delays.append) as client:
            assert client.get_json(url)[0] == {"ok": True}

    assert delays == [3.0]


def test_retry_after_http_date_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("tariff_catalogue.harvest.common.http.random.uniform", lambda a, b: 0)
    retry_at = datetime.now(UTC) + timedelta(seconds=5)
    url = "https://example.com/plans"

    with respx.mock:
        respx.get(url).mock(
            side_effect=[
                httpx.Response(
                    503, headers={"Retry-After": format_datetime(retry_at, usegmt=True)}
                ),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        with PoliteClient(min_interval=0, sleep=delays.append) as client:
            assert client.get_json(url)[0] == {"ok": True}

    assert len(delays) == 1
    assert 4.0 <= delays[0] <= 5.0


def test_connection_error_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []
    monkeypatch.setattr("tariff_catalogue.harvest.common.http.random.uniform", lambda a, b: 0)
    url = "https://example.com/plans"

    with respx.mock:
        route = respx.get(url).mock(
            side_effect=[
                httpx.ConnectError("connection unavailable"),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        with PoliteClient(min_interval=0, sleep=delays.append) as client:
            assert client.get_json(url)[0] == {"ok": True}

    assert route.call_count == 2
    assert delays == [0]


def test_404_is_not_retried_and_error_body_is_truncated() -> None:
    url = "https://example.com/missing"
    body = b"x" * 4096

    with respx.mock:
        route = respx.get(url).mock(return_value=httpx.Response(404, content=body))
        with PoliteClient(min_interval=0, sleep=lambda _: None) as client:
            with pytest.raises(HarvestHTTPError) as raised:
                client.get_json(url)

    assert route.call_count == 1
    assert raised.value.url == url
    assert raised.value.status == 404
    assert len(raised.value.body.encode()) == 2048


def test_per_host_concurrency_limit() -> None:
    active = 0
    max_active = 0
    lock = threading.Lock()
    both_started = threading.Event()
    release = threading.Event()

    def respond(_: httpx.Request) -> httpx.Response:
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
            if active == 2:
                both_started.set()
        release.wait(timeout=2)
        with lock:
            active -= 1
        return httpx.Response(200, json={"ok": True})

    with respx.mock:
        respx.get(url__regex=r"https://example\.com/.*").mock(side_effect=respond)
        with PoliteClient(
            max_concurrency_per_host=2,
            min_interval=0,
            sleep=lambda _: None,
        ) as client, ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(client.get_json, f"https://example.com/{index}")
                for index in range(5)
            ]
            assert both_started.wait(timeout=2)
            release.set()
            assert all(future.result(timeout=2)[0] == {"ok": True} for future in futures)

    assert max_active == 2


def test_user_agent_and_custom_headers_are_sent() -> None:
    url = "https://example.com/plans"
    requests: list[httpx.Request] = []

    with respx.mock:
        respx.get(url).mock(
            side_effect=lambda request: (
                requests.append(request) or httpx.Response(200, json={"ok": True})
            )
        )
        with PoliteClient(min_interval=0, sleep=lambda _: None) as client:
            client.get_json(
                url,
                headers={"User-Agent": "custom-agent", "X-Test": "value"},
            )
            client.get_json(url)

    assert len(requests) == 2
    assert all(
        request.headers["User-Agent"].startswith("tariff-catalogue/")
        for request in requests
    )
    assert requests[0].headers["X-Test"] == "value"


def test_run_report_serialization_and_github_summary(tmp_path) -> None:
    report = RunReport()
    report.record_request()
    report.record_retry()
    report.record_failure("source unavailable")
    report.record_version("new")
    report.record_version("unchanged")
    report.record_version("partial")
    report.record_duration("http", 0.25)

    parsed = json.loads(report.to_json())
    assert parsed["requests"] == 1
    assert parsed["failures"] == 1
    assert parsed["durations"] == {"http": 0.25}
    assert "| New versions | 1 |" in report.to_markdown()

    summary = tmp_path / "summary.md"
    assert report.write_summary(summary)
    assert summary.read_text().startswith("## Harvest run summary")


def test_default_user_agent_uses_repository_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY_OWNER", "example-owner")

    with PoliteClient() as client:
        assert client.user_agent.endswith(
            "(+https://github.com/example-owner/tariff-catalogue)"
        )
