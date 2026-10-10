import json
from pathlib import Path

import pytest

from tariff_catalogue.cli import build_parser, main


@pytest.mark.parametrize(
    "command",
    [
        ["check"],
        ["community", "intake"],
    ],
)
def test_commands_are_stubbed(command: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(command) == 0
    assert capsys.readouterr().out.strip() == "not implemented"


def test_au_cdr_cli_accepts_brand_full_and_dry_run_flags() -> None:
    args = build_parser().parse_args(
        ["harvest", "au-cdr", "--brand", "origin", "--full", "--dry-run"]
    )

    assert args.brand == "origin"
    assert args.full
    assert args.dry_run


def test_au_cdr_cli_writes_json_report_and_step_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tariff_catalogue import cli

    report_path = tmp_path / "report.json"
    summary_path = tmp_path / "summary.md"

    def record_version(client, _archive, **_kwargs) -> None:
        client._report.record_version("new")

    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    monkeypatch.setattr(cli, "run_au_cdr", record_version)

    assert main(["harvest", "au-cdr", "--dry-run", "--report", str(report_path)]) == 0

    report = json.loads(report_path.read_text())
    assert report["new_versions"] == 1
    assert report["detail_failures"] == 0
    assert "## Harvest run summary" in summary_path.read_text()


def test_publish_cli_builds_locally_in_dry_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "dist"

    assert (
        main(
            [
                "publish",
                "--dry-run",
                "--out",
                str(output),
                "--archive-root",
                str(tmp_path / "archive"),
            ]
        )
        == 0
    )

    assert json.loads((output / "v1" / "index.json").read_text())["schema_version"] == "v1"
    assert "Built 0 plans" in capsys.readouterr().out
