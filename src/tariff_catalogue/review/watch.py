"""Watch network tariff documents and report changes for human review."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import os
import random
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from tariff_catalogue.harvest.common.archive import ArchiveStore, LocalArchiveStore, S3ArchiveStore

STATE_PATH = "state/network_watch.json"
USER_AGENT = (
    "tariff-catalogue network document watcher (https://github.com/cabberley/tariff-catalogue)"
)
RETRY_STATUSES = {429, 500, 502, 503, 504}


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    delay = random.uniform(0.0, min(60.0, 2**attempt))
    if retry_after:
        try:
            return max(delay, float(retry_after))
        except ValueError:
            try:
                retry_time = parsedate_to_datetime(retry_after)
                if retry_time.tzinfo is None:
                    retry_time = retry_time.replace(tzinfo=UTC)
                delay = max(delay, (retry_time - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, delay)


def load_sources(path: Path) -> list[dict[str, Any]]:
    """Load document URLs grouped by distributor."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Network sources must contain a mapping of distributors")

    documents: list[dict[str, Any]] = []
    for dnsp, config in sorted(data.items()):
        if not isinstance(config, dict) or not isinstance(config.get("documents"), list):
            raise ValueError(f"Network sources for {dnsp} must define a documents list")
        for document in config["documents"]:
            if not isinstance(document, dict) or not isinstance(document.get("url"), str):
                raise ValueError(f"Network source document for {dnsp} must define a URL")
            documents.append(
                {
                    "dnsp": dnsp,
                    "name": str(document.get("name", document["url"])),
                    "url": document["url"],
                    "needs_browser": bool(config.get("needs_browser", False)),
                }
            )
    return documents


def _fetch_http(client: httpx.Client, url: str) -> tuple[bytes, str]:
    for attempt in range(6):
        try:
            response = client.get(url)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            if attempt == 5:
                raise
            time.sleep(_retry_delay(attempt))
            continue
        if response.status_code in RETRY_STATUSES and attempt < 5:
            time.sleep(_retry_delay(attempt, response.headers.get("Retry-After")))
            continue
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "application/octet-stream")
    raise RuntimeError(f"HTTP retries exhausted for {url}")


def _fetch_browser(url: str) -> tuple[bytes, str]:
    try:
        playwright_api = importlib.import_module("playwright.sync_api")
    except ImportError as error:
        raise RuntimeError(
            "Install the network-watch extra to fetch browser-rendered sources"
        ) from error

    with playwright_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            for attempt in range(6):
                try:
                    response = page.goto(url, wait_until="networkidle", timeout=60_000)
                except playwright_api.Error:
                    if attempt == 5:
                        raise
                    time.sleep(_retry_delay(attempt))
                    continue
                if response is None:
                    if attempt == 5:
                        raise RuntimeError(f"Browser request returned no response for {url}")
                    time.sleep(_retry_delay(attempt))
                    continue
                if response.status in RETRY_STATUSES and attempt < 5:
                    time.sleep(_retry_delay(attempt, response.headers.get("retry-after")))
                    continue
                if not response.ok:
                    raise httpx.HTTPStatusError(
                        f"Browser request failed: {response.status}",
                        request=httpx.Request("GET", url),
                        response=httpx.Response(response.status),
                    )
                content_type = response.headers.get("content-type", "application/octet-stream")
                content = (
                    page.content().encode("utf-8")
                    if "text/html" in content_type
                    else response.body()
                )
                return (
                    content,
                    content_type,
                )
            raise RuntimeError(f"Browser retries exhausted for {url}")
        finally:
            browser.close()


def _issue_body(
    document: Mapping[str, Any],
    old_hash: str,
    new_hash: str,
    old_archive_path: str | None,
    new_archive_path: str,
) -> str:
    run_url = ""
    if (
        os.getenv("GITHUB_SERVER_URL")
        and os.getenv("GITHUB_REPOSITORY")
        and os.getenv("GITHUB_RUN_ID")
    ):
        run_url = (
            f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        )
    return "\n".join(
        [
            f"## Network tariff document changed: {document['dnsp']}",
            "",
            f"- Document: [{document['name']}]({document['url']})",
            f"- Old SHA-256: `{old_hash}`",
            f"- New SHA-256: `{new_hash}`",
            f"- Previous archive object: `{old_archive_path or 'not available'}`",
            f"- New archive object: `{new_archive_path}`",
            *([f"- [View workflow run]({run_url})"] if run_url else []),
            "",
            "Please review the source document and update curated network tariff data if needed. "
            "No tariff extraction or change has been made automatically.",
        ]
    )


def run_watch(
    documents: list[dict[str, Any]],
    archive: ArchiveStore,
    create_issue: Callable[[str, str], None],
    *,
    client: httpx.Client | None = None,
    browser_fetch: Callable[[str], tuple[bytes, str]] = _fetch_browser,
    retrieved_at: datetime | None = None,
) -> int:
    """Archive changed documents and open one review issue for each change."""
    state = archive.get_json(STATE_PATH) if archive.exists(STATE_PATH) else {}
    if not isinstance(state, dict):
        raise ValueError("Network watcher state must contain a JSON object")
    tracked = state.setdefault("documents", {})
    if not isinstance(tracked, dict):
        raise ValueError("Network watcher state documents must be a JSON object")

    now = retrieved_at or datetime.now(UTC)
    own_client = client is None
    http = client or httpx.Client(
        follow_redirects=True,
        timeout=30.0,
        headers={"User-Agent": USER_AGENT},
    )
    changes = 0
    try:
        for document in documents:
            content, content_type = (
                browser_fetch(document["url"])
                if document["needs_browser"]
                else _fetch_http(http, document["url"])
            )
            digest = hashlib.sha256(content).hexdigest()
            key = hashlib.sha256(document["url"].encode("utf-8")).hexdigest()
            previous = tracked.get(key, {})
            old_hash = previous.get("sha256") if isinstance(previous, dict) else None
            old_archive_path = previous.get("archive_path") if isinstance(previous, dict) else None
            archive_path = old_archive_path
            if old_hash != digest:
                archive_path = archive.put_raw(
                    "network_watch",
                    f"{document['dnsp']}/{key}",
                    content,
                    {
                        "url": document["url"],
                        "content_type": content_type,
                        "retrieved_at": now,
                    },
                )
                if old_hash is not None:
                    title = f"Network tariff document changed: {document['dnsp']}"
                    create_issue(
                        title,
                        _issue_body(
                            document,
                            old_hash,
                            digest,
                            old_archive_path,
                            archive_path,
                        ),
                    )
                    changes += 1

            tracked[key] = {
                "dnsp": document["dnsp"],
                "url": document["url"],
                "sha256": digest,
                "archive_path": archive_path,
                "checked_at": now.isoformat(),
            }
            archive.put_json(STATE_PATH, state)
    finally:
        if own_client:
            http.close()
    return changes


def _create_github_issue(title: str, body: str, client: httpx.Client) -> None:
    repository = os.getenv("GITHUB_REPOSITORY")
    if not repository or "/" not in repository:
        raise ValueError("GITHUB_REPOSITORY must be set as owner/repository")
    response = client.post(
        f"https://api.github.com/repos/{repository}/issues",
        json={"title": title, "body": body},
    )
    response.raise_for_status()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sources", type=Path, default=Path("network/sources.yaml"))
    parser.add_argument(
        "--archive-root", type=Path, default=Path(os.getenv("ARCHIVE_ROOT", "archive"))
    )
    args = parser.parse_args(argv)
    archive: ArchiveStore
    if os.getenv("R2_BUCKET") or os.getenv("R2_BUCKET_NAME"):
        archive = S3ArchiveStore()
    elif os.getenv("GITHUB_ACTIONS") == "true":
        parser.error("R2_BUCKET is required in GitHub Actions for durable watcher state")
    else:
        archive = LocalArchiveStore(args.archive_root)
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        parser.error("GITHUB_TOKEN is required to open change issues")
    with (
        httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT, "Authorization": "Bearer " + token},
        ) as github_client,
        httpx.Client(
            follow_redirects=True,
            timeout=30.0,
            headers={"User-Agent": USER_AGENT},
        ) as fetch_client,
    ):
        changes = run_watch(
            load_sources(args.sources),
            archive,
            lambda title, body: _create_github_issue(title, body, github_client),
            client=fetch_client,
        )
    print(f"Detected {changes} changed network tariff document(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
