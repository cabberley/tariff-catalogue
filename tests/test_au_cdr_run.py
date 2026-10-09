import copy
import json
from pathlib import Path

import httpx
import respx

from tariff_catalogue.harvest.au_cdr.brands import Brand
from tariff_catalogue.harvest.au_cdr.detail import fetch_detail
from tariff_catalogue.harvest.au_cdr.run import INDEX_PATH, run_au_cdr
from tariff_catalogue.harvest.common.archive import LocalArchiveStore
from tariff_catalogue.harvest.common.http import PoliteClient
from tariff_catalogue.harvest.common.report import RunReport

FIXTURES = Path(__file__).parent / "fixtures" / "cdr" / "detail"
ORIGIN_BASE = "https://cdr.energymadeeasy.gov.au/origin"
PLANS_URL = f"{ORIGIN_BASE}/cds-au/v1/energy/plans"
DETAIL_PATH = "/cds-au/v1/energy/plans"
DETAIL_PLAN_ID = "ORI1161031MRE3@EME"
DETAIL_URL = f"{ORIGIN_BASE}{DETAIL_PATH}/{DETAIL_PLAN_ID}"
BRAND = Brand("origin", "Origin Energy", ORIGIN_BASE, "ACTIVE")


def _detail() -> dict:
    return json.loads((FIXTURES / f"{DETAIL_PLAN_ID}.json").read_text())


def _listing(*plan_ids: str) -> dict:
    return {
        "data": {
            "plans": [
                {
                    "planId": plan_id,
                    "displayName": f"Plan {index}",
                    "lastUpdated": "2026-09-30T14:06:33.916Z",
                }
                for index, plan_id in enumerate(plan_ids)
            ]
        },
        "meta": {"totalPages": 1},
    }


def test_fetch_detail_falls_back_after_406() -> None:
    with respx.mock:
        route = respx.get(DETAIL_URL).mock(
            side_effect=[
                httpx.Response(406),
                httpx.Response(200, json={"data": {"planId": DETAIL_PLAN_ID}}),
            ]
        )
        with PoliteClient(min_interval=0) as client:
            data, _metadata = fetch_detail(client, BRAND, DETAIL_PLAN_ID)

    assert [call.request.headers["x-v"] for call in route.calls] == ["3", "2"]
    assert data["data"]["planId"] == DETAIL_PLAN_ID


def test_run_creates_versions_deduplicates_and_detects_rate_change(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive")
    detail = _detail()
    report = RunReport()
    with respx.mock:
        listing_route = respx.get(url__regex=rf"{PLANS_URL}\?.*").mock(
            return_value=httpx.Response(200, json=_listing(DETAIL_PLAN_ID))
        )
        detail_route = respx.get(DETAIL_URL).mock(
            side_effect=lambda _request: httpx.Response(200, json=detail)
        )
        with PoliteClient(min_interval=0, report=report) as client:
            run_au_cdr(client, store, brands=[BRAND])
            run_au_cdr(client, store, brands=[BRAND])
            detail["data"]["electricityContract"]["tariffPeriod"][0]["timeOfUseRates"][0][
                "rates"
            ][0]["unitPrice"] = "0.38955"
            run_au_cdr(client, store, brands=[BRAND])

    assert listing_route.call_count == 3
    assert detail_route.call_count == 3
    versions = list(store.list("versions/"))
    assert len(versions) == 2
    assert report.new_versions == 2
    assert report.unchanged == 1
    assert report.brands["origin"]["plans_listed"] == 3
    assert report.brands["origin"]["details_fetched"] == 3
    assert "| origin | 3 | 3 | 2 | 1 | 0 | 0 |" in report.to_markdown()
    index = store.get_json(INDEX_PATH)
    assert any(index[0]["latest_version_hash"] in path for path in versions)
    assert len(list(store.list("raw/au_cdr_detail/"))) == 6


def test_invalid_plan_is_archived_but_not_versioned(tmp_path: Path, monkeypatch) -> None:
    from tariff_core.validate import ValidationIssue

    from tariff_catalogue.harvest.au_cdr import run as run_module

    store = LocalArchiveStore(tmp_path / "archive")
    monkeypatch.setattr(
        run_module,
        "validate_plan",
        lambda _plan: [ValidationIssue("$.components", "invalid fixture")],
    )
    report = RunReport()
    with respx.mock:
        respx.get(url__regex=rf"{PLANS_URL}\?.*").mock(
            return_value=httpx.Response(200, json=_listing(DETAIL_PLAN_ID))
        )
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=_detail()))
        with PoliteClient(min_interval=0, report=report) as client:
            run_au_cdr(client, store, brands=[BRAND])

    assert report.invalid == 1
    assert report.brands["origin"]["invalid"] == 1
    assert not list(store.list("versions/"))
    assert list(store.list("raw/au_cdr_detail/"))


def test_equivalence_group_ignores_display_name(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive")
    base = _detail()
    second = copy.deepcopy(base)
    first_id = base["data"]["planId"]
    second_id = "ORI1161031MRE3-BROKER@EME"
    second["data"]["planId"] = second_id
    second["data"]["displayName"] = "Broker-labelled equivalent"
    report = RunReport()
    with respx.mock:
        respx.get(url__regex=rf"{PLANS_URL}\?.*").mock(
            return_value=httpx.Response(200, json=_listing(first_id, second_id))
        )
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=base))
        respx.get(f"{ORIGIN_BASE}{DETAIL_PATH}/{second_id}").mock(
            return_value=httpx.Response(200, json=second)
        )
        with PoliteClient(min_interval=0, report=report) as client:
            run_au_cdr(client, store, brands=[BRAND])

    entries = store.get_json(INDEX_PATH)
    assert len(entries) == 2
    assert entries[0]["equivalence_group"] == entries[1]["equivalence_group"]


def test_full_run_marks_missing_plans_withdrawn(tmp_path: Path) -> None:
    store = LocalArchiveStore(tmp_path / "archive")
    with respx.mock:
        calls = 0

        def listing_response(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(
                200,
                json=_listing(DETAIL_PLAN_ID) if calls == 1 else _listing(),
            )

        respx.get(url__regex=rf"{PLANS_URL}\?.*").mock(side_effect=listing_response)
        respx.get(DETAIL_URL).mock(return_value=httpx.Response(200, json=_detail()))
        with PoliteClient(min_interval=0) as client:
            run_au_cdr(client, store, brands=[BRAND])
            run_au_cdr(client, store, brands=[BRAND], full=True)

    entry = store.get_json(INDEX_PATH)[0]
    assert entry["status"] == "withdrawn"
    assert "withdrawn_at" in entry
