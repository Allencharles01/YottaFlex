"""End to end at starter scale: ``coldchain generate`` then ``coldchain check``.

The engine's results must match ``ground_truth/expected_outcomes`` for every shipment: status,
action, reason codes, excursion count, windows and durations. (This *test* reads the ground
truth; the engine never does, see ``test_compliance_isolation.py``.) It also follows every
decision back to its readings and policy (compliance_rules.md §3), checks that the raw layer is
left untouched, and that a rerun is byte-identical.
"""

import hashlib
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from coldchain.cli import app
from coldchain.compliance import ENGINE_OUTPUTS
from coldchain.enums import Layer, TableName
from coldchain.io import layer_dir, read_table
from coldchain.schemas import ISO_TIMESTAMP_FORMAT, split_list

pytestmark = pytest.mark.slow

runner = CliRunner()


def _digests(directory: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> dict:
    """Generate and check starter data once; return the root, layers and raw digests."""
    root = tmp_path_factory.mktemp("e2e")
    generated = runner.invoke(app, ["generate", "--scale", "starter", "--output-dir", str(root)])
    assert generated.exit_code == 0, generated.output
    raw_dir = layer_dir(root, "starter", Layer.RAW)
    raw_before = _digests(raw_dir)
    checked = runner.invoke(app, ["check", "--scale", "starter", "--output-dir", str(root)])
    assert checked.exit_code == 0, checked.output
    compliance = layer_dir(root, "starter", Layer.COMPLIANCE)
    truth = layer_dir(root, "starter", Layer.GROUND_TRUTH)
    return {
        "root": root,
        "raw_dir": raw_dir,
        "raw_before": raw_before,
        "compliance_dir": compliance,
        "decisions": read_table(TableName.COMPLIANCE_DECISIONS, compliance).set_index(
            "shipment_id"
        ),
        "reports": read_table(TableName.EXCURSION_REPORTS, compliance),
        "bridge": read_table(TableName.EXCURSION_REPORT_READINGS, compliance),
        "events": read_table(TableName.COMPLIANCE_STATUS_EVENTS, compliance),
        "expected": read_table(TableName.EXPECTED_OUTCOMES, truth).set_index("shipment_id"),
        "raw": {
            name: read_table(name, raw_dir)
            for name in (
                TableName.TEMPERATURE_READINGS,
                TableName.THRESHOLD_POLICIES,
                TableName.SHIPMENT_STATUS_EVENTS,
                TableName.SHIPMENTS,
            )
        },
    }


def _mismatches(left: pd.Series, right: pd.Series) -> list[str]:
    a, b = left.fillna(""), right.reindex(left.index).fillna("")
    return [f"{i}: engine={a[i]!r} expected={b[i]!r}" for i in a.index[a != b]]


def test_every_shipment_has_one_decision(run) -> None:
    assert sorted(run["decisions"].index) == sorted(run["expected"].index)
    assert len(run["decisions"]) == 50


def test_statuses_and_actions_match_ground_truth(run) -> None:
    decisions, expected = run["decisions"], run["expected"]
    assert _mismatches(decisions["status"], expected["expected_status"]) == []
    assert _mismatches(decisions["decided_action"], expected["expected_action"]) == []


def test_reason_codes_match_ground_truth(run) -> None:
    decisions, expected = run["decisions"], run["expected"]
    assert _mismatches(decisions["reason_codes"], expected["expected_reason_codes"]) == []


def test_excursion_counts_windows_and_durations_match_ground_truth(run) -> None:
    reports = run["reports"].sort_values(["shipment_id", "started_at"])
    windows = (
        reports["started_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
        + "/"
        + reports["ended_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
    )
    by_ship = reports.assign(window=windows, duration=reports["duration_min"].astype(str))
    grouped = by_ship.groupby("shipment_id")
    index = run["expected"].index
    count = grouped.size().reindex(index, fill_value=0)
    expected = run["expected"]
    assert (count == expected["expected_excursion_count"]).all()
    assert (
        _mismatches(
            grouped["duration"].agg("|".join).reindex(index),
            expected["expected_excursion_durations_min"],
        )
        == []
    )
    assert (
        _mismatches(
            grouped["window"].agg("|".join).reindex(index), expected["expected_excursion_windows"]
        )
        == []
    )
    totals = run["decisions"]["total_excursion_min"].reindex(index)
    assert (totals == expected["expected_total_excursion_min"]).all()


def test_starter_covers_every_breach_type_and_status(run) -> None:
    assert set(run["reports"]["breach_type"]) == {
        "WITHIN_ALLOWANCE",
        "SINGLE_DURATION_BREACH",
        "ABSOLUTE_LIMIT_BREACH",
    }
    assert set(run["decisions"]["status"]) == {
        "COMPLIANT",
        "COMPLIANT_WITH_WARNINGS",
        "INCONCLUSIVE",
        "NON_COMPLIANT",
    }


def test_decisions_trace_to_out_of_range_readings(run) -> None:
    """compliance_rules.md §3: decision → reports → bridge → readings, under the applied policy."""
    readings = run["raw"][TableName.TEMPERATURE_READINGS].set_index("reading_id")
    policies = run["raw"][TableName.THRESHOLD_POLICIES].set_index("policy_id")
    reports = run["reports"].set_index("report_id")
    bridge = run["bridge"]
    decisions = run["decisions"]

    for shipment, row in decisions.iterrows():
        for report_id in split_list(row["excursion_report_ids"]):
            assert reports.loc[report_id, "shipment_id"] == shipment
            assert reports.loc[report_id, "policy_id"] == row["applied_policy_id"]
    assert sorted(reports.index) == sorted(
        rid for ids in decisions["excursion_report_ids"] for rid in split_list(ids)
    )

    linked = bridge.merge(readings, left_on="reading_id", right_index=True, how="left")
    linked = linked.merge(reports, left_on="report_id", right_index=True, suffixes=("", "_r"))
    assert linked["recorded_at"].notna().all(), "every linked reading exists"
    assert (linked["logger_id"] == linked["logger_id_r"]).all()
    limits = policies.loc[linked["policy_id"]]
    high = linked["direction"].to_numpy() == "HIGH"
    temps = linked["temperature_c"].to_numpy()
    assert (temps[high] > limits["max_c"].to_numpy()[high]).all()
    assert (temps[~high] < limits["min_c"].to_numpy()[~high]).all()

    per_report = linked.groupby("report_id").agg(
        n=("reading_id", "size"), first=("recorded_at", "min"), last=("recorded_at", "max")
    )
    assert (per_report["n"] == reports.loc[per_report.index, "reading_count"]).all()
    assert (per_report["first"] == reports.loc[per_report.index, "started_at"]).all()
    assert (per_report["last"] == reports.loc[per_report.index, "ended_at"]).all()
    recomputed = (per_report["last"] - per_report["first"]).dt.total_seconds() // 60 + 10
    assert (recomputed == reports.loc[per_report.index, "duration_min"]).all()


def test_status_events_close_the_lifecycle(run) -> None:
    events, decisions = run["events"], run["decisions"]
    raw_last = run["raw"][TableName.SHIPMENT_STATUS_EVENTS].groupby("shipment_id")["sequence"].max()
    review = events[events["status"] == "UNDER_REVIEW"].set_index("shipment_id")
    assert sorted(review.index) == sorted(decisions.index)
    assert (review["sequence"] == raw_last.loc[review.index] + 1).all()
    final = events[events["status"] != "UNDER_REVIEW"].set_index("shipment_id")
    closing = decisions.loc[decisions["decided_action"] != "REVIEW", "decided_action"]
    assert sorted(final.index) == sorted(closing.index)
    expected_final = closing.map({"RELEASE": "RELEASED", "QUARANTINE": "QUARANTINED"})
    assert (final.loc[closing.index, "status"] == expected_final).all()


def test_no_carrier_contradiction_in_natural_data(run) -> None:
    """DECISIONS.md #38: the baseline claim agrees with the rules."""
    assert not run["decisions"]["reason_codes"].fillna("").str.contains("CARRIER").any()


def test_raw_layer_is_unchanged_by_check(run) -> None:
    assert _digests(run["raw_dir"]) == run["raw_before"]


def test_check_writes_only_compliance_tables(run) -> None:
    names = {p.name for p in run["compliance_dir"].iterdir()}
    assert names == {f"{t.value}.{ext}" for t in ENGINE_OUTPUTS for ext in ("csv", "parquet")}


def test_check_rerun_is_byte_identical(run) -> None:
    before = _digests(run["compliance_dir"])
    again = runner.invoke(app, ["check", "--scale", "starter", "--output-dir", str(run["root"])])
    assert again.exit_code == 0, again.output
    assert _digests(run["compliance_dir"]) == before
