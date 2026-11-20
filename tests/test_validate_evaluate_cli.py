"""``coldchain validate`` and ``coldchain evaluate`` end to end at starter scale (Phase 6 UAT).

Exit codes: validate fails only on INTEGRITY checks; evaluate fails on any unexplained finding
and, at starter, on any metric below 100 %. Reports are byte-identical on a rerun, and evaluate
refuses a validation summary whose input files have changed since.
"""

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from coldchain.cli import app
from coldchain.enums import Layer, TableName
from coldchain.io import layer_dir, read_table, write_table

pytestmark = pytest.mark.slow

runner = CliRunner()


def _invoke(root: Path, *args: str):
    return runner.invoke(app, [*args, "--scale", "starter", "--output-dir", str(root)])


def _ok(root: Path, *args: str) -> None:
    result = _invoke(root, *args)
    assert result.exit_code == 0, result.output


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """generate → inject → check → validate (raw and baseline) → evaluate, once."""
    root = tmp_path_factory.mktemp("phase6")
    for command in ("generate", "inject", "check", "validate"):
        _ok(root, command)
    _ok(root, "validate", "--source", "baseline")
    _ok(root, "evaluate")
    return root


def _copy(root: Path, tmp_path: Path) -> Path:
    target = tmp_path / "copy"
    shutil.copytree(root, target)
    return target


def test_reports_are_written(root) -> None:
    for folder, stem in (
        ("validation", "validation_summary"),
        ("validation/baseline", "validation_summary"),
        ("evaluation", "evaluation_report"),
    ):
        for suffix in (".md", ".json"):
            assert (root / "starter" / folder / f"{stem}{suffix}").is_file(), (folder, suffix)


def test_baseline_has_zero_findings(root) -> None:
    payload = _json(root / "starter" / "validation" / "baseline" / "validation_summary.json")
    assert payload["status"] == "PASS" and payload["source"] == "baseline"
    assert payload["totals"]["findings"] == 0 and payload["totals"]["fail"] == 0
    assert payload["totals"]["error"] == 0
    assert all(path.startswith("starter/baseline/raw/") for path in payload["inputs"])


def test_injected_findings_are_business_rules_and_all_explained(root) -> None:
    validation = _json(root / "starter" / "validation" / "validation_summary.json")
    assert validation["status"] == "PASS" and validation["totals"]["skipped"] == 0
    assert validation["totals"]["integrity_findings"] == 0
    assert validation["totals"]["business_rule_findings"] > 0
    evaluation = _json(root / "starter" / "evaluation" / "evaluation_report.json")
    assert evaluation["verdict"] == "PASS" and evaluation["failures"] == []
    assert evaluation["validation"]["unexplained"] == 0
    assert evaluation["validation"]["explained"] == validation["totals"]["findings"]
    text = (root / "starter" / "evaluation" / "evaluation_report.md").read_text(encoding="utf-8")
    assert "**Unexplained validation findings: 0**" in text


def test_reports_are_byte_identical_on_a_rerun(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    _ok(copy, "validate")
    _ok(copy, "evaluate")
    for relative in (
        "starter/validation/validation_summary.json",
        "starter/validation/validation_summary.md",
        "starter/evaluation/evaluation_report.json",
        "starter/evaluation/evaluation_report.md",
    ):
        assert (copy / relative).read_bytes() == (root / relative).read_bytes(), relative


def _break_natural_custody(root: Path) -> str:
    """Delete a HANDOFF of a shipment that no anomaly targets; return that shipment."""
    raw = layer_dir(root, "starter", Layer.RAW)
    truth = layer_dir(root, "starter", Layer.GROUND_TRUTH)
    targets = set(read_table(TableName.ANOMALIES, truth)["shipment_id"])
    custody = read_table(TableName.CUSTODY_EVENTS, raw)
    handoffs = custody[(custody["event_type"] == "HANDOFF") & ~custody["shipment_id"].isin(targets)]
    victim = handoffs.iloc[0]
    write_table(TableName.CUSTODY_EVENTS, custody.drop(index=victim.name), raw)
    return victim["shipment_id"]


def test_evaluate_refuses_a_stale_validation_summary(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    _break_natural_custody(copy)
    result = _invoke(copy, "evaluate")
    assert result.exit_code == 1
    assert "stale" in result.output


def test_an_unexplained_business_finding_fails_evaluate_only(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    shipment = _break_natural_custody(copy)
    _ok(copy, "check")
    _ok(copy, "validate")  # a business-rule finding does not fail validate
    result = _invoke(copy, "evaluate")
    assert result.exit_code == 1
    assert "unexplained validation finding" in result.output
    report = _json(copy / "starter" / "evaluation" / "evaluation_report.json")
    unexplained = {f["shipment_id"] for f in report["validation"]["unexplained_findings"]}
    assert unexplained == {shipment}
    assert report["verdict"] == "FAIL"


def test_an_integrity_failure_fails_validate(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    raw = layer_dir(copy, "starter", Layer.RAW)
    lots = read_table(TableName.LOTS, raw)
    lots.loc[0, "quantity_units"] = 1
    write_table(TableName.LOTS, lots, raw)
    result = _invoke(copy, "validate")
    assert result.exit_code == 1
    assert "integrity checks failed: recon.lots.quantity" in result.output
    payload = _json(copy / "starter" / "validation" / "validation_summary.json")
    assert payload["status"] == "FAIL"


def test_evaluate_needs_a_validation_summary(tmp_path) -> None:
    for command in ("generate", "check"):
        _ok(tmp_path, command)
    result = _invoke(tmp_path, "evaluate")
    assert result.exit_code == 1
    assert "Report not found" in result.output
