from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from coldchain import __version__
from coldchain.cli import app
from coldchain.io import raw_tables

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"coldchain {__version__}" in result.output


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "version" in result.output
    assert "generate" in result.output


def test_unknown_command_fails() -> None:
    result = runner.invoke(app, ["does-not-exist"])
    assert result.exit_code != 0


def _generate(tmp_path: Path, *extra: str):
    return runner.invoke(
        app, ["generate", "--scale", "starter", "--output-dir", str(tmp_path), *extra]
    )


@pytest.fixture(scope="module")
def generated(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("out")
    result = _generate(root)
    assert result.exit_code == 0, result.output
    return root / "starter" / "raw"


def test_generate_writes_parquet_and_csv_for_every_raw_table(generated: Path) -> None:
    names = {p.name for p in generated.iterdir()}
    for table in raw_tables():
        assert f"{table.value}.csv" in names
        assert f"{table.value}.parquet" in names
    assert len(names) == 2 * len(raw_tables())


def test_generate_writes_ground_truth_tables_to_ground_truth_only(generated: Path) -> None:
    ground_truth = generated.parent / "ground_truth"
    assert sorted(p.name for p in ground_truth.iterdir()) == [
        "device_truth.csv",
        "device_truth.parquet",
        "expected_outcomes.csv",
        "expected_outcomes.parquet",
    ]
    outcomes = pd.read_csv(ground_truth / "expected_outcomes.csv")
    assert len(outcomes) == 50
    assert len(pd.read_csv(ground_truth / "device_truth.csv")) == 20
    assert not any("expected" in p.name or "truth" in p.name for p in generated.iterdir())


def test_generate_writes_fifty_shipments_with_one_logger_each(generated: Path) -> None:
    ships = pd.read_csv(generated / "shipments.csv")
    loggers = pd.read_csv(generated / "loggers.csv")
    assert len(ships) == 50
    assert sorted(loggers["logger_id"]) == sorted("LOG-" + ships["shipment_id"] + "-01")


def test_generate_is_byte_identical_on_rerun(tmp_path: Path, generated: Path) -> None:
    assert _generate(tmp_path).exit_code == 0
    for layer in ("raw", "ground_truth"):
        for path in (generated.parent / layer).iterdir():
            again = tmp_path / "starter" / layer / path.name
            assert again.read_bytes() == path.read_bytes(), path


def test_seed_option_changes_output(tmp_path: Path, generated: Path) -> None:
    assert _generate(tmp_path, "--seed", "7").exit_code == 0
    other = (tmp_path / "starter" / "raw" / "shipments.csv").read_bytes()
    assert other != (generated / "shipments.csv").read_bytes()


def test_config_option_uses_the_given_file(tmp_path: Path, raw_config: dict) -> None:
    raw_config["scales"]["starter"]["shipments"] = 12
    path = tmp_path / "custom.yaml"
    path.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    result = _generate(tmp_path, "--config", str(path))
    assert result.exit_code == 0, result.output
    assert len(pd.read_csv(tmp_path / "starter" / "raw" / "shipments.csv")) == 12


def test_missing_config_exits_non_zero(tmp_path: Path) -> None:
    result = _generate(tmp_path, "--config", str(tmp_path / "missing.yaml"))
    assert result.exit_code == 1
    assert "Config file not found" in result.output


def test_invalid_scale_and_negative_seed_rejected(tmp_path: Path) -> None:
    assert runner.invoke(app, ["generate", "--scale", "huge"]).exit_code != 0
    assert _generate(tmp_path, "--seed", "-1").exit_code != 0


def test_no_args_help_lists_check() -> None:
    assert "check" in runner.invoke(app, []).output


def test_check_without_raw_data_exits_with_status_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", "--scale", "starter", "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Table file not found" in result.output
    assert not (tmp_path / "starter" / "compliance").exists()
