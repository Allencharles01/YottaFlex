"""``coldchain reproduce`` and the pure comparison in ``export/reproducibility.py``.

Pure functions on hand-made file facts, the CLI with a fake pipeline (fast: every failure
branch), and one real starter-scale reproduce (slow): three full runs, every file identical
between run1 and run2, the control seed changes the data, and ``run-all`` writes exactly what
the single-stage commands write.
"""

import json
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from coldchain import pipeline
from coldchain.cli import app
from coldchain.enums import PipelineStage
from coldchain.export import reproducibility as repro
from coldchain.export.manifest import content_sha256
from coldchain.io import describe_files

runner = CliRunner()

SCALE = "starter"


def _fact(sha: str, rows: int | None = 1, size: int = 10) -> dict:
    return {"sha256": sha, "bytes": size, "rows": rows}


# --------------------------------------------------------------------------------------------
# Pure: run_facts, compare_runs, control_comparison, build_report, report_markdown
# --------------------------------------------------------------------------------------------


def _manifest(generated_at: str, rows: int = 7) -> dict:
    core = {
        "dataset_sha256": "d",
        "files": [{"path": "sql/seed.sql", "kind": "sql", "rows": rows}],
    }
    return {**core, "content_sha256": content_sha256(core), "run": {"generated_at": generated_at}}


def test_run_facts_compares_the_manifest_on_its_content() -> None:
    files = {
        repro.MANIFEST_PATH: _fact("bytes1", rows=None),
        "export/sql/seed.sql": _fact("s", None),
    }
    early = repro.run_facts(files, {repro.MANIFEST_PATH: _manifest("2026-11-20T10:00:00Z")})
    late = repro.run_facts(files, {repro.MANIFEST_PATH: _manifest("2026-11-20T11:00:00Z")})
    assert early[repro.MANIFEST_PATH]["sha256"] == late[repro.MANIFEST_PATH]["sha256"]
    assert early[repro.MANIFEST_PATH]["compared"] == "content"
    assert early[repro.MANIFEST_PATH]["file_sha256"] == "bytes1"
    assert early["export/sql/seed.sql"]["compared"] == "bytes"
    assert early["export/sql/seed.sql"]["rows"] == 7  # SQL rows come from the manifest
    changed = repro.run_facts(files, {repro.MANIFEST_PATH: _manifest("x", rows=8)})
    assert changed[repro.MANIFEST_PATH]["sha256"] != early[repro.MANIFEST_PATH]["sha256"]


def test_the_export_record_hash_leaves_out_only_the_manifest_digest() -> None:
    record = {
        "stage": "export",
        "outputs": {"starter/export/manifest.json": "m1", "starter/export/csv/raw/a.csv": "a1"},
    }
    other_manifest = {
        **record,
        "outputs": {**record["outputs"], "starter/export/manifest.json": "m2"},
    }
    other_csv = {**record, "outputs": {**record["outputs"], "starter/export/csv/raw/a.csv": "a2"}}
    assert repro.export_record_sha256(record) == repro.export_record_sha256(other_manifest)
    assert repro.export_record_sha256(record) != repro.export_record_sha256(other_csv)
    assert repro.export_record_sha256(record) != repro.export_record_sha256(
        {**record, "command": "coldchain export --no-verify-sql"}
    )


def test_compare_runs_flags_rows_hashes_and_missing_files() -> None:
    first = {"a": _fact("x"), "b": _fact("y", rows=5), "c": _fact("z"), "d": _fact("w")}
    second = {"a": _fact("x"), "b": _fact("y", rows=6), "c": _fact("Z"), "e": _fact("v")}
    rows = {row["path"]: row for row in repro.compare_runs(first, second)}
    assert list(rows) == ["a", "b", "c", "d", "e"]
    assert rows["a"]["match"]
    assert not rows["b"]["match"] and (rows["b"]["rows_run1"], rows["b"]["rows_run2"]) == (5, 6)
    assert not rows["c"]["match"]
    assert not rows["d"]["match"] and rows["d"]["sha256_run2"] is None
    assert not rows["e"]["match"] and rows["e"]["rows_run1"] is None


def test_control_comparison_needs_every_seed_dependent_file_to_change() -> None:
    run = {path: _fact("same") for path in repro.CONTROL_MUST_CHANGE} | {"ref": _fact("r")}
    control = {path: _fact("new") for path in repro.CONTROL_MUST_CHANGE} | {"ref": _fact("r")}
    result = repro.control_comparison(run, control)
    assert result["passed"] and result["unchanged"] == ["ref"]
    assert result["changed"] == len(repro.CONTROL_MUST_CHANGE)
    control[repro.CONTROL_MUST_CHANGE[0]] = _fact("same")
    result = repro.control_comparison(run, control)
    assert not result["passed"]
    assert result["must_change"][repro.CONTROL_MUST_CHANGE[0]] is False


def _summary(seed: int, exit_code: int = 0, stage: str | None = None) -> dict:
    return {
        "command": f"coldchain run-all --seed {seed}",
        "seed": seed,
        "status": "PASS" if exit_code == 0 else "FAIL",
        "exit_code": exit_code,
        "failed_stage": stage,
        "error": None if exit_code == 0 else "boom",
        "stages": [
            {
                "stage": "export",
                "details": {"dataset_sha256": f"{seed:064d}"},
                "timing": {"duration_s": 1.0, "peak_rss_mib": 10.0},
            }
        ],
        "timing": {"total_duration_s": 2.0, "peak_rss_mib": 10.0},
    }


def _report(facts: dict, summaries: dict | None = None) -> dict:
    summaries = summaries or {label: _summary(1) for label in repro.RUN_LABELS}
    return repro.build_report(
        scale=SCALE, seed=1, control_seed=2, verify_sql=False, summaries=summaries, facts=facts
    )


def _three_runs() -> dict:
    data = {path: _fact("one") for path in repro.CONTROL_MUST_CHANGE} | {"ref": _fact("r")}
    control = {path: _fact("two") for path in repro.CONTROL_MUST_CHANGE} | {"ref": _fact("r")}
    return {"run1": data, "run2": dict(data), "control": control}


def test_report_passes_when_every_file_matches_and_the_control_changes() -> None:
    payload = _report(_three_runs())
    assert payload["verdict"] == "PASS" and payload["failures"] == []
    assert payload["totals"]["matching"] == payload["totals"]["files"] == 7
    markdown = repro.report_markdown(payload)
    assert "**Verdict: PASS.**" in markdown
    assert "| File | Rows run1 | Rows run2 | sha256 match |" in markdown
    assert "| `ref` | 1 | 1 | yes |" in markdown
    assert "## Control run (seed `2`)" in markdown


def test_report_fails_on_a_mismatch() -> None:
    facts = _three_runs()
    facts["run2"]["ref"] = _fact("drift")
    payload = _report(facts)
    assert payload["verdict"] == "FAIL"
    assert payload["failures"] == ["1 file(s) differ between run1 and run2: ref"]
    assert "| `ref` | 1 | 1 | **NO** |" in repro.report_markdown(payload)


def test_report_fails_when_the_control_seed_changes_nothing() -> None:
    facts = _three_runs()
    facts["control"] = dict(facts["run1"])
    payload = _report(facts)
    assert payload["verdict"] == "FAIL"
    assert payload["failures"][0].startswith("the control seed did not change: raw/shipments")


def test_report_fails_when_a_run_fails_and_marks_the_rest_not_run() -> None:
    payload = _report({}, {"run1": _summary(1, exit_code=1, stage="inject")})
    assert payload["verdict"] == "FAIL"
    assert payload["failures"] == [
        "run1 failed at stage inject: boom",
        "run2 was not run (an earlier run failed)",
        "control was not run (an earlier run failed)",
    ]
    assert payload["files"] == [] and payload["control"] == {}
    assert "**Verdict: FAIL.**" in repro.report_markdown(payload)


# --------------------------------------------------------------------------------------------
# CLI with a fake pipeline (fast): every branch of `coldchain reproduce`
# --------------------------------------------------------------------------------------------


def _fake_pipeline(drift: bool = False, fail: bool = False):
    calls: list[dict] = []

    def run_pipeline(options, *, seed, verify_sql, commands, command):
        calls.append({"root": Path(options.output_dir), "seed": seed, "command": command})
        effective = 20261022 if seed is None else seed
        scale_dir = options.scale_dir
        for path in repro.CONTROL_MUST_CHANGE:
            target = scale_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.suffix == ".parquet":
                pd.DataFrame({"seed": [effective]}).to_parquet(target, index=False)
            else:
                target.write_text(json.dumps({"seed": effective}), encoding="utf-8")
        (scale_dir / "raw" / "reference.csv").write_text("a\n1\n", encoding="utf-8")
        if drift and len(calls) == 2:
            (scale_dir / "raw" / "reference.csv").write_text("a\n2\n", encoding="utf-8")
        summary = _summary(
            effective, exit_code=1 if fail else 0, stage="generate" if fail else None
        )
        summary["command"] = command
        return summary

    return run_pipeline, calls


def _reproduce(tmp_path: Path, *extra: str):
    return runner.invoke(
        app, ["reproduce", "--scale", SCALE, "--output-dir", str(tmp_path), *extra]
    )


def _report_json(tmp_path: Path) -> dict:
    path = tmp_path / SCALE / "reproducibility" / "reproducibility_check.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_reproduce_runs_three_times_at_one_path(monkeypatch, tmp_path: Path) -> None:
    fake, calls = _fake_pipeline()
    monkeypatch.setattr(pipeline, "run_pipeline", fake)
    result = _reproduce(tmp_path)
    assert result.exit_code == 0, result.output
    current = tmp_path / SCALE / "reproducibility" / "runs" / "current"
    assert [c["root"] for c in calls] == [current] * 3
    assert [c["seed"] for c in calls] == [None, None, 20261023]  # run1/run2 as a plain run-all
    assert "--seed 20261023" in calls[2]["command"]
    assert "--seed" not in calls[0]["command"]
    assert "--no-verify-sql" in calls[0]["command"]  # reproduce's default (DECISIONS #118)
    payload = _report_json(tmp_path)
    assert payload["verdict"] == "PASS"
    assert payload["control"]["unchanged"] == ["raw/reference.csv"]
    assert not (tmp_path / SCALE / "reproducibility" / "runs").exists()  # removed by default


def test_reproduce_keeps_the_runs_on_request(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline, "run_pipeline", _fake_pipeline()[0])
    assert _reproduce(tmp_path, "--keep-runs", "--seed", "5", "--control-seed", "9").exit_code == 0
    runs = tmp_path / SCALE / "reproducibility" / "runs"
    assert sorted(p.name for p in runs.iterdir()) == ["control", "run1", "run2"]
    payload = _report_json(tmp_path)
    assert (payload["seed"], payload["control_seed"]) == (5, 9)


def test_reproduce_exits_1_on_any_difference(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(pipeline, "run_pipeline", _fake_pipeline(drift=True)[0])
    result = _reproduce(tmp_path)
    assert result.exit_code == 1
    assert "1 file(s) differ between run1 and run2: raw/reference.csv" in result.output
    assert _report_json(tmp_path)["verdict"] == "FAIL"


def test_reproduce_exits_1_when_a_run_fails(monkeypatch, tmp_path: Path) -> None:
    fake, calls = _fake_pipeline(fail=True)
    monkeypatch.setattr(pipeline, "run_pipeline", fake)
    result = _reproduce(tmp_path)
    assert result.exit_code == 1
    assert "run1 failed at stage generate" in result.output
    assert len(calls) == 1


def test_reproduce_refuses_a_control_seed_equal_to_the_seed(tmp_path: Path) -> None:
    result = _reproduce(tmp_path, "--seed", "4", "--control-seed", "4")
    assert result.exit_code == 1
    assert "control seed must differ" in result.output


# --------------------------------------------------------------------------------------------
# The real thing at starter scale (slow)
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def reproduced(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("reproduce")
    result = runner.invoke(
        app,
        [
            "--log-level",
            "WARNING",
            "reproduce",
            "--scale",
            SCALE,
            "--output-dir",
            str(root),
            "--keep-runs",
        ],
    )
    assert result.exit_code == 0, result.output
    return root


@pytest.mark.slow
def test_same_seed_gives_identical_files(reproduced: Path) -> None:
    payload = _report_json(reproduced)
    assert payload["verdict"] == "PASS", payload["failures"]
    files = payload["files"]
    assert files and all(row["match"] for row in files)
    assert payload["totals"]["files"] == len(files) > 100
    folders = {row["path"].split("/")[0] for row in files}
    assert {"raw", "baseline", "ground_truth", "compliance", "validation", "evaluation"} <= folders
    assert {"provenance", "export", "run_summary.json"} <= folders
    content = {row["path"] for row in files if row["compared"] == "content"}
    assert content == set(repro.CONTENT_HASHED)  # every other file is compared byte for byte
    rows = {row["path"]: row["rows_run1"] for row in files}
    assert rows["raw/shipments.parquet"] == rows["raw/shipments.csv"] == 50
    assert (
        rows["export/json/raw/temperature_readings.jsonl"] == rows["raw/temperature_readings.csv"]
    )
    assert rows["export/sql/seed.sql"] > rows["raw/temperature_readings.csv"]


@pytest.mark.slow
def test_a_different_seed_changes_the_data(reproduced: Path) -> None:
    payload = _report_json(reproduced)
    control = payload["control"]
    assert control["passed"] and all(control["must_change"].values())
    assert control["changed"] > control["files"] // 2
    datasets = {label: run["dataset_sha256"] for label, run in payload["runs"].items()}
    assert datasets["run1"] == datasets["run2"] != datasets["control"]
    markdown = (reproduced / SCALE / "reproducibility" / "reproducibility_check.md").read_text()
    assert "**Verdict: PASS.**" in markdown


@pytest.mark.slow
def test_each_run_has_a_complete_run_summary(reproduced: Path) -> None:
    summary = json.loads(
        (
            reproduced / SCALE / "reproducibility" / "runs" / "run1" / SCALE / "run_summary.json"
        ).read_text()
    )
    assert (summary["status"], summary["exit_code"]) == ("PASS", 0)
    assert [s["stage"] for s in summary["stages"]] == list(pipeline.RUN_ALL_STAGES)
    assert all(s["status"] == "PASS" and s["timing"]["duration_s"] > 0 for s in summary["stages"])
    by_stage = {s["stage"]: s for s in summary["stages"]}
    assert by_stage["generate"]["rows"]["raw/shipments"] == 50
    assert by_stage["evaluate"]["details"]["verdict"] == "PASS"
    assert by_stage["validate_baseline"]["details"]["findings"] == 0
    assert by_stage["export"]["details"]["sql_verification"] is None  # --no-verify-sql


@pytest.mark.slow
def test_run_all_writes_what_the_single_commands_write(reproduced: Path) -> None:
    """Run every stage by hand into the path run-all used: the files must be byte-identical."""
    runs = reproduced / SCALE / "reproducibility" / "runs"
    current = runs / pipeline.CURRENT_RUN
    opts = ["--scale", SCALE, "--output-dir", str(current)]
    commands = [
        ["generate"],
        ["inject"],
        ["check"],
        ["validate"],
        ["validate", "--source", "baseline"],
        ["evaluate"],
        ["export", "--no-verify-sql"],
    ]
    for command in commands:
        result = runner.invoke(app, ["--log-level", "WARNING", *command, *opts])
        assert result.exit_code == 0, result.output
    by_hand = repro.run_facts(describe_files(current / SCALE), {})
    run_all = repro.run_facts(describe_files(runs / "run1" / SCALE), {})
    differ = {row["path"] for row in repro.compare_runs(run_all, by_hand) if not row["match"]}
    # Only run-all writes run_summary.json; the manifest differs only in generated_at.
    assert differ == {"run_summary.json", repro.MANIFEST_PATH, repro.EXPORT_RECORD_PATH}
    record = json.loads((current / SCALE / "provenance" / "export.json").read_text())
    assert record["command"] == (
        "coldchain export --scale starter --config config/default.yaml "
        f"--output-dir {current.resolve()} --no-verify-sql"
    )
    manifests = [
        json.loads((root / SCALE / "export" / "manifest.json").read_text())
        for root in (current, runs / "run1")
    ]
    assert manifests[0]["content_sha256"] == manifests[1]["content_sha256"]
    assert manifests[0]["pipeline"] == manifests[1]["pipeline"]
    assert {s.value for s in PipelineStage} - {"export"} <= {
        path.stem for path in (current / SCALE / "provenance").iterdir()
    }
