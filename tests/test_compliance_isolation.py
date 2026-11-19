"""The compliance engine never reads the ground truth (PROJECT_PLAN.md §7, DECISIONS.md #54).

Three independent proofs:

1. *static*: no engine module (nor the ``check`` command) names a ground-truth table or layer,
   or imports the generator (which knows every shipment's scenario);
2. *declared inputs*: every table in ``ENGINE_INPUTS`` belongs to the ``raw`` layer;
3. *dynamic*: on injected data, ``coldchain check`` runs with ``ground_truth/`` deleted, while
   every file open, directory listing and table read is recorded; none touches
   ``ground_truth`` or the ``baseline`` snapshot (which holds a copy of the natural answer key).
"""

import ast
import inspect
import shutil
import sys
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from coldchain import cli
from coldchain.compliance import ENGINE_INPUTS
from coldchain.config import PROJECT_ROOT
from coldchain.enums import Layer, TableName
from coldchain.io import BASELINE_DIR
from coldchain.schemas import TABLE_LAYERS

ENGINE_DIR = PROJECT_ROOT / "src" / "coldchain" / "compliance"

GROUND_TRUTH_NAMES = (
    Layer.GROUND_TRUTH.value,
    Layer.GROUND_TRUTH.name,
    *(name.value for name in TableName if TABLE_LAYERS[name] is Layer.GROUND_TRUTH),
    *(name.name for name in TableName if TABLE_LAYERS[name] is Layer.GROUND_TRUTH),
)
"""Every spelling of the ground-truth layer and its tables (values and enum member names)."""

FORBIDDEN_IMPORTS = ("coldchain.generate", "coldchain.inject")


def _engine_sources() -> list[Path]:
    sources = sorted(ENGINE_DIR.glob("*.py"))
    assert sources, ENGINE_DIR
    return sources


@pytest.mark.parametrize("path", _engine_sources(), ids=lambda p: p.name)
def test_engine_modules_never_name_the_baseline_snapshot(path: Path) -> None:
    assert BASELINE_DIR not in path.read_text(encoding="utf-8"), path.name


@pytest.mark.parametrize("path", _engine_sources(), ids=lambda p: p.name)
def test_engine_modules_never_name_the_ground_truth(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name in GROUND_TRUTH_NAMES:
        assert name not in text, f"{path.name} mentions {name!r}"


@pytest.mark.parametrize("path", _engine_sources(), ids=lambda p: p.name)
def test_engine_modules_never_import_the_generator(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = [
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    ] + [
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    ]
    for module in imported:
        assert not module.startswith(FORBIDDEN_IMPORTS), f"{path.name} imports {module}"


def test_check_command_never_names_the_ground_truth() -> None:
    source = inspect.getsource(cli.check)
    for name in GROUND_TRUTH_NAMES:
        assert name not in source, name


def test_engine_inputs_are_raw_tables_only() -> None:
    assert ENGINE_INPUTS
    assert {TABLE_LAYERS[name] for name in ENGINE_INPUTS} == {Layer.RAW}


# --------------------------------------------------------------------------------------------
# Dynamic proof: record every file the check command touches.
# --------------------------------------------------------------------------------------------

_RECORDED: list[str] | None = None
_AUDITED_EVENTS = {"open", "os.listdir", "os.scandir", "os.walk", "glob.glob", "os.mkdir"}


def _audit(event: str, args: tuple) -> None:
    if _RECORDED is not None and event in _AUDITED_EVENTS and args:
        _RECORDED.append(str(args[0]))


sys.addaudithook(_audit)  # audit hooks cannot be removed; it records only while _RECORDED is set


@pytest.mark.slow
def test_check_runs_without_ground_truth_and_never_touches_it(tmp_path, monkeypatch) -> None:
    global _RECORDED
    runner = CliRunner()
    for command in ("generate", "inject"):  # injected data, with a baseline snapshot on disk
        done = runner.invoke(
            cli.app, [command, "--scale", "starter", "--output-dir", str(tmp_path)]
        )
        assert done.exit_code == 0, done.output
    ground_truth = tmp_path / "starter" / Layer.GROUND_TRUTH.value
    assert ground_truth.is_dir()
    shutil.rmtree(ground_truth)

    tables_read: list[str] = []
    real_parquet, real_csv = pd.read_parquet, pd.read_csv

    def spy_parquet(path, *args, **kwargs):
        tables_read.append(str(path))
        return real_parquet(path, *args, **kwargs)

    def spy_csv(path, *args, **kwargs):
        tables_read.append(str(path))
        return real_csv(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", spy_parquet)
    monkeypatch.setattr(pd, "read_csv", spy_csv)
    _RECORDED = []
    try:
        result = runner.invoke(
            cli.app, ["check", "--scale", "starter", "--output-dir", str(tmp_path)]
        )
        touched = list(_RECORDED)
    finally:
        _RECORDED = None

    assert result.exit_code == 0, result.output
    assert not ground_truth.exists(), "check must not create ground_truth/"
    assert touched, "the audit hook recorded nothing"
    for path in touched + tables_read:
        assert Layer.GROUND_TRUTH.value not in path, path
        assert BASELINE_DIR not in Path(path).parts, path  # nor the pre-injection snapshot
    raw_dir = tmp_path / "starter" / Layer.RAW.value
    assert sorted(tables_read) == sorted(str(raw_dir / f"{t.value}.parquet") for t in ENGINE_INPUTS)
    assert (tmp_path / "starter" / Layer.COMPLIANCE.value / "compliance_decisions.csv").is_file()
