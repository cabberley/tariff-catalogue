"""Validation helpers for curated network tariff plans."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml
from tariff_core import parse_plan, validate_plan


def validate_network_file(path: Path) -> list[str]:
    """Return validation errors for one curated network tariff YAML file."""
    try:
        value: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        return [f"{path}: {error}"]
    if not isinstance(value, dict):
        return [f"{path}: expected a YAML object"]
    source = value.get("source")
    if not isinstance(source, dict) or not isinstance(source.get("url"), str) or not source["url"]:
        return [f"{path}: source.url is required"]
    if value.get("kind") != "network":
        return [f"{path}: kind must be network"]
    if not isinstance(value.get("plan_id"), str) or not re.fullmatch(
        r"au:network:[a-z0-9]+:[a-z0-9_]+:[A-Za-z0-9_-]+", value["plan_id"]
    ):
        return [f"{path}: plan_id must match au:network:{'{dnsp}:{zone}:{code}'}"]
    try:
        relative = path.relative_to(Path("network"))
    except ValueError:
        relative = path
    if len(relative.parts) == 5 and relative.parts[0] == "au":
        dnsp, _year, zone, filename = relative.parts[1:]
        expected_id = f"au:network:{dnsp}:{zone}:{Path(filename).stem}"
        if value["plan_id"] != expected_id:
            return [f"{path}: plan_id must match its network path ({expected_id})"]
    try:
        plan = parse_plan(value)
    except (TypeError, ValueError) as error:
        return [f"{path}: {error}"]
    issues = validate_plan(plan)
    return [f"{path}: {issue.path}: {issue.message}" for issue in issues]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("files", nargs="*", type=Path)
    args = parser.parse_args(argv)
    files = args.files or sorted(Path("network").rglob("*.yaml"))
    errors = [error for path in files for error in validate_network_file(path)]
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Validated {len(files)} network tariff file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
