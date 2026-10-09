import pytest

from tariff_catalogue.cli import main


@pytest.mark.parametrize(
    "command",
    [
        ["harvest", "au-cdr"],
        ["publish"],
        ["check"],
        ["community", "intake"],
    ],
)
def test_commands_are_stubbed(command: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert main(command) == 0
    assert capsys.readouterr().out.strip() == "not implemented"
