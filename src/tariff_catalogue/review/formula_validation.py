"""Validation helpers for dynamic-price formula templates."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml
from tariff_core import PlanVersion, parse_plan, validate_plan, version_id


def load_formula_file(
    path: Path, formula_root: Path = Path("formula")
) -> tuple[PlanVersion, dict[str, Any]]:
    """Load a formula template, validating its plan and template metadata."""
    try:
        data: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise ValueError(f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a YAML object")

    plan_data = dict(data)
    template = plan_data.pop("template", None)
    if not isinstance(template, dict) or any(
        not isinstance(template.get(field), str) or not template[field]
        for field in ("price_entity_hint", "expected_unit")
    ):
        raise ValueError(f"{path}: template requires price_entity_hint and expected_unit")
    editable_adders = template.get("editable_adders")
    if editable_adders is not None and (
        not isinstance(editable_adders, dict)
        or not all(
            isinstance(editable_adders.get(field), str) and editable_adders[field]
            for field in ("supplier_markup", "energy_tax")
        )
    ):
        raise ValueError(
            f"{path}: template.editable_adders requires supplier_markup and energy_tax"
        )
    if not isinstance(template.get("notes"), list) or not all(
        isinstance(note, str) and note for note in template["notes"]
    ):
        raise ValueError(f"{path}: template.notes must be a list of non-empty strings")

    try:
        relative = path.relative_to(formula_root)
    except ValueError:
        try:
            relative = path.resolve().relative_to(formula_root.resolve())
        except ValueError:
            relative = path
    if len(relative.parts) != 2 or relative.suffix.lower() not in {".yaml", ".yml"}:
        raise ValueError(f"{path}: formula path must be formula/{{country}}/{{slug}}.yaml")
    country = relative.parts[0]
    slug = relative.stem
    if not re.fullmatch(r"[a-z]{2}", country) or not re.fullmatch(
        r"[a-z0-9]+(?:-[a-z0-9]+)*", slug
    ):
        raise ValueError(f"{path}: formula path must use a lowercase country and slug")
    expected_plan_id = f"{country}:formula:{slug}"
    if plan_data.get("plan_id") != expected_plan_id:
        raise ValueError(f"{path}: plan_id must be {expected_plan_id}")

    try:
        plan = parse_plan(plan_data)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError(f"{path}: plan could not be parsed: {error}") from error
    if (
        plan.source is None
        or plan.source.type is None
        or plan.source.type.value != "formula"
        or plan.confidence is None
        or plan.confidence.value != "unverified"
        or plan.region is None
        or plan.region.country is None
        or plan.region.country.casefold() != country
    ):
        raise ValueError(f"{path}: plan source, confidence, or country metadata is invalid")
    components = plan_data.get("components")
    if not isinstance(components, list) or not any(
        isinstance(component, dict)
        and component.get("kind") == "usage"
        and isinstance(component.get("rate"), dict)
        and component["rate"].get("entity") == "{price_entity}"
        for component in components
    ):
        raise ValueError(f"{path}: a usage rate must use the {{price_entity}} placeholder")
    issues = validate_plan(plan)
    if issues:
        raise ValueError(
            f"{path}: " + "; ".join(f"{issue.path}: {issue.message}" for issue in issues)
        )
    try:
        expected_version_id = version_id(plan)
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error
    if plan.id not in (None, expected_version_id):
        raise ValueError(f"{path}: id does not match the formula plan contents")
    return plan, template


def validate_formula_file(path: Path, formula_root: Path = Path("formula")) -> list[str]:
    """Return validation errors for one formula template."""
    try:
        load_formula_file(path, formula_root)
    except ValueError as error:
        return [str(error)]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    formula_root = args.root / "formula"
    files = args.files or sorted([*formula_root.rglob("*.yaml"), *formula_root.rglob("*.yml")])
    errors = [error for path in files for error in validate_formula_file(path, formula_root)]
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Validated {len(files)} formula template(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
