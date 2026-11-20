"""``coldchain validate`` judges the data on its own: it never reads the ground truth.

Like the engine's isolation test (``test_compliance_isolation.py``), the proof is both static
and dynamic:

1. no validation module (nor the ``validate`` command) names a ground-truth table or layer, or
   imports the engine, the generator, the injector or the evaluation;
2. on injected data, ``coldchain validate`` (both sources) runs with ``ground_truth/`` deleted
   while every file open and directory listing is recorded; none touches a ground-truth folder.
"""

import ast
import inspect
import shutil
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coldchain import cli
from coldchain.config import PROJECT_ROOT
from coldchain.enums import Layer, TableName
from coldchain.io import BASELINE_DIR
from coldchain.schemas import TABLE_LAYERS

VALIDATION_DIR = PROJECT_ROOT / "src" / "coldchain" / "validation"

GROUND_TRUTH_NAMES = (
    Layer.GROUND_TRUTH.value,
    Layer.GROUND_TRUTH.name,
    *(name.value for name in TableName if TABLE_LAYERS[name] is Layer.GROUND_TRUTH),
    *(name.name for name in TableName if TABLE_LAYERS[name] is Layer.GROUND_TRUTH),
)

FORBIDDEN_IMPORTS = (
    "coldchain.compliance",
    "coldchain.generate",
    "coldchain.inject",
    "coldchain.evaluation",
)


def _sources() -> list[Path]:
    sources = sorted(VALIDATION_DIR.glob("*.py"))
    assert len(sources) >= 4, sources
    return sources


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_validation_modules_never_name_the_ground_truth(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for name in GROUND_TRUTH_NAMES:
        assert name not in text, f"{path.name} mentions {name!r}"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_validation_modules_never_import_the_engine_or_the_answer_key(path: Path) -> None:
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


def test_validate_command_never_names_the_ground_truth() -> None:
    source = inspect.getsource(cli.validate) + inspect.getsource(cli.validation_dir)
    for name in GROUND_TRUTH_NAMES:
        assert name not in source, name


_RECORDED: list[str] | None = None
_AUDITED_EVENTS = {"open", "os.listdir", "os.scandir", "os.walk", "glob.glob"}


def _audit(event: str, args: tuple) -> None:
    if _RECORDED is not None and event in _AUDITED_EVENTS and args:
        _RECORDED.append(str(args[0]))


sys.addaudithook(_audit)  # records only while _RECORDED is set


@pytest.mark.slow
@pytest.mark.parametrize("source", ["raw", "baseline"])
def test_validate_runs_without_ground_truth_and_never_touches_it(tmp_path, source) -> None:
    global _RECORDED
    runner = CliRunner()
    opts = ["--scale", "starter", "--output-dir", str(tmp_path)]
    for command in ("generate", "inject", "check"):
        done = runner.invoke(cli.app, [command, *opts])
        assert done.exit_code == 0, done.output
    root = tmp_path / "starter"
    for truth in (root / Layer.GROUND_TRUTH.value, root / BASELINE_DIR / Layer.GROUND_TRUTH.value):
        assert truth.is_dir()
        shutil.rmtree(truth)
    _RECORDED = []
    try:
        result = runner.invoke(cli.app, ["validate", *opts, "--source", source])
        touched = list(_RECORDED)
    finally:
        _RECORDED = None
    assert result.exit_code == 0, result.output
    assert touched, "the audit hook recorded nothing"
    for path in touched:
        assert Layer.GROUND_TRUTH.value not in Path(path).parts, path
    raw_folder = (
        root / Layer.RAW.value if source == "raw" else root / BASELINE_DIR / Layer.RAW.value
    )
    assert any(Path(p).parent == raw_folder for p in touched)
    assert not (root / Layer.GROUND_TRUTH.value).exists()
