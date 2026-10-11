import json
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from tariff_core import parse_plan, rate_at

from tariff_catalogue.harvest.common.archive import LocalArchiveStore
from tariff_catalogue.publish.build import build
from tariff_catalogue.review.formula_validation import load_formula_file, validate_formula_file

FORMULA_ROOT = Path(__file__).parents[1] / "formula"


@pytest.mark.parametrize(
    ("relative_path", "price", "expected_rate"),
    [
        ("au/amber-general.yaml", "0.27", "0.27"),
        ("nl/dynamic-generic.yaml", "0.10", "0.121"),
    ],
)
def test_formula_template_validates_and_resolves(
    relative_path: str, price: str, expected_rate: str
) -> None:
    path = FORMULA_ROOT / relative_path
    plan, metadata = load_formula_file(path, FORMULA_ROOT)

    assert plan.source is not None
    assert plan.source.type == "formula"
    assert metadata["expected_unit"]
    assert validate_formula_file(path, FORMULA_ROOT) == []

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.pop("template")
    for component in data["components"]:
        rate = component.get("rate")
        if isinstance(rate, dict) and rate.get("entity") == "{price_entity}":
            rate["entity"] = "sensor.example_price"
    resolved_plan = parse_plan(data)
    result = rate_at(
        resolved_plan,
        datetime(2026, 10, 1, tzinfo=UTC),
        resolved={"sensor.example_price": Decimal(price)},
    )

    assert result.rate == Decimal(expected_rate)


def test_build_publishes_formula_plan_versions_and_country_indexes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    shutil.copytree(FORMULA_ROOT, tmp_path / "formula", ignore=shutil.ignore_patterns("README.md"))
    report = build(LocalArchiveStore(tmp_path / "archive"), tmp_path / "dist")

    assert report.plans == 2
    assert report.versions == 2
    for country, slug in (("au", "amber-general"), ("nl", "dynamic-generic")):
        formula_index_path = tmp_path / "dist" / "v1" / country / "formula" / "index.json"
        formula_index = json.loads(formula_index_path.read_text(encoding="utf-8"))
        assert formula_index["schema_version"] == "v1"
        assert formula_index["templates"][0]["plan_id"] == f"{country}:formula:{slug}"

        plan_id = f"{country}:formula:{slug}"
        plan_index_path = (
            tmp_path / "dist" / "v1" / "plans" / quote(plan_id, safe="") / "index.json"
        )
        plan_index = json.loads(plan_index_path.read_text(encoding="utf-8"))
        version_hash = plan_index["versions"][0]["version_hash"]
        plan_path = (
            tmp_path / "dist" / "v1" / "plans" / quote(plan_id, safe="") / f"{version_hash}.json"
        )
        published_plan = json.loads(plan_path.read_text(encoding="utf-8"))
        assert published_plan["source"]["type"] == "formula"
        assert "template" not in published_plan
        region_index = json.loads(
            (tmp_path / "dist" / "v1" / country / "global" / "index.json").read_text(
                encoding="utf-8"
            )
        )
        assert any(plan["plan_id"] == plan_id for plan in region_index["plans"])
