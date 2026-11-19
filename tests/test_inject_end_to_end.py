"""End to end at starter scale: ``generate`` → ``inject`` → ``check`` (Phase 5 UAT, BACKLOG #2).

* ``coldchain check`` completes on injected data, and the engine matches the updated
  ``expected_outcomes`` for **every** shipment, natural and injected;
* the clean baseline is preserved byte for byte, and a diff against it shows that only the
  target shipments' records changed;
* re-running ``inject`` is idempotent, and ``generate`` removes a stale injection.
(This *test* reads the ground truth; the engine never does, see ``test_compliance_isolation.py``.)
"""

import hashlib
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from coldchain.cli import app
from coldchain.enums import FileFormat, IssueType, Layer, TableName
from coldchain.io import baseline_dir, baseline_root, layer_dir, raw_tables, read_table
from coldchain.schemas import ISO_TIMESTAMP_FORMAT

pytestmark = pytest.mark.slow

runner = CliRunner()


def _invoke(*args: str) -> None:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, result.output


def _digests(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """Generate, inject and check once; keep the generator's digests for comparison."""
    root = tmp_path_factory.mktemp("inject_e2e")
    opts = ("--scale", "starter", "--output-dir", str(root))
    _invoke("generate", *opts)
    generated = {
        layer: _digests(layer_dir(root, "starter", layer))
        for layer in (Layer.RAW, Layer.GROUND_TRUTH)
    }
    _invoke("inject", *opts)
    _invoke("check", *opts)
    compliance = layer_dir(root, "starter", Layer.COMPLIANCE)
    truth = layer_dir(root, "starter", Layer.GROUND_TRUTH)
    return {
        "root": root,
        "opts": opts,
        "generated": generated,
        "decisions": read_table(TableName.COMPLIANCE_DECISIONS, compliance).set_index(
            "shipment_id"
        ),
        "reports": read_table(TableName.EXCURSION_REPORTS, compliance),
        "expected": read_table(TableName.EXPECTED_OUTCOMES, truth).set_index("shipment_id"),
        "anomalies": read_table(TableName.ANOMALIES, truth),
    }


def _mismatches(left: pd.Series, right: pd.Series) -> list[str]:
    a, b = left.fillna(""), right.reindex(left.index).fillna("")
    return [f"{i}: engine={a[i]!r} expected={b[i]!r}" for i in a.index[a != b]]


def test_check_matches_the_updated_ground_truth_for_every_shipment(run) -> None:
    decisions, expected = run["decisions"], run["expected"]
    assert len(decisions) == 50 and sorted(decisions.index) == sorted(expected.index)
    assert _mismatches(decisions["status"], expected["expected_status"]) == []
    assert _mismatches(decisions["decided_action"], expected["expected_action"]) == []
    assert _mismatches(decisions["reason_codes"], expected["expected_reason_codes"]) == []
    totals = decisions["total_excursion_min"].reindex(expected.index)
    assert (totals == expected["expected_total_excursion_min"]).all()


def test_excursion_windows_and_durations_match_for_every_shipment(run) -> None:
    reports = run["reports"].sort_values(["shipment_id", "started_at"])
    windows = (
        reports["started_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
        + "/"
        + reports["ended_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
    )
    grouped = reports.assign(window=windows, duration=reports["duration_min"].astype(str))
    grouped = grouped.groupby("shipment_id")
    expected = run["expected"]
    index = expected.index
    assert (
        grouped.size().reindex(index, fill_value=0) == expected["expected_excursion_count"]
    ).all()
    durations = grouped["duration"].agg("|".join).reindex(index)
    assert _mismatches(durations, expected["expected_excursion_durations_min"]) == []
    joined = grouped["window"].agg("|".join).reindex(index)
    assert _mismatches(joined, expected["expected_excursion_windows"]) == []


def test_every_anomaly_is_flagged_with_its_expected_codes(run) -> None:
    anomalies, decisions = run["anomalies"], run["decisions"]
    assert set(anomalies["issue_type"]) == {issue.value for issue in IssueType}
    found = decisions.loc[anomalies["shipment_id"]]
    assert found["status"].tolist() == anomalies["expected_status"].tolist()
    assert found["reason_codes"].tolist() == anomalies["expected_reason_codes"].tolist()


def test_pair_durations_are_separate_per_shipment(run) -> None:
    pair = run["anomalies"][
        run["anomalies"]["issue_type"] == IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value
    ]
    reports = run["reports"].set_index("shipment_id")
    for row in pair.itertuples():
        engine = reports.loc[[row.shipment_id], "duration_min"].astype(str).tolist()
        assert engine == [row.expected_excursion_durations_min]


def test_baseline_is_the_untouched_generator_output(run) -> None:
    root = run["root"]
    assert _digests(baseline_dir(root, "starter", Layer.RAW)) == run["generated"][Layer.RAW]
    truth = _digests(baseline_dir(root, "starter", Layer.GROUND_TRUTH))
    assert truth == {
        name: digest
        for name, digest in run["generated"][Layer.GROUND_TRUTH].items()
        if name.startswith(TableName.EXPECTED_OUTCOMES.value)
    }


def test_diff_against_the_baseline_touches_only_target_shipments(run) -> None:
    root = run["root"]
    targets = set(run["anomalies"]["shipment_id"])
    changed_files = {
        name
        for name, digest in _digests(layer_dir(root, "starter", Layer.RAW)).items()
        if run["generated"][Layer.RAW][name] != digest
    }
    assert changed_files == {
        f"{t.value}.{f.value}"
        for t in (TableName.SHIPMENTS, TableName.TEMPERATURE_READINGS, TableName.CUSTODY_EVENTS)
        for f in (FileFormat.PARQUET, FileFormat.CSV)
    }
    for table in raw_tables():
        before = read_table(table, baseline_dir(root, "starter", Layer.RAW))
        after = read_table(table, layer_dir(root, "starter", Layer.RAW))
        merged = before.merge(after, how="outer", indicator=True, on=list(before.columns))
        diff = merged[merged["_merge"] != "both"]
        scoped = "shipment_id" if "shipment_id" in diff.columns else "logger_id"
        if diff.empty:
            continue
        ships = diff[scoped].str.extract(r"(SHP-\d{4}-\d{6})")[0]
        assert ships.notna().all() and set(ships) <= targets, table.value


def test_anomalies_are_written_as_parquet_csv_and_json(run) -> None:
    truth = layer_dir(run["root"], "starter", Layer.GROUND_TRUTH)
    from_json = read_table(TableName.ANOMALIES, truth, FileFormat.JSON)
    pd.testing.assert_frame_equal(from_json, run["anomalies"])
    records = json.loads((truth / "anomalies.json").read_text(encoding="utf-8"))
    assert len(records) == len(run["anomalies"]) == 6


def test_inject_rerun_is_idempotent(run) -> None:
    root, opts = run["root"], run["opts"]
    before = {layer: _digests(layer_dir(root, "starter", layer)) for layer in Layer}
    _invoke("inject", *opts)
    after = {layer: _digests(layer_dir(root, "starter", layer)) for layer in Layer}
    assert after == before


def test_generate_removes_a_stale_injection(run, tmp_path) -> None:
    root = tmp_path / "copy"
    shutil.copytree(run["root"], root)
    opts = ("--scale", "starter", "--output-dir", str(root))
    _invoke("generate", *opts)
    assert not baseline_root(root, "starter").exists()
    truth = layer_dir(root, "starter", Layer.GROUND_TRUTH)
    assert not list(truth.glob("anomalies.*"))
    assert _digests(layer_dir(root, "starter", Layer.RAW)) == run["generated"][Layer.RAW]


def test_inject_without_generated_data_fails_cleanly(tmp_path) -> None:
    result = runner.invoke(app, ["inject", "--scale", "starter", "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "run `coldchain generate` first" in result.output


def test_inject_seed_option_changes_the_targets(run, tmp_path) -> None:
    root = tmp_path / "seeded"
    shutil.copytree(run["root"], root)
    _invoke("inject", "--scale", "starter", "--output-dir", str(root), "--seed", "7")
    other = read_table(TableName.ANOMALIES, layer_dir(root, "starter", Layer.GROUND_TRUTH))
    assert other["shipment_id"].tolist() != run["anomalies"]["shipment_id"].tolist()
    assert _digests(baseline_dir(root, "starter", Layer.RAW)) == run["generated"][Layer.RAW]
