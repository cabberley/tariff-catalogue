"""Generate a Markdown rate-change report for two network tariff YAML files."""

from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml


def _rates(plan: dict[str, Any]) -> dict[str, Decimal]:
    result: dict[str, Decimal] = {}
    for index, component in enumerate(plan.get("components", [])):
        if not isinstance(component, dict):
            continue
        name = str(
            component.get("label")
            or component.get("period_label")
            or component.get("period")
            or component.get("kind", f"component {index + 1}")
        )
        if component.get("schedule") is not None:
            name = f"{name} ({component['schedule']})"
        for field in ("register", "direction", "season"):
            if component.get(field) is not None:
                name = f"{name} {component[field]}"
        if "rate" in component:
            try:
                rate = Decimal(str(component["rate"]))
                if rate.is_finite():
                    result[name] = rate
            except InvalidOperation:
                continue
        blocks = component.get("blocks")
        if isinstance(blocks, dict):
            for tier_index, tier in enumerate(blocks.get("tiers", []), start=1):
                try:
                    rate = Decimal(str(tier["rate"]))
                    if rate.is_finite():
                        result[f"{name} tier {tier_index}"] = rate
                except (InvalidOperation, KeyError, TypeError):
                    continue
    return result


def build_diff_report(old: dict[str, Any], new: dict[str, Any]) -> str:
    old_rates = _rates(old)
    new_rates = _rates(new)
    rows = []
    for name in sorted(old_rates.keys() | new_rates.keys()):
        previous = old_rates.get(name)
        current = new_rates.get(name)
        if previous == current:
            continue
        old_text = str(previous) if previous is not None else "—"
        new_text = str(current) if current is not None else "—"
        if previous is None or current is None or previous == 0:
            change = "—"
        else:
            change = f"{(current - previous) / abs(previous) * 100:.2f}%"
        rows.append(f"| {name} | {old_text} | {new_text} | {change} |")
    return (
        "\n".join(
            [
                "| Component | Old rate | New rate | % change |",
                "| --- | ---: | ---: | ---: |",
                *(rows or ["| No rate changes | — | — | — |"]),
            ]
        )
        + "\n"
    )


def _read_plan(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_diff_report(_read_plan(args.old), _read_plan(args.new))
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
