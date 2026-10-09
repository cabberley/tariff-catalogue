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
