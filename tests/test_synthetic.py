from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from importlib.resources import files

import pytest
from tariff_core import parse_plan

from tariff_catalogue.checks.synthetic import _load_profile, _profiles, check_version


def _plan(rate: str, *, fixed: bool = True, commodity: str = "electricity"):
    components = []
    if fixed:
        components.append({"kind": "fixed", "label": "supply", "unit": "per_day", "rate": "1"})
    components.append(
        {
            "kind": "usage",
            "direction": "import",
            "register": "general",
            "quantity_unit": "MJ" if commodity == "gas" else "kWh",
            "rate": rate,
        }
    )
    if commodity == "electricity":
        components.append(
            {
                "kind": "usage",
                "direction": "export",
                "register": "general",
                "quantity_unit": "kWh",
                "rate": "-0.05",
            }
        )
    return parse_plan(
        {
            "kind": "retail",
            "commodity": commodity,
            "timezone": "Australia/Brisbane",
            "components": components,
        }
    )


def test_high_usage_rate_produces_a_finding() -> None:
    findings = check_version(_plan("4.00", fixed=False))

    assert {finding.code for finding in findings} >= {
        "usage_rate_out_of_range",
        "missing_fixed_component",
    }


def test_fifty_percent_price_rise_produces_annual_bill_finding() -> None:
    findings = check_version(_plan("0.30"), _plan("0.20"))

    assert any(
        finding.code == "annual_bill_change" and finding.profile == "no_solar"
        for finding in findings
    )


def test_normal_electricity_and_converted_gas_plans_have_no_findings() -> None:
    assert check_version(_plan("0.20")) == []
    assert check_version(_plan("0.04", commodity="gas")) == []


def test_profiles_are_package_resources() -> None:
    for name in (
        "no_solar",
        "solar_only",
        "solar_battery",
        "ev_heavy",
        "controlled_load",
        "gas_household",
    ):
        assert files("tariff_catalogue.checks").joinpath("profiles", f"{name}.csv").is_file()
        assert _load_profile(name).intervals


def test_gas_profile_is_winter_weighted() -> None:
    intervals = _load_profile("gas_household").intervals
    winter = sum(
        interval.import_qty
        for interval in intervals
        if (interval.end - timedelta(days=1)).month in (6, 7, 8)
    )
    summer = sum(
        interval.import_qty
        for interval in intervals
        if (interval.end - timedelta(days=1)).month in (12, 1, 2)
    )
    assert winter > summer


@pytest.mark.parametrize(
    "registers",
    [
        ("controlled_load_1",),
        ("controlled_load_2",),
        ("controlled_load_1", "controlled_load_2"),
    ],
)
def test_each_controlled_load_register_is_billed_and_compared(registers) -> None:
    plan = _plan("0.20")
    controlled_components = tuple(
        parse_plan(
            {
                "kind": "retail",
                "commodity": "electricity",
                "timezone": "Australia/Brisbane",
                "components": [
                    {
                        "kind": "usage",
                        "direction": "import",
                        "register": register,
                        "quantity_unit": "kWh",
                        "rate": "0.10",
                    }
                ],
            }
        ).components[0]
        for register in registers
    )
    previous = replace(plan, components=plan.components + controlled_components)
    current = replace(
        plan,
        components=plan.components
        + tuple(replace(component, rate=Decimal("0.50")) for component in controlled_components),
    )
    profiles = _profiles(current)
    assert {profile.name for profile in profiles[4:]} == set(registers)
    for profile in profiles[4:]:
        assert {interval.register for interval in profile.intervals} == {"general", profile.name}
    findings = check_version(current, previous)
    assert {
        finding.profile for finding in findings if finding.code == "annual_bill_change"
    } == set(registers)
