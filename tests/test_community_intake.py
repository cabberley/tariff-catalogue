import json
from pathlib import Path

import httpx
import respx
import yaml
from tariff_core import parse_plan, to_dict, version_id

from tariff_catalogue.community_tools import intake
from tariff_catalogue.community_tools.intake import (
    Submission,
    _issue_comment,
    _pricing_hash,
    build_pr_body,
    find_published_duplicate,
    parse_issue_body,
    scan_personal_data,
    validate_submission,
)

ISSUE_URL = "https://github.com/cabberley/tariff-catalogue/issues/10"


def _submission(plan: dict | None = None, **overrides) -> Submission:
    plan = plan or {
        "kind": "retail",
        "commodity": "electricity",
        "currency": "AUD",
        "timezone": "Australia/Brisbane",
        "pricing_model": "bundled",
        "effective": {"from": "2026-01-01", "to": None},
        "components": [
            {"kind": "fixed", "label": "supply", "unit": "per_day", "rate": "1"},
            {
                "kind": "usage",
                "direction": "import",
                "register": "general",
                "quantity_unit": "kWh",
                "rate": "0.20",
            },
        ],
    }
    values = {
        "country": "au",
        "supplier": "Example Energy",
        "plan_name": "Example Variable",
        "commodity": "electricity",
        "source_link": "https://example.com/plan",
        "from_my_bill": False,
        "plan_yaml": yaml.safe_dump(plan, sort_keys=False),
    }
    values.update(overrides)
    return Submission(**values)


def _issue_body(plan_yaml: str) -> str:
    return f"""### Country
au

### Supplier
Example Energy

### Plan name
Example Variable

### Commodity
electricity

### Source link
https://example.com/plan

### Source
- [ ] This plan is from my bill (no public source link)

### Plan YAML
```yaml
{plan_yaml.rstrip()}
```
"""


def test_valid_submission_builds_a_review_pr_body(tmp_path: Path) -> None:
    result = validate_submission(_submission(), tmp_path)

    assert result.errors == []
    assert result.file_path == Path("community/au/example-energy/example-variable.yaml")
    assert result.plan is not None
    assert result.plan.plan_id == "au:community:example-variable"

    body = build_pr_body(ISSUE_URL, result.findings)
    assert "Closes #10" in body
    assert ISSUE_URL in body
    assert "human review" in body
    assert "Synthetic bill checks" in body


def test_from_my_bill_option_is_parsed() -> None:
    body = _issue_body(_submission().plan_yaml).replace("- [ ] This plan", "- [x] This plan")

    assert parse_issue_body(body).from_my_bill


def test_issue_form_markdown_fields_are_parsed() -> None:
    submission = parse_issue_body(_issue_body(_submission().plan_yaml))

    assert submission.country == "au"
    assert submission.plan_name == "Example Variable"
    assert not submission.from_my_bill
    assert "kind" in submission.plan_yaml


def test_malformed_yaml_and_missing_source_are_reported_without_echoing_input(
    tmp_path: Path,
) -> None:
    malformed = validate_submission(_submission(plan_yaml="plan: [invalid"), tmp_path)
    missing_source = validate_submission(
        _submission(source_link="", from_my_bill=False),
        tmp_path,
    )

    assert "Plan YAML could not be parsed." in malformed.errors
    assert "Provide a public source link or select the bill source option." in missing_source.errors


def test_nmi_is_rejected_without_echoing_it_in_the_comment(tmp_path: Path) -> None:
    result = validate_submission(
        _submission(
            {
                **yaml.safe_load(_submission().plan_yaml),
                "notes": ["NMI: 4102000000"],
            }
        ),
        tmp_path,
    )

    comment = _issue_comment(result.errors)
    assert any("personal information" in error for error in result.errors)
    assert "4102000000" not in comment


def test_issue_rejection_comment_does_not_echo_nmi(tmp_path: Path, monkeypatch) -> None:
    plan = yaml.safe_load(_submission().plan_yaml)
    plan["notes"] = ["NMI: 4102000000"]
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "issue": {
                    "number": 10,
                    "html_url": ISSUE_URL,
                    "title": "[Plan submission]: Example Variable",
                    "body": _issue_body(yaml.safe_dump(plan)),
                    "labels": [],
                },
                "action": "opened",
                "repository": {"full_name": "cabberley/tariff-catalogue"},
            }
        ),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(intake, "_run", lambda args, root: calls.append(args) or "")

    assert intake._process_event(tmp_path, event_path, "main") == 0

    comment = calls[0][-1]
    assert "personal information" in comment
    assert "4102000000" not in comment


def test_retry_reuses_an_existing_submission_branch(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CATALOGUE_BASE_URL", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    body = _issue_body(_submission().plan_yaml)
    result = validate_submission(parse_issue_body(body), tmp_path)
    assert result.plan is not None
    destination = tmp_path / result.file_path
    plan_content = yaml.safe_dump(to_dict(result.plan), sort_keys=False, allow_unicode=True)
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "issue": {
                    "number": 10,
                    "html_url": ISSUE_URL,
                    "title": "[Plan submission]: Example Variable",
                    "body": body,
                    "labels": [{"name": "plan-submission"}],
                },
                "action": "labeled",
                "repository": {"full_name": "cabberley/tariff-catalogue"},
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def run(arguments, root):
        calls.append(arguments)
        if arguments[:3] == ["gh", "pr", "list"]:
            return ""
        if arguments[:2] == ["git", "ls-remote"]:
            return "commit refs/heads/community/issue-10"
        if arguments[:4] == ["git", "checkout", "-b", "community/issue-10"]:
            destination.parent.mkdir(parents=True)
            destination.write_text(plan_content, encoding="utf-8")
        if arguments[:3] == ["git", "diff", "--name-only"]:
            return result.file_path.as_posix()
        if arguments[:3] == ["gh", "pr", "create"]:
            return "https://github.com/cabberley/tariff-catalogue/pull/11"
        return ""

    monkeypatch.setattr(intake, "_run", run)

    assert intake._process_event(tmp_path, event_path, "main") == 0

    assert ["git", "fetch", "origin", "community/issue-10"] in calls
    assert ["git", "push", "--set-upstream", "origin", "community/issue-10"] not in calls
    assert ["gh", "pr", "create"] == calls[-2][:3]


def test_personal_data_scanner_recognizes_supported_identifier_patterns() -> None:
    examples = (
        "NMI: QL123456789",
        "MPAN: 20 0000 0000 001",
        "MPRN: 1234567890",
        "12345678",
        "customer@example.com",
        "+61 412 345 678",
        "123 Main Street",
    )

    for example in examples:
        assert scan_personal_data(example)


def test_plan_id_source_confidence_and_schema_failures_are_blocking(tmp_path: Path) -> None:
    cases = [
        (
            {"plan_id": "gb:community:other-plan"},
            "Plan ID must match",
        ),
        (
            {"source": {"type": "official_feed"}},
            "Plan source.type must be community",
        ),
        ({"confidence": "high"}, "Plan confidence must be unverified"),
        ({"pricing_model": "pass_through"}, "Tariff validation failed"),
    ]

    for changes, message in cases:
        plan = yaml.safe_load(_submission().plan_yaml)
        plan.update(changes)
        result = validate_submission(_submission(plan), tmp_path)
        assert any(message in error for error in result.errors)


def test_synthetic_findings_are_reported_but_do_not_block_submission(tmp_path: Path) -> None:
    plan = yaml.safe_load(_submission().plan_yaml)
    plan["components"][1]["rate"] = "4.00"

    result = validate_submission(_submission(plan), tmp_path)

    assert result.errors == []
    assert any(finding.code == "usage_rate_out_of_range" for finding in result.findings)
    assert "usage_rate_out_of_range" in build_pr_body(ISSUE_URL, result.findings)


def test_duplicate_pricing_is_reported_instead_of_becoming_a_pr(tmp_path: Path) -> None:
    result = validate_submission(_submission(), tmp_path)
    assert result.plan is not None

    official_plan = to_dict(result.plan)
    official_plan["plan_id"] = "au:cdr:official-plan"
    official_plan["source"] = {"type": "official_feed"}
    existing_path = tmp_path / "network" / "au" / "official.yaml"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text(yaml.safe_dump(official_plan), encoding="utf-8")

    duplicate = validate_submission(_submission(), tmp_path)

    assert duplicate.duplicate_path == Path("network/au/official.yaml")
    comment = _issue_comment([], duplicate_url="https://example.com/matching-plan")
    assert "same pricing" in comment
    assert "https://example.com/matching-plan" in comment


def test_same_numeric_rates_in_another_currency_are_not_duplicates(tmp_path: Path) -> None:
    result = validate_submission(_submission(), tmp_path)
    assert result.plan is not None
    other_market = to_dict(result.plan)
    other_market["plan_id"] = "au:community:other-plan"
    other_market["display_name"] = "Other Plan"
    other_market["currency"] = "GBP"
    existing = parse_plan(other_market)
    other_market["id"] = version_id(existing)
    existing_path = tmp_path / "community" / "au" / "example-energy" / "other-plan.yaml"
    existing_path.parent.mkdir(parents=True)
    existing_path.write_text(yaml.safe_dump(other_market), encoding="utf-8")

    duplicate = validate_submission(_submission(), tmp_path)

    assert duplicate.duplicate_path is None
    assert duplicate.id_conflict_path is None


def test_plan_id_collision_with_different_pricing_is_rejected(tmp_path: Path) -> None:
    result = validate_submission(_submission(), tmp_path)
    assert result.plan is not None
    conflicting = to_dict(result.plan)
    conflicting["supplier"] = {"id": "other-energy", "name": "Other Energy"}
    conflicting["components"][1]["rate"] = "0.30"
    parsed_conflict = parse_plan(conflicting)
    conflicting["id"] = version_id(parsed_conflict)
    conflict_path = tmp_path / "community" / "au" / "other-energy" / "example-variable.yaml"
    conflict_path.parent.mkdir(parents=True)
    conflict_path.write_text(yaml.safe_dump(conflicting), encoding="utf-8")

    submission = validate_submission(_submission(), tmp_path)

    assert submission.duplicate_path is None
    assert submission.id_conflict_path == Path("community/au/other-energy/example-variable.yaml")


def test_published_official_duplicate_uses_the_equivalence_group(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CATALOGUE_BASE_URL", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    plan = validate_submission(_submission(), Path.cwd()).plan
    assert plan is not None
    monkeypatch.setenv("CATALOGUE_BASE_URL", "https://catalogue.example")
    group = _pricing_hash(plan)

    with respx.mock(base_url="https://catalogue.example") as router:
        router.get("/v1/au/index.json").mock(
            return_value=httpx.Response(200, json={"regions": [{"region": "global"}]})
        )
        router.get("/v1/au/global/index.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "plans": [
                        {
                            "plan_id": "au:cdr:official-plan",
                            "equivalence_group": group,
                            "commodity": "electricity",
                            "currency": "AUD",
                        }
                    ]
                },
            )
        )

        duplicate = find_published_duplicate(plan)

    assert duplicate == "https://catalogue.example/v1/plans/au%3Acdr%3Aofficial-plan/index.json"


def test_published_duplicate_checks_currency_in_plan_details_for_old_indexes(
    monkeypatch,
) -> None:
    monkeypatch.delenv("CATALOGUE_BASE_URL", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    plan = validate_submission(_submission(), Path.cwd()).plan
    assert plan is not None
    monkeypatch.setenv("CATALOGUE_BASE_URL", "https://catalogue.example")
    group = _pricing_hash(plan)

    with respx.mock(base_url="https://catalogue.example") as router:
        router.get("/v1/au/index.json").mock(
            return_value=httpx.Response(200, json={"regions": [{"region": "global"}]})
        )
        router.get("/v1/au/global/index.json").mock(
            return_value=httpx.Response(
                200,
                json={
                    "plans": [
                        {
                            "plan_id": "au:cdr:official-plan",
                            "latest_version": "version-hash",
                            "equivalence_group": group,
                            "commodity": "electricity",
                        }
                    ]
                },
            )
        )
        router.get("/v1/plans/au%3Acdr%3Aofficial-plan/version-hash.json").mock(
            return_value=httpx.Response(200, json={"currency": "AUD"})
        )

        duplicate = find_published_duplicate(plan)

    assert duplicate == "https://catalogue.example/v1/plans/au%3Acdr%3Aofficial-plan/index.json"


def test_community_pr_validation_checks_id_and_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CATALOGUE_BASE_URL", raising=False)
    monkeypatch.delenv("R2_BUCKET", raising=False)
    result = validate_submission(_submission(), tmp_path)
    assert result.plan is not None

    plan_path = tmp_path / result.file_path
    plan_path.parent.mkdir(parents=True)
    plan_path.write_text(yaml.safe_dump(to_dict(result.plan)), encoding="utf-8")
    assert intake._validate_community(tmp_path) == 0

    plan_data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan_data["plan_id"] = "gb:community:wrong-plan"
    plan_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    assert intake._validate_community(tmp_path) == 1

    plan_data = to_dict(result.plan)
    plan_data["supplier"] = {"id": "other-energy", "name": "Other Energy"}
    plan_data["components"][1]["rate"] = "0.30"
    conflicting_plan = parse_plan(plan_data)
    plan_data["id"] = version_id(conflicting_plan)
    plan_path.write_text(yaml.safe_dump(to_dict(result.plan)), encoding="utf-8")
    conflict_path = tmp_path / "community/au/other-energy/example-variable.yaml"
    conflict_path.parent.mkdir(parents=True)
    conflict_path.write_text(yaml.safe_dump(plan_data), encoding="utf-8")
    assert intake._validate_community(tmp_path) == 1
