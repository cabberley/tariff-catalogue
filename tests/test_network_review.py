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
from tariff_catalogue.review.watch import run_watch

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
        path
        for path in archive.list("raw/network_watch/")
        if path.endswith(".json.gz")
    ]
    assert len(archived_documents) == 3
    assert browser_calls == [NETWORK_DOCS[0]["url"], NETWORK_DOCS[0]["url"]]
    assert prices.call_count == 2


def test_network_diff_report_shows_rate_and_percent_changes() -> None:
    fixture_dir = ROOT / "tests/fixtures/network"
    old = yaml.safe_load((fixture_dir / "old.yaml").read_text(encoding="utf-8"))
    new = yaml.safe_load((fixture_dir / "new.yaml").read_text(encoding="utf-8"))

    report = build_diff_report(old, new)

    assert "| supply | 1.00 | 1.10 | 10.00% |" in report
    assert "| peak (peak) | 0.20 | 0.25 | 25.00% |" in report


def test_build_publishes_curated_network_tariff(
    tmp_path: Path, monkeypatch
) -> None:
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
