"""Build report-based issue bodies and validate harvest results."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urljoin


def _read_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Harvest report must contain a JSON object")
    return value


def report_outputs(report: Mapping[str, Any]) -> dict[str, str]:
    details_fetched = sum(
        int(metrics.get("details_fetched", 0))
        for metrics in report.get("brands", {}).values()
        if isinstance(metrics, Mapping)
    )
    return {
        "has_findings": str(bool(report.get("findings"))).lower(),
        "fully_successful": str(
            not report.get("failures", 0)
            and not report.get("detail_failures", 0)
            and not report.get("invalid", 0)
        ).lower(),
        "details_fetched": str(details_fetched),
    }


def check_report(report: Mapping[str, Any]) -> list[str]:
    detail_failures = int(report.get("detail_failures", 0))
    details_fetched = int(report_outputs(report)["details_fetched"])
    detail_attempts = details_fetched + detail_failures
    problems = []
    if detail_attempts and detail_failures / detail_attempts > 0.05:
        problems.append(
            f"{detail_failures} of {detail_attempts} detail fetches failed "
            f"({detail_failures / detail_attempts:.1%}; maximum allowed is 5%)."
        )
    other_failures = max(0, int(report.get("failures", 0)) - detail_failures)
    if other_failures:
        problems.append(f"{other_failures} non-detail harvest failure(s) were reported.")
    return problems


def build_failure_issue_body(report: Mapping[str, Any], run_url: str) -> str:
    lines = [
        "## Harvest failure: au-cdr",
        "",
        f"[View workflow run]({run_url})",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
    ]
    for label, key in (
        ("Requests", "requests"),
        ("Retries", "retries"),
        ("Failures", "failures"),
        ("Detail fetch failures", "detail_failures"),
        ("New versions", "new_versions"),
        ("Unchanged", "unchanged"),
        ("Invalid", "invalid"),
    ):
        lines.append(f"| {label} | {report.get(key, 0)} |")

    errors = report.get("errors", [])
    if errors:
        lines.extend(["", "### Errors", ""])
        lines.extend(f"- {error}" for error in errors)
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "### Warnings", ""])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def build_findings_issue_body(report: Mapping[str, Any], run_url: str, catalogue_url: str) -> str:
    lines = [
        "## Data review: au-cdr",
        "",
        f"[View workflow run]({run_url})",
        "",
    ]
    findings = report.get("findings", {})
    finding_versions = report.get("finding_versions", {})
    for plan_id, plan_findings in sorted(findings.items()):
        for finding in plan_findings:
            version_path = finding_versions.get(plan_id, "")
            version_url = urljoin(catalogue_url.rstrip("/") + "/", version_path)
            profile = f" ({finding['profile']})" if finding.get("profile") else ""
            lines.append(
                f"- [`{plan_id}`]({version_url}) — **{finding.get('code', 'finding')}** "
                f"[{finding.get('severity', 'unknown')}]{profile}: {finding.get('message', '')}"
            )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("check", "failure", "findings"))
    parser.add_argument("--report", type=Path, default=Path("harvest-report.json"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--catalogue-url", default="")
    args = parser.parse_args(argv)
    report = _read_report(args.report)

    if os.getenv("GITHUB_OUTPUT"):
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            for key, value in report_outputs(report).items():
                output.write(f"{key}={value}\n")

    if args.action == "check":
        problems = check_report(report)
        if problems:
            print("\n".join(problems))
            return 1
        return 0

    if args.action == "failure":
        body = build_failure_issue_body(report, args.run_url)
    else:
        catalogue_url = args.catalogue_url
        if not catalogue_url:
            parser.error("--catalogue-url is required for findings issue body generation")
        body = build_findings_issue_body(report, args.run_url, catalogue_url)

    if args.output is None:
        parser.error("--output is required for issue body generation")
    args.output.write_text(body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
