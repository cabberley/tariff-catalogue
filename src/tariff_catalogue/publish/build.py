"""Build deterministic static catalogue files from archived plan versions."""

from __future__ import annotations

import importlib.metadata
import json
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

from tariff_core import parse_plan, validate_plan

from tariff_catalogue.harvest.common.archive import ArchiveStore


@dataclass(frozen=True, slots=True)
class BuildReport:
    plans: int
    versions: int
    files: int
    countries: int


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _count_summary(plans: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    commodities = Counter(str(plan["commodity"]) for plan in plans)
    customer_types = Counter(str(plan["customer_type"]) for plan in plans)
    return {
        "commodity": dict(sorted(commodities.items())),
        "customer_type": dict(sorted(customer_types.items())),
    }


def _valid_plan(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    try:
        return not validate_plan(parse_plan(data))
    except Exception:
        return False


def _last_harvest(store: ArchiveStore, state_path: str) -> str | None:
    if not store.exists(state_path):
        return None
    state = store.get_json(state_path)
    brands = state.get("brands", {}) if isinstance(state, dict) else {}
    times: list[str] = []
    if isinstance(brands, dict):
        times.extend(
            value["last_success"]
            for value in brands.values()
            if isinstance(value, dict) and isinstance(value.get("last_success"), str)
        )
    return max(times) if times else None


def _version_files(store: ArchiveStore) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = defaultdict(dict)
    for path in store.list("versions/"):
        parts = PurePosixPath(path).parts
        if len(parts) != 3 or not parts[2].endswith(".json"):
            continue
        result[unquote(parts[1])][parts[2][:-5]] = path
    return result


def _plan_indexes(store: ArchiveStore) -> list[str]:
    return sorted(path for path in store.list("index/") if path.endswith("/plans.json"))


def _plan_entries(store: ArchiveStore) -> dict[str, dict[str, Any]]:
    entries: dict[str, dict[str, Any]] = {}
    for path in _plan_indexes(store):
        value = store.get_json(path)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("plan_id"), str):
                entries[item["plan_id"]] = item
    return entries


def build(store: ArchiveStore, out_dir: Path) -> BuildReport:
    """Build ``out_dir/v1`` from the archive's current plan indexes and versions."""
    out_dir = Path(out_dir)
    version_paths = _version_files(store)
    plan_entries = _plan_entries(store)
    published: list[dict[str, Any]] = []
    published_versions: dict[str, list[dict[str, Any]]] = {}
    copied_versions: list[tuple[str, str, bytes]] = []

    for plan_id, entry in sorted(plan_entries.items()):
        if entry.get("status") not in {"current", "withdrawn"}:
            continue
        latest_hash = entry.get("version_hash")
        archived = version_paths.get(plan_id, {})
        latest_path = archived.get(latest_hash) if isinstance(latest_hash, str) else None
        if latest_path is None:
            continue
        latest_bytes = store.get(latest_path)
        try:
            latest_data = json.loads(latest_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not _valid_plan(latest_data):
            continue

        commodity = entry.get("commodity", latest_data.get("commodity"))
        customer_type = entry.get("customer_type", latest_data.get("customer_type"))
        region = entry.get("region", latest_data.get("region", {}))
        region = region if isinstance(region, dict) else {}
        country = region.get("country")
        country = str(country).lower() if country else "global"
        network = region.get("network")
        region_name = str(network).lower() if network else "global"
        summary = {
            "plan_id": plan_id,
            "display_name": entry.get("display_name", latest_data.get("display_name")),
            "supplier": entry.get("supplier", latest_data.get("supplier")),
            "commodity": commodity,
            "customer_type": customer_type,
            "pricing_model": latest_data.get("pricing_model"),
            "latest_version": latest_hash,
            "version_hash": latest_hash,
            "status": entry["status"],
            "partial": bool(entry.get("partial", latest_data.get("partial", False))),
            "confidence": latest_data.get("confidence"),
            "equivalence_group": entry.get("equivalence_group"),
            "equivalent_count": 1,
            "_country": country,
            "_region": region_name,
        }
        published.append(summary)

        plan_versions: list[dict[str, Any]] = []
        for version_hash, path in sorted(archived.items()):
            version_bytes = latest_bytes if version_hash == latest_hash else store.get(path)
            try:
                version_data = json.loads(version_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not _valid_plan(version_data):
                continue
            effective = version_data.get("effective", {})
            source = version_data.get("source", {})
            first_seen = (
                entry.get("first_seen")
                if version_hash == latest_hash
                else source.get("retrieved_at")
                if isinstance(source, dict)
                else None
            )
            plan_versions.append(
                {
                    "version_hash": version_hash,
                    "effective_from": (
                        effective.get("from") if isinstance(effective, dict) else None
                    ),
                    "effective_to": effective.get("to") if isinstance(effective, dict) else None,
                    "first_seen": first_seen,
                }
            )
            copied_versions.append((plan_id, version_hash, version_bytes))
        plan_versions.sort(key=lambda item: (item["effective_from"] or "", item["version_hash"]))
        for index, version in enumerate(plan_versions[:-1]):
            if version["effective_to"] is None:
                version["effective_to"] = plan_versions[index + 1]["effective_from"]
        published_versions[plan_id] = plan_versions

    equivalence_counts = Counter(
        plan["equivalence_group"]
        for plan in published
        if isinstance(plan["equivalence_group"], str)
    )
    for plan in published:
        group = plan["equivalence_group"]
        if isinstance(group, str):
            plan["equivalent_count"] = equivalence_counts[group]

    published.sort(key=lambda item: item["plan_id"])
    # Do not leave build-only grouping keys in public summaries.
    country_plans: dict[str, list[dict[str, Any]]] = defaultdict(list)
    region_plans: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for plan in published:
        country = plan.pop("_country")
        region = plan.pop("_region")
        country_plans[country].append(plan)
        region_plans[(country, region)].append(plan)

    root = out_dir / "v1"
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    files = 0

    for plan_id, version_hash, contents in copied_versions:
        version_path = root / "plans" / quote(plan_id, safe="") / f"{version_hash}.json"
        version_path.parent.mkdir(parents=True, exist_ok=True)
        version_path.write_bytes(contents)
        files += 1
    for plan_id, versions in sorted(published_versions.items()):
        plan_index_path = root / "plans" / quote(plan_id, safe="") / "index.json"
        plan_index_path.parent.mkdir(parents=True, exist_ok=True)
        plan_index_path.write_bytes(
            _json_bytes({"schema_version": "v1", "plan_id": plan_id, "versions": versions})
        )
        files += 1

    countries: list[dict[str, Any]] = []
    for country, plans in sorted(country_plans.items()):
        countries.append(
            {"country": country, "plan_count": len(plans), "counts": _count_summary(plans)}
        )
        country_path = root / country / "index.json"
        country_path.parent.mkdir(parents=True, exist_ok=True)
        regions: list[dict[str, Any]] = [
            {
                "region": region,
                "plan_count": len(values),
                "counts": _count_summary(values),
            }
            for (region_country, region), values in sorted(region_plans.items())
            if region_country == country
        ]
        country_path.write_bytes(
            _json_bytes({"schema_version": "v1", "country": country, "regions": regions})
        )
        files += 1
        for region in regions:
            region_path = root / country / region["region"] / "index.json"
            region_path.parent.mkdir(parents=True, exist_ok=True)
            region_summary = region_plans[(country, region["region"])]
            region_path.write_bytes(
                _json_bytes(
                    {
                        "schema_version": "v1",
                        "country": country,
                        "region": region["region"],
                        "plans": region_summary,
                    }
                )
            )
            files += 1

    state_paths = sorted(path for path in store.list("state/") if path.endswith(".json"))
    feeds = [
        {"feed": PurePosixPath(path).stem, "last_harvest": _last_harvest(store, path)}
        for path in state_paths
    ]
    top_index = {
        "schema_version": "v1",
        "build_time": datetime.now(UTC).isoformat(),
        "tariff_core_version": importlib.metadata.version("tariff-core"),
        "countries": countries,
        "feeds": feeds,
    }
    (root / "index.json").write_bytes(_json_bytes(top_index))
    files += 1
    return BuildReport(
        plans=len(published),
        versions=len(copied_versions),
        files=files,
        countries=len(countries),
    )
