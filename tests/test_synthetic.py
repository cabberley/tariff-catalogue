from tariff_core import parse_plan

from tariff_catalogue.checks.synthetic import check_version


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
