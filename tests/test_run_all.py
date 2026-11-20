"""``coldchain run-all``: the whole pipeline in one command, stopping at the first failure."""

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from coldchain import pipeline
from coldchain.cli import app
from coldchain.enums import PipelineStage

runner = CliRunner()

SCALE = "starter"


def _run_all(root: Path, *extra: str):
    return runner.invoke(
        app,
        ["--log-level", "WARNING", "run-all", "--scale", SCALE, "--output-dir", str(root), *extra],
    )


def _summary(root: Path) -> dict:
    return json.loads((root / SCALE / "run_summary.json").read_text(encoding="utf-8"))


def test_help_lists_run_all_and_reproduce() -> None:
    output = runner.invoke(app, []).output
    assert "run-all" in output and "reproduce" in output


def test_run_all_stops_at_the_first_failing_stage(tmp_path: Path, raw_config: dict) -> None:
    """A real failure: injection rates that need more targets than starter scale has."""
    raw_config["injection_rates"]["logger_not_assigned"] = 0.9
    config = tmp_path / "too_many_targets.yaml"
    config.write_text(yaml.safe_dump(raw_config), encoding="utf-8")
    result = _run_all(tmp_path, "--config", str(config))
    assert result.exit_code == 1
    assert "run-all stopped at stage inject:" in result.output
    summary = _summary(tmp_path)
    assert (summary["status"], summary["exit_code"], summary["failed_stage"]) == (
        "FAIL",
        1,
        "inject",
    )
    statuses = [(s["stage"], s["status"]) for s in summary["stages"]]
    assert statuses == [
        ("generate", "PASS"),
        ("inject", "FAIL"),
        ("check", "NOT_RUN"),
        ("validate", "NOT_RUN"),
        ("validate_baseline", "NOT_RUN"),
        ("evaluate", "NOT_RUN"),
        ("export", "NOT_RUN"),
    ]
    inject = summary["stages"][1]
    assert inject["error"] == summary["error"] and inject["error"]
    assert inject["timing"]["duration_s"] > 0
    assert summary["stages"][2]["timing"] is None
    assert not (tmp_path / SCALE / "compliance").exists()
    assert summary["stages"][2]["command"].startswith("coldchain check --scale starter --config ")


def test_a_fail_verdict_stops_the_run_too(tmp_path: Path, monkeypatch) -> None:
    def failing_check(options, *, command):
        return pipeline.StageResult(details={"verdict": "FAIL"}, failure="engine said no")

    monkeypatch.setattr(pipeline, "run_check", failing_check)
    result = _run_all(tmp_path)
    assert result.exit_code == 1
    assert "run-all stopped at stage check: engine said no" in result.output
    summary = _summary(tmp_path)
    check = summary["stages"][2]
    assert (check["status"], check["error"], check["details"]) == (
        "FAIL",
        "engine said no",
        {"verdict": "FAIL"},
    )
    assert [s["status"] for s in summary["stages"][3:]] == ["NOT_RUN"] * 4


@pytest.mark.slow
def test_run_all_runs_every_stage_and_proves_the_sql(tmp_path: Path) -> None:
    result = _run_all(tmp_path, "--seed", "20261022")
    assert result.exit_code == 0, result.output
    summary = _summary(tmp_path)
    assert (summary["status"], summary["exit_code"], summary["seed"]) == ("PASS", 0, 20261022)
    assert [s["stage"] for s in summary["stages"]] == list(pipeline.RUN_ALL_STAGES)
    stages = {s["stage"]: s for s in summary["stages"]}
    assert stages["generate"]["details"]["seed"] == stages["inject"]["details"]["seed"] == 20261022
    assert stages["inject"]["details"]["anomalies"] == 5
    assert stages["check"]["rows"]["compliance/compliance_decisions"] == 50
    assert stages["validate"]["details"]["integrity_findings"] == 0
    assert stages["evaluate"]["details"]["fully_correct_shipments"] == 50
    assert stages["export"]["details"]["sql_verification"] == {"duckdb": "PASS", "sqlite": "PASS"}
    assert all(s["timing"]["peak_rss_mib"] > 0 for s in summary["stages"])
    manifest = json.loads((tmp_path / SCALE / "export" / "manifest.json").read_text())
    assert manifest["dataset_sha256"] == stages["export"]["details"]["dataset_sha256"]
    assert [p["stage"] for p in manifest["pipeline"]] == [s.value for s in pipeline.CHAIN_STAGES]
    record = json.loads((tmp_path / SCALE / "provenance" / "generate.json").read_text())
    assert "--seed 20261022" in record["command"]
    assert {s.value for s in PipelineStage} == {
        p.stem for p in (tmp_path / SCALE / "provenance").iterdir()
    }
