from tariff_catalogue.review.issues import (
    build_failure_issue_body,
    build_findings_issue_body,
    check_report,
    report_outputs,
)


def test_failure_issue_body_includes_run_and_report_summary() -> None:
    body = build_failure_issue_body(
        {
            "requests": 12,
            "failures": 2,
            "detail_failures": 1,
            "new_versions": 3,
            "errors": ["detail request timed out"],
        },
        "https://github.com/example/repo/actions/runs/42",
    )

    assert "[View workflow run](https://github.com/example/repo/actions/runs/42)" in body
    assert "| Requests | 12 |" in body
    assert "| Detail fetch failures | 1 |" in body
    assert "- detail request timed out" in body


def test_findings_issue_body_links_to_version_file() -> None:
    body = build_findings_issue_body(
        {
            "findings": {
                "au:cdr:plan-1": [
                    {
                        "code": "annual_bill_change",
                        "severity": "review",
                        "message": "Bill changed significantly.",
                        "profile": "no_solar",
                    }
                ]
            },
            "finding_versions": {"au:cdr:plan-1": "v1/plans/au%3Acdr%3Aplan-1/abc123.json"},
        },
        "https://github.com/example/repo/actions/runs/42",
        "https://catalogue.example/",
    )

    assert (
        "[`au:cdr:plan-1`](https://catalogue.example/v1/plans/"
        "au%3Acdr%3Aplan-1/abc123.json)" in body
    )
    assert "**annual_bill_change** [review] (no_solar)" in body


def test_detail_failure_threshold_is_strictly_greater_than_five_percent() -> None:
    five_percent = {
        "failures": 1,
        "detail_failures": 1,
        "brands": {"origin": {"details_fetched": 19}},
    }
    above_five_percent = {
        "failures": 1,
        "detail_failures": 1,
        "brands": {"origin": {"details_fetched": 18}},
    }

    assert check_report(five_percent) == []
    assert "maximum allowed is 5%" in check_report(above_five_percent)[0]
    assert report_outputs(five_percent)["fully_successful"] == "false"
