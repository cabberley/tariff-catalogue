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
    new_versions: int
    unchanged: int
    partial: int
    durations: dict[str, float]
    errors: list[str]
    warnings: list[str]


@dataclass
class RunReport:
    requests: int = 0
    retries: int = 0
    failures: int = 0
    new_versions: int = 0
    unchanged: int = 0
    partial: int = 0
    durations: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
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

    def record_duration(self, name: str, seconds: float) -> None:
        with self._lock:
            self.durations[name] = self.durations.get(name, 0.0) + seconds

    def to_dict(self) -> _ReportData:
        with self._lock:
            return {
                "requests": self.requests,
                "retries": self.retries,
                "failures": self.failures,
                "new_versions": self.new_versions,
                "unchanged": self.unchanged,
                "partial": self.partial,
                "durations": dict(self.durations),
                "errors": list(self.errors),
                "warnings": list(self.warnings),
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
            f"| New versions | {data['new_versions']} |",
            f"| Unchanged | {data['unchanged']} |",
            f"| Partial | {data['partial']} |",
        ]
        durations = data["durations"]
        if durations:
            lines.extend(
                ["", "### Durations", "", "| Operation | Seconds |", "| --- | ---: |"]
            )
            lines.extend(
                f"| {name} | {seconds:.2f} |"
                for name, seconds in sorted(durations.items())
            )
        errors = data["errors"]
        if errors:
            lines.extend(["", "### Errors", ""])
            lines.extend(f"- {error}" for error in errors)
        warnings = data["warnings"]
        if warnings:
            lines.extend(["", "### Warnings", ""])
            lines.extend(f"- {warning}" for warning in warnings)
        return "\n".join(lines) + "\n"

    def write_summary(self, path: str | Path | None = None) -> bool:
        summary_path = path if path is not None else os.getenv("GITHUB_STEP_SUMMARY")
        if summary_path is None:
            return False
        with Path(summary_path).open("a", encoding="utf-8") as summary:
            summary.write(self.to_markdown())
        return True
