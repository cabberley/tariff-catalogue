"""List current Australian CDR plans."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from tariff_catalogue.harvest.au_cdr.brands import Brand, CDRResponseError
from tariff_catalogue.harvest.common.archive import ArchiveStore
from tariff_catalogue.harvest.common.http import PoliteClient

PLANS_PATH = "/cds-au/v1/energy/plans"


@dataclass(frozen=True, slots=True)
class PlanSummary:
    plan_id: str
    display_name: str
    fuel_type: str
    customer_type: str
    type: str
    effective_from: datetime | None
    last_updated: datetime | None
    distributors: tuple[str, ...]


def _timestamp(value: Any, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CDRResponseError(f"CDR plan {field} must be an ISO 8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CDRResponseError(f"CDR plan has invalid {field}: {value}") from error
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _request_url(brand: Brand, page: int, since: datetime | None) -> str:
    params: list[tuple[str, str]] = [
        ("type", "ALL"),
        ("fuelType", "ALL"),
        ("effective", "CURRENT"),
        ("page-size", "1000"),
        ("page", str(page)),
    ]
    if since is not None:
        timestamp = since
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        updated_since = timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")
        params.append(("updated-since", updated_since))
    return f"{brand.product_base_uri}{PLANS_PATH}?{urlencode(params)}"


def _plan_summaries(payload: Any) -> tuple[list[dict[str, Any]], int]:
    if isinstance(payload, dict) and payload.get("errors"):
        raise CDRResponseError(f"CDR API returned errors: {payload['errors']}")
    try:
        plans = payload["data"]["plans"]
        metadata = payload.get("meta", {})
    except (KeyError, TypeError) as error:
        raise CDRResponseError("CDR plan listing response has no data.plans") from error
    if not isinstance(plans, list) or any(not isinstance(plan, dict) for plan in plans):
        raise CDRResponseError("CDR data.plans must be a list of objects")
    try:
        total_pages = int(metadata.get("totalPages", 1))
    except (AttributeError, TypeError, ValueError) as error:
        raise CDRResponseError("CDR listing meta.totalPages must be an integer") from error
    if total_pages < 1:
        raise CDRResponseError("CDR listing meta.totalPages must be at least 1")
    return plans, total_pages


def _parse_plan(plan: dict[str, Any]) -> PlanSummary:
    plan_id = plan.get("planId")
    display_name = plan.get("displayName")
    if not isinstance(plan_id, str) or not plan_id:
        raise CDRResponseError("CDR plan is missing planId")
    if not isinstance(display_name, str):
        display_name = ""
    geography = plan.get("geography", {})
    distributors = geography.get("distributors", []) if isinstance(geography, dict) else []
    if not isinstance(distributors, list) or any(
        not isinstance(value, str) for value in distributors
    ):
        raise CDRResponseError(f"CDR plan {plan_id} has invalid geography.distributors")
    return PlanSummary(
        plan_id=plan_id,
        display_name=display_name,
        fuel_type=str(plan.get("fuelType", "")),
        customer_type=str(plan.get("customerType", "")),
        type=str(plan.get("type", "")),
        effective_from=_timestamp(plan.get("effectiveFrom"), "effectiveFrom"),
        last_updated=_timestamp(plan.get("lastUpdated"), "lastUpdated"),
        distributors=tuple(distributors),
    )


def _archive_page(
    archive: ArchiveStore | None,
    brand: Brand,
    page: int,
    payload: Any,
    response_meta: dict[str, Any],
) -> None:
    if archive is None:
        return
    raw_body = response_meta.get("raw_body")
    if not isinstance(raw_body, bytes):
        raw_body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    metadata = {key: value for key, value in response_meta.items() if key != "raw_body"}
    retrieved_at = datetime.now(UTC)
    metadata["retrieved_at"] = retrieved_at.isoformat()
    archive.put_raw(
        "au_cdr_list",
        f"{brand.brand_id}/page-{page}/{retrieved_at.isoformat()}",
        raw_body,
        metadata,
    )


def list_changed_plans(
    client: PoliteClient,
    brand: Brand,
    since: datetime | None,
    archive: ArchiveStore | None = None,
) -> list[PlanSummary]:
    """List current plans changed since ``since`` (or all current plans)."""
    results: list[PlanSummary] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        url = _request_url(brand, page, since)
        payload, response_meta = client.get_json(url, headers={"x-v": "1"})
        _archive_page(archive, brand, page, payload, response_meta)
        page_plans, total_pages = _plan_summaries(payload)
        results.extend(_parse_plan(plan) for plan in page_plans)
        page += 1
    return results


def list_all_current_ids(
    client: PoliteClient,
    brand: Brand,
    archive: ArchiveStore | None = None,
) -> set[str]:
    """Return IDs of all current plans, without an updated-since filter."""
    return {plan.plan_id for plan in list_changed_plans(client, brand, None, archive)}


def load_last_success(archive: ArchiveStore, brand_id: str) -> datetime | None:
    """Read a brand's successful-run timestamp, if one has been persisted."""
    state_path = "state/au_cdr.json"
    if not archive.exists(state_path):
        return None
    state = archive.get_json(state_path)
    if not isinstance(state, dict):
        raise ValueError("state/au_cdr.json must contain an object")
    brands = state.get("brands", {})
    brand_state = brands.get(brand_id, {}) if isinstance(brands, dict) else {}
    value = brand_state.get("last_success") if isinstance(brand_state, dict) else None
    if value is None:
        return None
    return _timestamp(value, "last_success")
