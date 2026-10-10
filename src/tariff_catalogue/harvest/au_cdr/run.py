"""Harvest, validate, and version changed Australian CDR plans."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from tariff_core import PlanVersion, content_hash, from_cdr, parse_plan, to_dict, validate_plan

from tariff_catalogue.checks.synthetic import check_version, lower_confidence
from tariff_catalogue.harvest.au_cdr.brands import Brand, CDRResponseError, discover_brands
from tariff_catalogue.harvest.au_cdr.detail import fetch_detail
from tariff_catalogue.harvest.au_cdr.listing import (
    PlanSummary,
    list_all_current_ids,
    list_changed_plans,
    load_last_success,
)
from tariff_catalogue.harvest.common.archive import ArchiveStore
from tariff_catalogue.harvest.common.http import HarvestHTTPError, PoliteClient
from tariff_catalogue.harvest.common.report import RunReport

INDEX_PATH = "index/au_cdr/plans.json"
STATE_PATH = "state/au_cdr.json"


def _raw_bytes(data: dict[str, Any], metadata: dict[str, Any]) -> bytes:
    raw_body = metadata.get("raw_body")
    if isinstance(raw_body, bytes):
        return raw_body
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _archive_detail(
    store: ArchiveStore,
    brand: Brand,
    summary: PlanSummary,
    data: dict[str, Any],
    metadata: dict[str, Any],
) -> None:
    raw_body = _raw_bytes(data, metadata)
    digest = hashlib.sha256(raw_body).hexdigest()
    retrieved_at = datetime.now(UTC)
    detail = data.get("data")
    last_updated = (
        detail.get("lastUpdated")
        if isinstance(detail, dict) and isinstance(detail.get("lastUpdated"), str)
        else summary.last_updated.isoformat()
        if summary.last_updated is not None
        else "unknown"
    )
    archive_metadata = {key: value for key, value in metadata.items() if key != "raw_body"}
    archive_metadata.update(
        {
            "brand_id": brand.brand_id,
            "plan_id": summary.plan_id,
            "retrieved_at": retrieved_at.isoformat(),
        }
    )
    store.put_raw(
        "au_cdr_detail",
        f"{brand.brand_id}/{summary.plan_id}/{last_updated}/{digest}/{retrieved_at.isoformat()}",
        raw_body,
        archive_metadata,
    )


def _load_index(store: ArchiveStore) -> list[dict[str, Any]]:
    if not store.exists(INDEX_PATH):
        return []
    value = store.get_json(INDEX_PATH)
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError(f"{INDEX_PATH} must contain a list of plan objects")
    return value


def _pricing_hash(plan: Any) -> str:
    value = to_dict(plan)
    region = value.get("region", {})
    content = {
        "components": value.get("components", []),
        "schedules": value.get("schedules", {}),
        "seasons": value.get("seasons", {}),
        "tax": value.get("tax"),
        "region": {"network": region.get("network")} if isinstance(region, dict) else {},
    }
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _index_entry(
    plan: Any,
    version_hash: str,
    previous: dict[str, Any] | None,
    now: datetime,
    finding_codes: list[str],
) -> dict[str, Any]:
    value = to_dict(plan)
    effective = value.get("effective", {})
    supplier = value.get("supplier", {})
    return {
        "plan_id": plan.plan_id,
        "version_hash": version_hash,
        "effective_from": effective.get("from") if isinstance(effective, dict) else None,
        "commodity": plan.commodity.value,
        "customer_type": plan.customer_type.value,
        "region": value.get("region", {}),
        "supplier": supplier,
        "display_name": plan.display_name,
        "status": "current",
        "partial": plan.partial,
        "confidence": value.get("confidence"),
        "first_seen": previous.get("first_seen", now.isoformat()) if previous else now.isoformat(),
        "last_seen": now.isoformat(),
        "equivalence_group": _pricing_hash(plan),
        "finding_codes": finding_codes,
    }


def _previous_plan(
    store: ArchiveStore,
    plan_id: str,
    previous: dict[str, Any] | None,
    report: RunReport,
) -> PlanVersion | None:
    version_hash = previous.get("version_hash") if previous else None
    if not isinstance(version_hash, str):
        return None
    version_path = f"versions/{quote(plan_id, safe=':@')}/{version_hash}.json"
    if not store.exists(version_path):
        report.record_warning(f"Previous version for plan {plan_id} is missing from the archive.")
        return None
    try:
        return parse_plan(store.get_json(version_path))
    except Exception as error:
        report.record_warning(f"Could not load previous version for plan {plan_id}: {error}")
        return None


def _plan_key(plan_id: str) -> str:
    return plan_id if plan_id.startswith("au:cdr:") else f"au:cdr:{plan_id}"


def _set_current_timestamp(
    index: dict[str, dict[str, Any]], plan_id: str, brand_id: str, timestamp: str
) -> None:
    entry = index.get(_plan_key(plan_id))
    if entry is None:
        return
    supplier = entry.get("supplier")
    if isinstance(supplier, dict) and supplier.get("id") == brand_id:
        entry["status"] = "current"
        entry["last_seen"] = timestamp
        entry.pop("withdrawn_at", None)


def _read_state(store: ArchiveStore) -> dict[str, Any]:
    if not store.exists(STATE_PATH):
        return {"brands": {}}
    state = store.get_json(STATE_PATH)
    if not isinstance(state, dict):
        raise ValueError(f"{STATE_PATH} must contain an object")
    brands = state.setdefault("brands", {})
    if not isinstance(brands, dict):
        raise ValueError(f"{STATE_PATH} brands must contain an object")
    return state


def _fetch_and_archive(
    client: PoliteClient,
    store: ArchiveStore,
    brand: Brand,
    summary: PlanSummary,
    dry_run: bool,
    report: RunReport,
) -> tuple[PlanSummary, dict[str, Any], dict[str, Any]]:
    data, metadata = fetch_detail(client, brand, summary.plan_id)
    report.record_brand_metric(brand.brand_id, "details_fetched")
    if not dry_run:
        _archive_detail(store, brand, summary, data, metadata)
    return summary, data, metadata


def run_au_cdr(
    client: PoliteClient,
    store: ArchiveStore,
    *,
    brands: Sequence[Brand] | None = None,
    dry_run: bool = False,
    full: bool = False,
) -> RunReport:
    """Fetch changed plans and persist only valid, previously unseen versions."""
    report = getattr(client, "_report", None) or RunReport()
    failures_before = report.failures
    try:
        selected_brands = list(brands) if brands is not None else discover_brands(client, report)
        index_by_id = {entry["plan_id"]: entry for entry in _load_index(store)}
    except Exception as error:
        if isinstance(error, CDRResponseError) or report.failures == failures_before:
            report.record_failure(error)
        return report
    now = datetime.now(UTC)
    timestamp = now.isoformat()

    for brand in selected_brands:
        failures_before = report.failures
        try:
            since = load_last_success(store, brand.brand_id)
            plans = list_changed_plans(client, brand, since, None if dry_run else store)
            report.record_brand_metric(brand.brand_id, "plans_listed", len(plans))
            all_current_ids = (
                list_all_current_ids(client, brand, None if dry_run else store) if full else None
            )
        except Exception as error:
            if isinstance(error, CDRResponseError) or report.failures == failures_before:
                report.record_failure(error)
            report.record_warning(f"{brand.brand_name} ({brand.brand_id}) listing failed: {error}")
            continue

        for current_id in all_current_ids or ():
            _set_current_timestamp(index_by_id, current_id, brand.brand_id, timestamp)
        for summary in plans:
            _set_current_timestamp(index_by_id, summary.plan_id, brand.brand_id, timestamp)

        futures: dict[Future[tuple[PlanSummary, dict[str, Any], dict[str, Any]]], PlanSummary] = {}
        fetches: list[tuple[PlanSummary, dict[str, Any], dict[str, Any]]] = []
        with ThreadPoolExecutor(max_workers=max(1, client.max_concurrency_per_host)) as executor:
            for summary in plans:
                futures[
                    executor.submit(
                        _fetch_and_archive, client, store, brand, summary, dry_run, report
                    )
                ] = summary
            for future in as_completed(futures):
                try:
                    fetches.append(future.result())
                except Exception as error:
                    if (
                        isinstance(error, CDRResponseError)
                        or not isinstance(
                            error,
                            (
                                HarvestHTTPError,
                                httpx.HTTPError,
                                json.JSONDecodeError,
                                UnicodeDecodeError,
                            ),
                        )
                        or getattr(client, "_report", None) is None
                    ):
                        report.record_failure(error)
                    report.record_warning(
                        f"{brand.brand_name} plan {futures[future].plan_id} detail failed: {error}"
                    )

        fetches_succeeded = len(fetches) == len(plans)
        for summary, data, metadata in fetches:
            retrieved_at = datetime.now(UTC)
            try:
                plans_from_detail = from_cdr(
                    data,
                    retrieved_at=retrieved_at,
                    url=metadata.get("final_url"),
                )
                if not plans_from_detail:
                    raise ValueError("CDR adapter returned no plan versions")
            except Exception as error:
                report.record_invalid(brand.brand_id, f"{summary.plan_id}: {error}")
                continue

            for plan in plans_from_detail:
                validation_errors = validate_plan(plan)
                if validation_errors:
                    message = "; ".join(
                        f"{issue.path}: {issue.message}" for issue in validation_errors
                    )
                    report.record_invalid(brand.brand_id, f"{summary.plan_id}: {message}")
                    continue
                plan_id = plan.plan_id
                if plan_id is None:
                    report.record_invalid(brand.brand_id, f"{summary.plan_id}: missing plan_id")
                    continue

                previous = index_by_id.get(plan_id)
                previous_plan = _previous_plan(store, plan_id, previous, report)
                findings = check_version(plan, previous_plan)
                plan = lower_confidence(plan, findings)
                version_hash = content_hash(plan)
                version_path = f"versions/{quote(plan.plan_id, safe=':@')}/{version_hash}.json"
                exists = store.exists(version_path)
                status = "unchanged" if exists else "new"
                report.record_version(status)
                report.record_brand_metric(brand.brand_id, status)
                if plan.partial:
                    report.record_version("partial")
                    report.record_brand_metric(brand.brand_id, "partial")
                if not exists and not dry_run:
                    store.put_json(version_path, plan)

                finding_data = [finding.to_dict() for finding in findings]
                report.record_findings(plan_id, finding_data)
                if finding_data and not dry_run:
                    checks_path = f"checks/{quote(plan_id, safe=':@')}/{version_hash}.json"
                    store.put_json(checks_path, finding_data)
                finding_codes = sorted({finding.code for finding in findings})
                index_by_id[plan_id] = _index_entry(
                    plan, version_hash, previous, now, finding_codes
                )

        if all_current_ids is not None:
            current_keys = {_plan_key(plan_id) for plan_id in all_current_ids}
            for plan_id, entry in index_by_id.items():
                supplier = entry.get("supplier")
                if (
                    plan_id not in current_keys
                    and isinstance(supplier, dict)
                    and supplier.get("id") == brand.brand_id
                ):
                    entry["status"] = "withdrawn"
                    entry.setdefault("withdrawn_at", timestamp)

        if not dry_run:
            if fetches_succeeded:
                state = _read_state(store)
                state["brands"][brand.brand_id] = {"last_success": timestamp}
                store.put_json(STATE_PATH, state)
            sorted_index = sorted(index_by_id.values(), key=lambda item: item["plan_id"])
            store.put_json(INDEX_PATH, sorted_index)

    return report
