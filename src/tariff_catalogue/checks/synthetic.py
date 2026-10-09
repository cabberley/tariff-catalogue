"""Synthetic annual bills used to flag suspicious tariff versions."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from tariff_core import (
    BillingPeriod,
    Commodity,
    Confidence,
    Contract,
    Conversion,
    Interval,
    PlanVersion,
    UsageComponent,
    bill,
)

PROFILE_DIR = Path(__file__).parents[3] / "tests" / "profiles"
GAS_HEATING_VALUE = Decimal("38.6")
GAS_CORRECTION_FACTOR = Decimal("0.98")


@dataclass(frozen=True, slots=True)
class CheckFinding:
    code: str
    severity: str
    message: str
    profile: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.profile is not None:
            value["profile"] = self.profile
        return value


@dataclass(frozen=True, slots=True)
class _Profile:
    name: str
    commodity: Commodity
    unit: str
    start: datetime
    end: datetime
    intervals: tuple[Interval, ...]


@lru_cache(maxsize=None)
def _load_profile(name: str) -> _Profile:
    path = PROFILE_DIR / f"{name}.csv"
    unit = "m3" if name == "gas_household" else "kWh"
    commodity = Commodity.GAS if name == "gas_household" else Commodity.ELECTRICITY
    duration = timedelta(days=1) if commodity == Commodity.GAS else timedelta(minutes=30)
    intervals: list[Interval] = []
    with path.open(encoding="utf-8", newline="") as file:
        for row in csv.DictReader(file):
            end = datetime.fromisoformat(row["interval_end"])
            intervals.append(
                Interval(
                    end=end,
                    duration=duration,
                    register=row["register"],
                    unit=unit,
                    import_qty=Decimal(row["import_kwh"]),
                    export_qty=Decimal(row["export_kwh"]),
                )
            )
    if not intervals:
        raise ValueError(f"Synthetic profile is empty: {name}")
    return _Profile(
        name=name,
        commodity=commodity,
        unit=unit,
        start=intervals[0].end - duration,
        end=intervals[-1].end,
        intervals=tuple(intervals),
    )


def _profiles(plan: PlanVersion) -> tuple[_Profile, ...]:
    if plan.commodity == Commodity.GAS:
        return (_load_profile("gas_household"),)
    if plan.commodity != Commodity.ELECTRICITY:
        return ()
    profiles = [
        _load_profile(name)
        for name in ("no_solar", "solar_only", "solar_battery", "ev_heavy")
    ]
    registers = {
        component.register.value
        for component in plan.components
        if isinstance(component, UsageComponent)
    }
    if "controlled_load_1" in registers:
        profiles.append(_load_profile("controlled_load"))
    return tuple(profiles)


def _billing_period(profile: _Profile) -> BillingPeriod:
    contract = None
    if profile.commodity == Commodity.GAS:
        contract = Contract(
            commodity=Commodity.GAS,
            meters={"general": "m3"},
            conversions=(
                Conversion(
                    from_=date(profile.start.year, 1, 1),
                    register="general",
                    meter_unit="m3",
                    billed_unit="MJ",
                    heating_value=GAS_HEATING_VALUE,
                    correction_factor=GAS_CORRECTION_FACTOR,
                ),
            ),
        )
    return BillingPeriod(profile.start, profile.end, contract)


def _bill(plan: PlanVersion, profile: _Profile) -> Any:
    return bill(plan, profile.intervals, period=_billing_period(profile))


def _usage_rates(plan: PlanVersion) -> list[Decimal]:
    rates: list[Decimal] = []
    for component in plan.components:
        if not isinstance(component, UsageComponent):
            continue
        if isinstance(component.rate, Decimal):
            rates.append(component.rate)
        if component.blocks is not None:
            rates.extend(tier.rate for tier in component.blocks.tiers)
    return rates


def check_version(
    plan: PlanVersion, previous_plan: PlanVersion | None = None
) -> list[CheckFinding]:
    """Bill applicable fixed profiles and return review findings for this version."""
    findings = [
        CheckFinding(
            "usage_rate_out_of_range",
            "review",
            f"Usage rate {rate} is outside the inclusive range -1.00 to 1.00 per unit.",
        )
        for rate in _usage_rates(plan)
        if rate > Decimal("1.00") or rate < Decimal("-1.00")
    ]
    if plan.kind.value == "retail" and not any(
        component.kind == "fixed" for component in plan.components
    ):
        findings.append(
            CheckFinding(
                "missing_fixed_component",
                "review",
                "Retail plan has no fixed component.",
            )
        )

    previous_profiles = {
        profile.name: profile for profile in _profiles(previous_plan)
    } if previous_plan is not None and previous_plan.commodity == plan.commodity else {}
    for profile in _profiles(plan):
        result = _bill(plan, profile)
        for warning in result.warnings:
            findings.append(
                CheckFinding(
                    "bill_warning",
                    "review",
                    warning,
                    profile.name,
                )
            )
        previous_profile = previous_profiles.get(profile.name)
        if previous_plan is None or previous_profile is None:
            continue
        previous_result = _bill(previous_plan, previous_profile)
        baseline = abs(previous_result.total)
        difference = abs(result.total - previous_result.total)
        if (baseline == 0 and difference > 0) or (
            baseline > 0 and difference / baseline > Decimal("0.30")
        ):
            findings.append(
                CheckFinding(
                    "annual_bill_change",
                    "review",
                    f"Annual bill changed from {previous_result.total} to {result.total} "
                    f"({profile.name}).",
                    profile.name,
                )
            )
    return findings


def lower_confidence(plan: PlanVersion, findings: list[CheckFinding]) -> PlanVersion:
    """Set confidence for a flagged version without changing its pricing hash."""
    from dataclasses import replace

    return replace(plan, confidence=Confidence.MEDIUM) if findings else plan
