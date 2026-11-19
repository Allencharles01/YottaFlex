from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from coldchain import __version__
from coldchain.cli import app
from coldchain.enums import TableName
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


MASTER_TABLES = [
    t
    for t in raw_tables()
    if t
    in {
        TableName.PARTIES,
        TableName.THRESHOLD_POLICIES,
        TableName.PRODUCTS,
        TableName.PACKAGING,
        TableName.LOTS,
        TableName.DEVICES,
        TableName.SHIPMENTS,
        TableName.SHIPMENT_LOTS,
        TableName.ROUTE_STOPS,
        TableName.LOGGERS,
    }
]


def test_generate_writes_parquet_and_csv_for_every_master_table(generated: Path) -> None:
    names = {p.name for p in generated.iterdir()}
    for table in MASTER_TABLES:
        assert f"{table.value}.csv" in names
        assert f"{table.value}.parquet" in names
    assert len(names) == 2 * len(MASTER_TABLES)


def test_generate_writes_fifty_shipments_with_one_logger_each(generated: Path) -> None:
    ships = pd.read_csv(generated / "shipments.csv")
    loggers = pd.read_csv(generated / "loggers.csv")
    assert len(ships) == 50
    assert sorted(loggers["logger_id"]) == sorted("LOG-" + ships["shipment_id"] + "-01")


def test_generate_is_byte_identical_on_rerun(tmp_path: Path, generated: Path) -> None:
    assert _generate(tmp_path).exit_code == 0
    for path in generated.iterdir():
        assert (tmp_path / "starter" / "raw" / path.name).read_bytes() == path.read_bytes(), path


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
