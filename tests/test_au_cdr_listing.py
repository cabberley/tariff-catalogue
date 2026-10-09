import gzip
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import respx

from tariff_catalogue.cli import main
from tariff_catalogue.harvest.au_cdr.brands import BRANDS_URL, Brand, discover_brands
from tariff_catalogue.harvest.au_cdr.listing import (
    list_all_current_ids,
    list_changed_plans,
    load_last_success,
)
from tariff_catalogue.harvest.common.archive import LocalArchiveStore
from tariff_catalogue.harvest.common.http import PoliteClient
from tariff_catalogue.harvest.common.report import RunReport

FIXTURES = Path(__file__).parent / "fixtures" / "cdr"
ORIGIN_BASE = "https://cdr.energymadeeasy.gov.au/origin"
PLANS_URL = f"{ORIGIN_BASE}/cds-au/v1/energy/plans"


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_discover_brands_follows_pagination_and_skips_inactive() -> None:
    report = RunReport()
    with respx.mock:
        route = respx.get(url__regex=rf"{re.escape(BRANDS_URL)}(?:\?.*)?").mock(
            side_effect=lambda request: httpx.Response(
                200,
                content=_fixture(
                    "brands/summary-page-2.json"
                    if request.url.params.get("page") == "2"
                    else "brands/summary.json"
                ),
            )
        )
        with PoliteClient(min_interval=0) as client:
            brands = discover_brands(client, report)

    assert route.call_count == 2
    assert all(call.request.headers["x-v"] == "1" for call in route.calls)
    assert [brand.brand_id for brand in brands] == ["origin", "retailer-two", "another-brand"]
    assert brands[0].product_base_uri == ORIGIN_BASE
    assert brands[0].product_base_uri_field == "productBaseUri"
    assert brands[1].product_base_uri_field == "publicBaseUri"
    assert report.warnings == [
        "Skipping inactive CDR brand inactive (Inactive Retailer; status: REMOVED)"
    ]


def test_listing_archives_all_pages_and_reads_all_current_ids(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive")
    brand = Brand("origin", "Origin Energy", ORIGIN_BASE, "ACTIVE", "productBaseUri")
    with respx.mock:
        route = respx.get(url__regex=rf"{re.escape(PLANS_URL)}\?.*").mock(
            side_effect=[
                httpx.Response(200, content=_fixture("listing/origin-page-1.json")),
                httpx.Response(200, content=_fixture("listing/origin-page-2.json")),
            ]
        )
        with PoliteClient(min_interval=0) as client:
            plans = list_changed_plans(client, brand, None, store)

    assert route.call_count == 2
    assert [plan.plan_id for plan in plans] == ["ORIGIN-1", "ORIGIN-2"]
    assert plans[0].effective_from == datetime(2026, 10, 1, tzinfo=UTC)
    raw_paths = list(store.list("raw/au_cdr_list"))
    raw_pages = [path for path in raw_paths if path.endswith(".json.gz")]
    assert len(raw_pages) == 2
    assert sum("%2Fpage-1%2F" in path for path in raw_pages) == 1
    assert sum("%2Fpage-2%2F" in path for path in raw_pages) == 1
    assert {gzip.decompress(store.get(path)) for path in raw_pages} == {
        _fixture("listing/origin-page-1.json"),
        _fixture("listing/origin-page-2.json"),
    }


def test_updated_since_is_only_sent_when_brand_state_exists(tmp_path: Path) -> None:
    archive = LocalArchiveStore(tmp_path / "archive")
    state_path = "state/au_cdr.json"
    archive.put_json(
        state_path,
        {"brands": {"origin": {"last_success": "2026-10-01T12:00:00Z"}}},
    )
    since = load_last_success(archive, "origin")
    assert since == datetime(2026, 10, 1, 12, tzinfo=UTC)

    with respx.mock:
        route = respx.get(url__regex=rf"{re.escape(PLANS_URL)}\?.*").mock(
            return_value=httpx.Response(
                200, json={"data": {"plans": []}, "meta": {"totalPages": 1}}
            )
        )
        with PoliteClient(min_interval=0) as client:
            list_changed_plans(
                client,
                Brand("origin", "Origin Energy", ORIGIN_BASE, "ACTIVE", "productBaseUri"),
                since,
            )
    request = route.calls[0].request
    assert request.url.params.get("updated-since") == "2026-10-01T12:00:00Z"
    assert request.url.params["page-size"] == "1000"

    with respx.mock:
        route = respx.get(url__regex=rf"{re.escape(PLANS_URL)}\?.*").mock(
            return_value=httpx.Response(
                200, json={"data": {"plans": []}, "meta": {"totalPages": 1}}
            )
        )
        with PoliteClient(min_interval=0) as client:
            list_changed_plans(
                client,
                Brand("origin", "Origin Energy", ORIGIN_BASE, "ACTIVE", "productBaseUri"),
                load_last_success(archive, "missing"),
            )
    assert "updated-since" not in route.calls[0].request.url.params


def test_list_all_current_ids_does_not_send_updated_since() -> None:
    with respx.mock:
        route = respx.get(url__regex=rf"{re.escape(PLANS_URL)}\?.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {"plans": [{"planId": "ORIGIN-1"}]},
                    "meta": {"totalPages": 1},
                },
            )
        )
        with PoliteClient(min_interval=0) as client:
            ids = list_all_current_ids(
                client, Brand("origin", "Origin", ORIGIN_BASE, "ACTIVE", "productBaseUri")
            )
    assert ids == {"ORIGIN-1"}
    assert "updated-since" not in route.calls[0].request.url.params


def test_cli_reports_one_brand_error_and_continues(tmp_path: Path, monkeypatch, capsys) -> None:
    summary = {
        "data": {
            "brandSummaries": [
                {
                    "dataHolderBrandId": "bad",
                    "brandName": "Bad Brand",
                    "productBaseUri": "https://bad.example",
                    "dataHolderBrandStatus": "ACTIVE",
                },
                {
                    "dataHolderBrandId": "good",
                    "brandName": "Good Brand",
                    "productBaseUri": ORIGIN_BASE,
                    "dataHolderBrandStatus": "ACTIVE",
                },
            ]
        }
    }
    errors_url = "https://bad.example/cds-au/v1/energy/plans"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))

    with respx.mock:
        respx.get(BRANDS_URL).mock(return_value=httpx.Response(200, json=summary))
        respx.get(url__regex=rf"{re.escape(errors_url)}\?.*").mock(
            return_value=httpx.Response(200, json={"errors": [{"code": "TEMPORARY"}]})
        )
        respx.get(url__regex=rf"{re.escape(PLANS_URL)}\?.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "data": {
                        "plans": [
                            {
                                "planId": "GOOD-1",
                                "displayName": "Good Plan",
                                "geography": {"distributors": []},
                            }
                        ]
                    },
                    "meta": {"totalPages": 1},
                },
            )
        )
        assert main(
            [
                "harvest",
                "au-cdr",
                "--list-only",
                "--dry-run",
                "--archive-root",
                str(tmp_path / "archive"),
            ]
        ) == 0

    output = capsys.readouterr().out
    assert "Bad Brand (bad): error" in output
    assert "Good Brand (good): 1 plans" in output
    report = (tmp_path / "summary.md").read_text()
    assert "CDR API returned errors" in report
