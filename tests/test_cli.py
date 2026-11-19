from typer.testing import CliRunner

from coldchain import __version__
from coldchain.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"coldchain {__version__}" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output


def test_unknown_command_fails() -> None:
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code != 0
