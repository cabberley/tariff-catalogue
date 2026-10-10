from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx
import yaml

from tariff_catalogue.harvest.common.archive import LocalArchiveStore
from tariff_catalogue.publish.build import build
from tariff_catalogue.review.diff_report import build_diff_report
from tariff_catalogue.review.network_validation import validate_network_file
from tariff_catalogue.review.watch import load_sources, run_watch

ROOT = Path(__file__).parent.parent
NETWORK_PLAN = ROOT / "network/au/ergon/2026-27/east/RTOUE.yaml"
NETWORK_DOCS = [
    {
        "dnsp": "ergon",
        "name": "Tariff guide",
        "url": "https://example.test/guide.pdf",
        "needs_browser": True,
    },
    {
        "dnsp": "ergon",
        "name": "Price list",
        "url": "https://example.test/prices.xlsx",
        "needs_browser": False,
    },
]


def test_network_tariff_requires_source_url(tmp_path: Path) -> None:
    path = tmp_path / "tariff.yaml"
    path.write_text(
        yaml.safe_dump({"kind": "network", "plan_id": "au:network:ergon:east:RTOUE"}),
        encoding="utf-8",
    )

    assert validate_network_file(path) == [f"{path}: source.url is required"]


def test_network_tariff_example_is_valid() -> None:
    assert validate_network_file(NETWORK_PLAN) == []


def test_ergon_sources_mark_browser_rendered_documents() -> None:
    documents = load_sources(ROOT / "network/sources.yaml")

    assert len(documents) == 3
    assert all(document["needs_browser"] for document in documents)


def test_watcher_opens_one_issue_per_changed_document(
    tmp_path: Path,
) -> None:
    price_list_url = NETWORK_DOCS[1]["url"]
    with respx.mock:
        prices = respx.get(price_list_url).mock(
            side_effect=[
                httpx.Response(200, content=b"prices v1"),
                httpx.Response(200, content=b"prices v1"),
            ]
        )
        with httpx.Client() as client:
            archive = LocalArchiveStore(tmp_path / "archive")
            browser_calls: list[str] = []

            def fetch_guide(_url: str) -> tuple[bytes, str]:
                browser_calls.append(_url)
                return (b"guide v1" if len(browser_calls) == 1 else b"guide v2", "application/pdf")

            run_watch(
                NETWORK_DOCS,
                archive,
                lambda *_args: None,
                client=client,
                browser_fetch=fetch_guide,
                retrieved_at=datetime(2026, 10, 10, tzinfo=UTC),
            )
            issues: list[tuple[str, str]] = []
            changes = run_watch(
                NETWORK_DOCS,
                archive,
                lambda title, body: issues.append((title, body)),
                client=client,
                browser_fetch=fetch_guide,
                retrieved_at=datetime(2026, 10, 11, tzinfo=UTC),
            )

    assert changes == 1
    assert len(issues) == 1
    title, body = issues[0]
    assert title == "Network tariff document changed: ergon"
    assert "Old SHA-256:" in body
    assert "New SHA-256:" in body
    assert "guide.pdf" in body
    archived_documents = [
        path for path in archive.list("raw/network_watch/") if path.endswith(".json.gz")
    ]
    assert len(archived_documents) == 3
    assert browser_calls == [NETWORK_DOCS[0]["url"], NETWORK_DOCS[0]["url"]]
    assert prices.call_count == 2


def test_watcher_retries_transient_http_errors_and_honors_retry_after(
    tmp_path: Path, monkeypatch
) -> None:
    delays: list[float] = []
    monkeypatch.setattr("tariff_catalogue.review.watch.time.sleep", delays.append)
    url = "https://example.test/document.pdf"
    document = [{"dnsp": "example", "name": "Document", "url": url, "needs_browser": False}]
    with respx.mock:
        route = respx.get(url).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, content=b"document"),
            ]
        )
        with httpx.Client() as client:
            changes = run_watch(
                document,
                LocalArchiveStore(tmp_path / "archive"),
                lambda *_args: None,
                client=client,
            )

    assert route.call_count == 2
    assert changes == 0
    assert delays[0] >= 2


def test_network_diff_report_shows_rate_and_percent_changes() -> None:
    fixture_dir = ROOT / "tests/fixtures/network"
    old = yaml.safe_load((fixture_dir / "old.yaml").read_text(encoding="utf-8"))
    new = yaml.safe_load((fixture_dir / "new.yaml").read_text(encoding="utf-8"))

    report = build_diff_report(old, new)

    assert "| supply | 1.00 | 1.10 | 10.00% |" in report
    assert "| peak (peak) | 0.20 | 0.25 | 25.00% |" in report


def test_build_publishes_curated_network_tariff(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "network/au/ergon/2026-27/east/RTOUE.yaml"
    target.parent.mkdir(parents=True)
    shutil.copyfile(NETWORK_PLAN, target)

    report = build(LocalArchiveStore(tmp_path / "archive"), tmp_path / "dist")

    published = tmp_path / "dist/v1/au/network/ergon/2026-27/east/RTOUE.json"
    assert report.plans == 1
    assert published.is_file()
    assert yaml.safe_load(published.read_text(encoding="utf-8"))["plan_id"] == (
        "au:network:ergon:east:RTOUE"
    )
