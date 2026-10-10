"""Run-level harvest statistics and summaries."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TypedDict


class _ReportData(TypedDict):
    requests: int
    retries: int
    failures: int
    detail_failures: int
    new_versions: int
    unchanged: int
    partial: int
    invalid: int
    brands: dict[str, dict[str, int]]
    durations: dict[str, float]
    errors: list[str]
    warnings: list[str]
    findings: dict[str, list[dict[str, str]]]
    finding_versions: dict[str, str]


@dataclass
class RunReport:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    detail_failures: int = 0
    new_versions: int = 0
    unchanged: int = 0
    partial: int = 0
    invalid: int = 0
    brands: dict[str, dict[str, int]] = field(default_factory=dict)
    durations: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    findings: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    finding_versions: dict[str, str] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def record_request(self) -> None:
        with self._lock:
            self.requests += 1

    def record_retry(self) -> None:
        with self._lock:
            self.retries += 1

    def record_failure(self, error: Exception | str) -> None:
        with self._lock:
            self.failures += 1
            self.errors.append(str(error))

    def record_warning(self, warning: str) -> None:
        with self._lock:
            self.warnings.append(warning)

    def record_findings(
        self, plan_id: str, findings: list[dict[str, str]], version_path: str | None = None
    ) -> None:
        with self._lock:
            if findings:
                self.findings[plan_id] = [dict(finding) for finding in findings]
                if version_path is not None:
                    self.finding_versions[plan_id] = version_path
            else:
                self.findings.pop(plan_id, None)
                self.finding_versions.pop(plan_id, None)

    def record_version(self, status: str) -> None:
        if status not in {"new", "unchanged", "partial"}:
            raise ValueError(f"Unknown version status: {status}")
        with self._lock:
            if status == "new":
                self.new_versions += 1
            elif status == "unchanged":
                self.unchanged += 1
            else:
                self.partial += 1

    def record_brand_metric(self, brand: str, metric: str, amount: int = 1) -> None:
        with self._lock:
            metrics = self.brands.setdefault(
                brand,
                {
                    "plans_listed": 0,
                    "details_fetched": 0,
                    "new": 0,
                    "unchanged": 0,
                    "invalid": 0,
                    "partial": 0,
                },
            )
            metrics[metric] = metrics.get(metric, 0) + amount

    def record_invalid(self, brand: str, error: Exception | str) -> None:
        with self._lock:
            self.invalid += 1
            metrics = self.brands.setdefault(
                brand,
                {
                    "plans_listed": 0,
                    "details_fetched": 0,
                    "new": 0,
                    "unchanged": 0,
                    "invalid": 0,
                    "partial": 0,
                },
            )
            metrics["invalid"] += 1
            self.errors.append(str(error))

    def record_duration(self, name: str, seconds: float) -> None:
        with self._lock:
            self.durations[name] = self.durations.get(name, 0.0) + seconds

    def to_dict(self) -> _ReportData:
        with self._lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "failures": self.failures,
                "detail_failures": self.detail_failures,
                "new_versions": self.new_versions,
                "unchanged": self.unchanged,
                "partial": self.partial,
                "invalid": self.invalid,
                "brands": {brand: dict(metrics) for brand, metrics in self.brands.items()},
                "durations": dict(self.durations),
                "errors": list(self.errors),
                "warnings": list(self.warnings),
                "findings": {
                    plan_id: [dict(finding) for finding in findings]
                    for plan_id, findings in self.findings.items()
                },
                "finding_versions": dict(self.finding_versions),
            }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    def to_markdown(self) -> str:
        data = self.to_dict()
        lines = [
            "## Harvest run summary",
            "",
            "| Metric | Count |",
            "| --- | ---: |",
            f"| Requests | {data['requests']} |",
            f"| Retries | {data['retries']} |",
            f"| Failures | {data['failures']} |",
            f"| Detail fetch failures | {data['detail_failures']} |",
            f"| New versions | {data['new_versions']} |",
            f"| Unchanged | {data['unchanged']} |",
            f"| Partial | {data['partial']} |",
            f"| Invalid | {data['invalid']} |",
        ]
        if data["brands"]:
            lines.extend(
                [
                    "",
                    "### Brands",
                    "",
                    "| Brand | Listed | Fetched | New | Unchanged | Invalid | Partial |",
                    "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
                ]
            )
            lines.extend(
                f"| {brand} | {metrics['plans_listed']} | {metrics['details_fetched']} "
                f"| {metrics['new']} | {metrics['unchanged']} | {metrics['invalid']} "
                f"| {metrics['partial']} |"
                for brand, metrics in sorted(data["brands"].items())
            )
        durations = data["durations"]
        if durations:
            lines.extend(["", "### Durations", "", "| Operation | Seconds |", "| --- | ---: |"])
            lines.extend(
                f"| {name} | {seconds:.2f} |" for name, seconds in sorted(durations.items())
            )
        errors = data["errors"]
        if errors:
            lines.extend(["", "### Errors", ""])
            lines.extend(f"- {error}" for error in errors)
        warnings = data["warnings"]
        if warnings:
            lines.extend(["", "### Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
        findings = data["findings"]
        if findings:
            lines.extend(["", "### Synthetic bill findings", ""])
            for plan_id, plan_findings in sorted(findings.items()):
                details = []
                for finding in plan_findings:
                    profile = f" ({finding['profile']})" if "profile" in finding else ""
                    details.append(
                        f"{finding['code']} [{finding['severity']}]{profile}: {finding['message']}"
                    )
                lines.append(f"- `{plan_id}`: {'; '.join(details)}")
        return "\n".join(lines) + "\n"

    def write_summary(self, path: str | Path | None = None) -> bool:
        summary_path = path if path is not None else os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path is None:
            return False
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(self.to_markdown())
        return True
