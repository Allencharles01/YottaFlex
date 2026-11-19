"""Decision table, carrier claim and appended status events (compliance_rules.md §2, §1.10, §4).

These tests run the whole engine (:func:`coldchain.compliance.check_tables`) on hand-built
one- and two-shipment raw layers.
"""

import pandas as pd
import pytest

from coldchain.compliance import ENGINE_INPUTS, ENGINE_OUTPUTS, check_tables, report
from coldchain.enums import Layer, ReasonCode, TableName
from coldchain.errors import ComplianceError
from coldchain.schemas import TABLE_LAYERS
from conftest import (
    LOG,
    LOG2,
    SHP,
    SHP2,
    _ts,
    add_back_to_back_shipment,
    drop_readings,
    set_temperatures,
    set_temperatures_of,
)


def _decision(app_config, raw) -> pd.Series:
    return (
        check_tables(app_config, raw)[TableName.COMPLIANCE_DECISIONS]
        .set_index("shipment_id")
        .loc[SHP]
    )


def test_engine_inputs_are_raw_and_outputs_are_compliance() -> None:
    assert all(TABLE_LAYERS[name] is Layer.RAW for name in ENGINE_INPUTS)
    assert all(TABLE_LAYERS[name] is Layer.COMPLIANCE for name in ENGINE_OUTPUTS)


def test_compliant_shipment(app_config, raw_one) -> None:
    tables = check_tables(app_config, raw_one)
    row = tables[TableName.COMPLIANCE_DECISIONS].iloc[0]
    assert row["status"] == "COMPLIANT" and row["decided_action"] == "RELEASE"
    assert pd.isna(row["reason_codes"]) and pd.isna(row["excursion_report_ids"])
    assert row["applied_policy_id"] == "THR-REFRIG_2_8-V1"
    assert row["policy_source"] == "DECLARED_CONFIRMED"
    assert (row["reading_count"], row["expected_reading_count"]) == (31, 31)
    assert (row["coverage_pct"], row["max_gap_min"], row["total_excursion_min"]) == (100, 10, 0)
    assert tables[TableName.EXCURSION_REPORTS].empty
    assert tables[TableName.EXCURSION_REPORT_READINGS].empty


@pytest.mark.parametrize(
    ("temperatures", "status", "reasons"),
    [
        ({"0900": 8.5}, "COMPLIANT_WITH_WARNINGS", "EXCURSION_WITHIN_ALLOWANCE"),
        ({"0900": 8.0}, "COMPLIANT_WITH_WARNINGS", "NEAR_LIMIT_UNCERTAIN"),
        (
            {"0900": 8.5, "0910": 8.5, "0920": 8.5, "0930": 8.5},
            "NON_COMPLIANT",
            "SINGLE_DURATION_BREACH",
        ),
        ({"0900": 12.01}, "NON_COMPLIANT", "ABSOLUTE_LIMIT_BREACH"),
        (
            {"0900": 9.0, "0910": 8.0},  # breach-free excursion + an at-limit reading
            "COMPLIANT_WITH_WARNINGS",
            "EXCURSION_WITHIN_ALLOWANCE|NEAR_LIMIT_UNCERTAIN",
        ),
    ],
)
def test_temperature_decisions(app_config, raw_one, temperatures, status, reasons) -> None:
    set_temperatures(raw_one, temperatures)
    row = _decision(app_config, raw_one)
    assert row["status"] == status
    # The carrier said PASS, so a QUARANTINE decision contradicts it.
    expected = reasons + ("|CARRIER_STATUS_CONTRADICTED" if status == "NON_COMPLIANT" else "")
    assert row["reason_codes"] == expected


def test_all_codes_are_kept_and_worst_status_wins(app_config, raw_one) -> None:
    """A breach, a gap, a custody gap and a dwell warning: every code is listed, in order."""
    set_temperatures(raw_one, {"0900": 12.5, "0950": 8.5})
    drop_readings(raw_one, "1100", "1110", "1120")
    events = raw_one[TableName.CUSTODY_EVENTS]
    raw_one[TableName.CUSTODY_EVENTS] = events[events["sequence"] != 3]
    stops = raw_one[TableName.ROUTE_STOPS]
    stops.loc[stops["sequence"] == 2, "dwell_min"] = 121
    row = _decision(app_config, raw_one)
    assert row["status"] == "NON_COMPLIANT" and row["decided_action"] == "QUARANTINE"
    assert row["reason_codes"] == (
        "ABSOLUTE_LIMIT_BREACH|DATA_GAP|LOW_COVERAGE|CUSTODY_GAP|EXCURSION_WITHIN_ALLOWANCE"
        "|LONG_DWELL|CARRIER_STATUS_CONTRADICTED"
    )
    assert row["excursion_report_ids"] == "EXCR-2026-000001|EXCR-2026-000002"
    assert row["total_excursion_min"] == 20


def test_policy_mismatch_is_a_warning_and_overrides(app_config, raw_one) -> None:
    raw_one[TableName.SHIPMENTS]["declared_policy_id"] = "THR-REFRIG_2_8-V2"
    row = _decision(app_config, raw_one)
    assert row["status"] == "COMPLIANT_WITH_WARNINGS"
    assert row["reason_codes"] == "POLICY_MISMATCH_DECLARED"
    assert row["policy_source"] == "PRODUCT_OVERRIDE"
    assert row["applied_policy_id"] == "THR-REFRIG_2_8-V1"


def test_missing_logger_is_inconclusive(app_config, raw_one) -> None:
    raw_one[TableName.LOGGERS] = raw_one[TableName.LOGGERS].iloc[0:0]
    tables = check_tables(app_config, raw_one)
    row = tables[TableName.COMPLIANCE_DECISIONS].iloc[0]
    assert row["status"] == "INCONCLUSIVE" and row["decided_action"] == "REVIEW"
    assert row["reason_codes"] == (
        "LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE|CARRIER_STATUS_CONTRADICTED"
    )
    assert (row["reading_count"], row["expected_reading_count"]) == (0, 25)
    events = tables[TableName.COMPLIANCE_STATUS_EVENTS]
    assert list(events["status"]) == ["UNDER_REVIEW"]
    assert events.iloc[0]["event_at"] == pd.Timestamp("2026-03-10 12:00", tz="UTC")


def test_wrong_device_readings_are_inconclusive(app_config, raw_one) -> None:
    raw_one[TableName.TEMPERATURE_READINGS]["device_id"] = "DEV-000002"
    row = _decision(app_config, raw_one)
    assert row["status"] == "INCONCLUSIVE" and row["reading_count"] == 0
    assert row["reason_codes"].startswith("LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE")


def test_calibration_expired_is_inconclusive(app_config, raw_one) -> None:
    raw_one[TableName.DEVICES]["calibration_due_on"] = [pd.Timestamp("2026-03-09").date()]
    raw_one[TableName.SHIPMENTS]["carrier_reported_status"] = "FAIL"
    row = _decision(app_config, raw_one)
    assert row["status"] == "INCONCLUSIVE"
    assert row["reason_codes"] == "CALIBRATION_EXPIRED"  # FAIL agrees with REVIEW


@pytest.mark.parametrize(
    ("claim", "breach", "contradicted"),
    [("PASS", False, False), ("FAIL", False, True), ("PASS", True, True), ("FAIL", True, False)],
)
def test_carrier_claim(app_config, raw_one, claim, breach, contradicted) -> None:
    raw_one[TableName.SHIPMENTS]["carrier_reported_status"] = claim
    if breach:
        set_temperatures(raw_one, {"0900": 13.0})
    row = _decision(app_config, raw_one)
    codes = (row["reason_codes"] or "").split("|") if pd.notna(row["reason_codes"]) else []
    assert (ReasonCode.CARRIER_STATUS_CONTRADICTED.value in codes) == contradicted
    assert row["status"] == ("NON_COMPLIANT" if breach else "COMPLIANT")  # informational only


def test_status_events_continue_the_raw_history(app_config, raw_one) -> None:
    events = check_tables(app_config, raw_one)[TableName.COMPLIANCE_STATUS_EVENTS]
    assert list(events["status_event_id"]) == [f"STE-{SHP}-07", f"STE-{SHP}-08"]
    assert list(events["sequence"]) == [7, 8]
    assert list(events["status"]) == ["UNDER_REVIEW", "RELEASED"]
    assert list(events["actor_party_id"]) == ["PTY-0006", "PTY-0007"]
    assert (events["event_at"] == pd.Timestamp("2026-03-10 12:30", tz="UTC")).all()


def test_quarantine_appends_quarantined(app_config, raw_one) -> None:
    set_temperatures(raw_one, {"0900": 13.0})
    events = check_tables(app_config, raw_one)[TableName.COMPLIANCE_STATUS_EVENTS]
    assert list(events["status"]) == ["UNDER_REVIEW", "QUARANTINED"]


def test_raw_tables_are_not_modified(app_config, raw_one) -> None:
    before = {name: frame.copy() for name, frame in raw_one.items()}
    check_tables(app_config, raw_one)
    for name, frame in raw_one.items():
        pd.testing.assert_frame_equal(frame, before[name])


def test_actors_are_assigned_round_robin(app_config, raw_one) -> None:
    """DECISIONS.md #61: shipment k (in ID order) gets analyst/manager k mod n."""
    add_back_to_back_shipment(raw_one)
    parties = raw_one[TableName.PARTIES]
    extra = parties[parties["party_type"].isin(["LOGISTICS_ANALYST", "OPERATIONS_MANAGER"])]
    extra = extra.assign(party_id=["PTY-0008", "PTY-0009"], name=["QA Analyst B", "Ops Manager B"])
    raw_one[TableName.PARTIES] = pd.concat([parties, extra], ignore_index=True)
    events = check_tables(app_config, raw_one)[TableName.COMPLIANCE_STATUS_EVENTS]
    actors = events.set_index("status_event_id")["actor_party_id"]
    assert actors[f"STE-{SHP}-07"] == "PTY-0006" and actors[f"STE-{SHP}-08"] == "PTY-0007"
    assert actors[f"STE-{SHP2}-07"] == "PTY-0008" and actors[f"STE-{SHP2}-08"] == "PTY-0009"


def test_missing_reviewer_party_raises(app_config, raw_one) -> None:
    parties = raw_one[TableName.PARTIES]
    raw_one[TableName.PARTIES] = parties[parties["party_type"] != "LOGISTICS_ANALYST"]
    with pytest.raises(ComplianceError, match="LOGISTICS_ANALYST"):
        check_tables(app_config, raw_one)


def test_missing_input_table_raises(app_config, raw_one) -> None:
    del raw_one[TableName.CUSTODY_EVENTS]
    with pytest.raises(ComplianceError, match="custody_events"):
        check_tables(app_config, raw_one)


def test_excursion_report_ids_are_numbered_across_shipments(app_config, raw_one) -> None:
    add_back_to_back_shipment(raw_one)
    set_temperatures_of(raw_one, LOG2, {"1300": 9.0})
    set_temperatures_of(raw_one, LOG, {"0900": 9.0})
    tables = check_tables(app_config, raw_one)
    reports = tables[TableName.EXCURSION_REPORTS]
    assert list(reports["report_id"]) == ["EXCR-2026-000001", "EXCR-2026-000002"]
    assert list(reports["shipment_id"]) == [SHP, SHP2]
    decisions = tables[TableName.COMPLIANCE_DECISIONS].set_index("shipment_id")
    assert decisions.loc[SHP2, "excursion_report_ids"] == "EXCR-2026-000002"
    assert reports.iloc[0]["logger_id"] == LOG


def test_empty_run(app_config, raw_one) -> None:
    for name in (
        TableName.SHIPMENTS,
        TableName.SHIPMENT_LOTS,
        TableName.LOGGERS,
        TableName.TEMPERATURE_READINGS,
        TableName.ROUTE_STOPS,
        TableName.CUSTODY_EVENTS,
        TableName.SHIPMENT_STATUS_EVENTS,
    ):
        raw_one[name] = raw_one[name].iloc[0:0]
    tables = check_tables(app_config, raw_one)
    assert set(tables) == set(ENGINE_OUTPUTS)
    assert all(frame.empty for frame in tables.values())


def test_reason_helpers() -> None:
    index = pd.Index(["A", "B"], name="shipment_id")
    matrix = report.reason_matrix(
        index,
        [
            pd.Series([True, False], index=index, name="LONG_DWELL"),
            pd.DataFrame({"DATA_GAP": [False, True], "unrelated": [1, 2]}, index=index),
        ],
    )
    assert list(matrix.columns) == [code.value for code in ReasonCode]
    assert list(report.reason_codes_text(matrix)) == ["LONG_DWELL", "DATA_GAP"]
    assert list(report.statuses(matrix)) == ["COMPLIANT_WITH_WARNINGS", "INCONCLUSIVE"]
    empty = report.reason_matrix(index, [])
    assert report.reason_codes_text(empty).isna().all()
    assert (report.statuses(empty) == "COMPLIANT").all()


def test_off_grid_departure_does_not_matter_with_a_logger(app_config, raw_one) -> None:
    raw_one[TableName.SHIPMENTS]["actual_departure_at"] = _ts("2026-03-10 08:03")
    assert _decision(app_config, raw_one)["status"] == "COMPLIANT"
