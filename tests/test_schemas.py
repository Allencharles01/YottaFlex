from datetime import date

import pandas as pd
import pytest

from coldchain import schemas
from coldchain.enums import Layer, TableName
from coldchain.errors import SchemaValidationError
from conftest import LOG, SHP, _str, _ts

ALL_TABLES = list(TableName)
LABEL_COLUMNS = {"scenario", "issue_type", "anomaly_id"}


def _all_columns(role: str | None = None, *, required: bool | None = None) -> list:
    params = []
    for table in ALL_TABLES:
        for name, col in schemas.SCHEMAS[table].columns.items():
            if role is not None and col.metadata.get("role") != role:
                continue
            if required is not None and col.nullable == required:
                continue
            params.append(pytest.param(table, name, id=f"{table}.{name}"))
    return params


def assert_fails(table: TableName, frame: pd.DataFrame, check: str | None = None) -> None:
    with pytest.raises(SchemaValidationError) as info:
        schemas.validate(table, frame)
    if check is not None:
        assert check in str(info.value)


# ---------------------------------------------------------------- registry / contract


def test_every_table_has_schema_layer_and_primary_key() -> None:
    assert set(schemas.SCHEMAS) == set(TableName)
    assert set(schemas.TABLE_LAYERS) == set(TableName)
    assert set(schemas.PRIMARY_KEYS) == set(TableName)
    for table, key in schemas.PRIMARY_KEYS.items():
        assert set(key) <= set(schemas.SCHEMAS[table].columns), table


def test_foreign_keys_reference_existing_unique_columns() -> None:
    for child, child_col, parent, parent_col in schemas.FOREIGN_KEYS:
        assert child_col in schemas.SCHEMAS[child].columns, (child, child_col)
        assert schemas.PRIMARY_KEYS[parent] == (parent_col,), (parent, parent_col)


def test_raw_tables_carry_no_ground_truth_labels() -> None:
    for table, layer in schemas.TABLE_LAYERS.items():
        if layer is Layer.RAW:
            columns = set(schemas.SCHEMAS[table].columns)
            assert not columns & LABEL_COLUMNS, table
            assert not [c for c in columns if c.startswith("expected_")], table


def test_status_partition_covers_all_statuses() -> None:
    assert not schemas.GENERATOR_STATUSES & schemas.ENGINE_STATUSES
    assert len(schemas.GENERATOR_STATUSES | schemas.ENGINE_STATUSES) == 8


# ---------------------------------------------------------------- generic accept / reject


@pytest.mark.parametrize("table", ALL_TABLES)
def test_valid_sample_passes(valid_tables, table) -> None:
    frame = valid_tables[table]
    pd.testing.assert_frame_equal(schemas.validate(table, frame), frame)


@pytest.mark.parametrize("table", ALL_TABLES)
def test_empty_table_passes(valid_tables, table) -> None:
    schemas.validate(table, valid_tables[table].iloc[0:0])


@pytest.mark.parametrize(("table", "column"), _all_columns("enum"))
def test_bad_enum_rejected(valid_tables, table, column) -> None:
    frame = valid_tables[table]
    frame.loc[frame.index[0], column] = "NOT_AN_ALLOWED_VALUE"
    assert_fails(table, frame, column)


@pytest.mark.parametrize(("table", "column"), _all_columns("id"))
def test_malformed_id_rejected(valid_tables, table, column) -> None:
    frame = valid_tables[table]
    frame.loc[frame.index[0], column] = "XYZ-1"
    assert_fails(table, frame, column)


@pytest.mark.parametrize(("table", "column"), _all_columns(required=True))
def test_null_in_required_field_rejected(valid_tables, table, column) -> None:
    frame = valid_tables[table]
    if pd.api.types.is_integer_dtype(frame[column]):
        frame[column] = frame[column].astype("Int64")  # an int64 column cannot hold a null
    frame.loc[frame.index[0], column] = None
    assert_fails(table, frame, column)


@pytest.mark.parametrize("table", ALL_TABLES)
def test_extra_column_rejected(valid_tables, table) -> None:
    assert_fails(table, valid_tables[table].assign(unexpected_extra="x"))


def test_label_column_cannot_leak_into_raw_table(valid_tables) -> None:
    frame = valid_tables[TableName.SHIPMENTS].assign(scenario="NORMAL")
    assert_fails(TableName.SHIPMENTS, frame, "scenario")


@pytest.mark.parametrize("table", ALL_TABLES)
def test_duplicate_primary_key_rejected(valid_tables, table) -> None:
    frame = valid_tables[table]
    assert_fails(table, pd.concat([frame, frame.iloc[[0]]], ignore_index=True))


def test_reordered_columns_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.PARTIES]
    assert_fails(TableName.PARTIES, frame[list(reversed(frame.columns))])


def test_unknown_table_rejected(valid_tables) -> None:
    with pytest.raises(SchemaValidationError, match="Unknown table"):
        schemas.validate("not_a_table", valid_tables[TableName.PARTIES])


def test_error_message_names_table_and_check(valid_tables) -> None:
    frame = valid_tables[TableName.LOTS]
    frame["expires_on"] = frame["manufactured_on"]
    with pytest.raises(SchemaValidationError) as info:
        schemas.validate(TableName.LOTS, frame)
    assert "'lots'" in str(info.value)
    assert "<table>: expires_on_after_manufactured_on (1 rows" in str(info.value)


# ---------------------------------------------------------------- column types


def test_naive_timestamps_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.LOGGERS]
    frame["assigned_at"] = frame["assigned_at"].dt.tz_localize(None)
    assert_fails(TableName.LOGGERS, frame, "assigned_at")


def test_sub_minute_timestamps_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.LOGGERS]
    frame["assigned_at"] = frame["assigned_at"] + pd.Timedelta(seconds=30)
    assert_fails(TableName.LOGGERS, frame, "whole_minute_timestamp")


@pytest.mark.parametrize(("value", "ok"), [(8.0, True), (8.25, True), (8.257, False)])
def test_temperatures_rounded_to_two_decimals(valid_tables, value, ok) -> None:
    frame = valid_tables[TableName.TEMPERATURE_READINGS]
    frame.loc[0, "temperature_c"] = value
    if ok:
        schemas.validate(TableName.TEMPERATURE_READINGS, frame)
    else:
        assert_fails(TableName.TEMPERATURE_READINGS, frame, "rounded_to_2_decimals")


@pytest.mark.parametrize(("value", "ok"), [(-100.0, True), (100.0, True), (100.01, False)])
def test_temperature_sensor_range(valid_tables, value, ok) -> None:
    frame = valid_tables[TableName.TEMPERATURE_READINGS]
    frame.loc[0, "temperature_c"] = value
    if ok:
        schemas.validate(TableName.TEMPERATURE_READINGS, frame)
    else:
        assert_fails(TableName.TEMPERATURE_READINGS, frame, "temperature_c")


def test_text_with_surrounding_whitespace_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.PARTIES]
    frame.loc[0, "name"] = " Padded Name "
    assert_fails(TableName.PARTIES, frame, "no_surrounding_whitespace")


# ---------------------------------------------------------------- threshold policies


def _policies(valid_tables, **changes) -> pd.DataFrame:
    frame = valid_tables[TableName.THRESHOLD_POLICIES]
    for column, values in changes.items():
        frame[column] = pd.Series(values, dtype=frame[column].dtype)
    return frame


def test_adjacent_half_open_versions_accepted(valid_tables) -> None:
    # V1 ends 2026-07-01 (exclusive) and V2 starts 2026-07-01: no overlap.
    schemas.validate(TableName.THRESHOLD_POLICIES, valid_tables[TableName.THRESHOLD_POLICIES])


def test_overlapping_policy_versions_rejected(valid_tables) -> None:
    frame = _policies(valid_tables, effective_to=[date(2026, 7, 2), None])
    assert_fails(TableName.THRESHOLD_POLICIES, frame, "no_overlapping_versions")


def test_open_ended_version_followed_by_another_rejected(valid_tables) -> None:
    frame = _policies(valid_tables, effective_to=[None, None])
    assert_fails(TableName.THRESHOLD_POLICIES, frame, "no_overlapping_versions")


def test_overlap_detected_against_any_earlier_version() -> None:
    frame = pd.DataFrame(
        {
            "policy_code": ["A", "A", "A", "B"],
            "effective_from": [
                date(2026, 1, 1),
                date(2026, 3, 1),
                date(2026, 5, 1),
                date(2026, 5, 1),
            ],
            # V1 runs to Dec, so V3 overlaps V1 even though it does not overlap V2.
            "effective_to": [date(2026, 12, 1), date(2026, 4, 1), None, None],
        }
    )
    assert schemas.overlapping_policy_versions(frame).tolist() == [False, True, True, False]


def test_overlap_helper_handles_empty_frame() -> None:
    frame = pd.DataFrame(columns=["policy_code", "effective_from", "effective_to"])
    assert schemas.overlapping_policy_versions(frame).empty


def test_versions_must_increase_with_effective_date(valid_tables) -> None:
    frame = _policies(
        valid_tables,
        effective_from=[date(2026, 7, 1), date(2026, 1, 1)],
        effective_to=[None, date(2026, 7, 1)],
    )
    assert_fails(TableName.THRESHOLD_POLICIES, frame, "versions_increase_with_effective_from")


def test_policy_id_must_match_code_and_version(valid_tables) -> None:
    frame = _policies(valid_tables, policy_id=["THR-REFRIG_2_8-V1", "THR-REFRIG_2_8-V3"])
    assert_fails(TableName.THRESHOLD_POLICIES, frame, "policy_id_matches_code_and_version")


@pytest.mark.parametrize(
    ("column", "value"),
    [("abs_min_c", 2.0), ("min_c", 8.0), ("abs_max_c", 8.0), ("max_cumulative_excursion_min", 10)],
)
def test_policy_limit_ordering(valid_tables, column, value) -> None:
    frame = valid_tables[TableName.THRESHOLD_POLICIES]
    frame.loc[0, column] = value
    assert_fails(TableName.THRESHOLD_POLICIES, frame)


def test_effective_to_must_follow_effective_from(valid_tables) -> None:
    frame = _policies(valid_tables, effective_to=[date(2026, 1, 1), None])
    assert_fails(TableName.THRESHOLD_POLICIES, frame, "effective_to_after_effective_from")


# ---------------------------------------------------------------- master data rules


def test_lot_and_device_date_ordering(valid_tables) -> None:
    devices = valid_tables[TableName.DEVICES]
    devices["calibration_due_on"] = devices["calibrated_on"]
    assert_fails(TableName.DEVICES, devices, "calibration_due_on_after_calibrated_on")


def test_device_serial_numbers_unique(valid_tables) -> None:
    devices = valid_tables[TableName.DEVICES]
    second = devices.assign(device_id="DEV-000002")
    assert_fails(
        TableName.DEVICES, pd.concat([devices, second], ignore_index=True), "serial_number"
    )


@pytest.mark.parametrize("prefix", ["planned", "actual"])
def test_shipment_arrival_must_be_after_departure(valid_tables, prefix) -> None:
    frame = valid_tables[TableName.SHIPMENTS]
    frame[f"{prefix}_arrival_at"] = frame[f"{prefix}_departure_at"]  # equal → rejected (strict)
    assert_fails(TableName.SHIPMENTS, frame, f"{prefix}_arrival_at_after_{prefix}_departure_at")


def test_shipment_parties_distinct(valid_tables) -> None:
    frame = valid_tables[TableName.SHIPMENTS]
    frame["receiver_party_id"] = frame["carrier_party_id"]
    assert_fails(TableName.SHIPMENTS, frame, "parties_distinct")


def test_logger_one_per_shipment_and_id_matches(valid_tables) -> None:
    frame = valid_tables[TableName.LOGGERS]
    frame["logger_id"] = "LOG-SHP-2026-000002-01"
    assert_fails(TableName.LOGGERS, frame, "logger_id_matches_shipment")
    twice = valid_tables[TableName.LOGGERS]
    twice = pd.concat([twice, twice.assign(logger_id=f"LOG-{SHP}-02")], ignore_index=True)
    assert_fails(TableName.LOGGERS, twice, "shipment_id")


def test_logger_window_must_be_positive(valid_tables) -> None:
    frame = valid_tables[TableName.LOGGERS]
    frame["unassigned_at"] = frame["assigned_at"]
    assert_fails(TableName.LOGGERS, frame, "unassigned_at_after_assigned_at")


# ---------------------------------------------------------------- readings


def test_reading_id_must_match_logger_and_time(valid_tables) -> None:
    frame = valid_tables[TableName.TEMPERATURE_READINGS]
    frame.loc[0, "recorded_at"] = frame.loc[0, "recorded_at"] + pd.Timedelta(minutes=5)
    assert_fails(TableName.TEMPERATURE_READINGS, frame, "reading_id_matches_logger_and_recorded_at")


def test_reading_from_other_device_is_structurally_valid(valid_tables) -> None:
    # logger_not_assigned anomalies look like this; the engine (not the schema) flags them.
    frame = valid_tables[TableName.TEMPERATURE_READINGS]
    frame.loc[0, "device_id"] = "DEV-000999"
    schemas.validate(TableName.TEMPERATURE_READINGS, frame)


# ---------------------------------------------------------------- route stops / custody / status


def test_route_stop_timestamps_by_type(valid_tables) -> None:
    frame = valid_tables[TableName.ROUTE_STOPS]
    frame.loc[0, "arrived_at"] = frame.loc[0, "departed_at"]
    assert_fails(TableName.ROUTE_STOPS, frame, "timestamps_present_by_stop_type")


@pytest.mark.parametrize(("dwell", "ok"), [(30, True), (31, False)])
def test_dwell_min_derived_from_timestamps(valid_tables, dwell, ok) -> None:
    frame = valid_tables[TableName.ROUTE_STOPS]
    frame.loc[1, "dwell_min"] = dwell
    if ok:
        schemas.validate(TableName.ROUTE_STOPS, frame)
    else:
        assert_fails(TableName.ROUTE_STOPS, frame, "dwell_min_matches_timestamps")


def test_dwell_must_be_null_without_both_timestamps(valid_tables) -> None:
    frame = valid_tables[TableName.ROUTE_STOPS]
    frame.loc[0, "dwell_min"] = 0
    assert_fails(TableName.ROUTE_STOPS, frame, "dwell_min_matches_timestamps")


def test_route_stop_id_must_match_sequence(valid_tables) -> None:
    frame = valid_tables[TableName.ROUTE_STOPS]
    frame.loc[1, "sequence"] = 5
    assert_fails(TableName.ROUTE_STOPS, frame, "stop_id_matches_shipment_and_sequence")


def test_custody_stop_must_belong_to_shipment(valid_tables) -> None:
    frame = valid_tables[TableName.CUSTODY_EVENTS]
    frame.loc[0, "stop_id"] = "RST-SHP-2026-000002-01"
    assert_fails(TableName.CUSTODY_EVENTS, frame, "stop_id_belongs_to_shipment")


def test_custody_self_handoff_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.CUSTODY_EVENTS]
    frame.loc[0, "to_party_id"] = frame.loc[0, "from_party_id"]
    assert_fails(TableName.CUSTODY_EVENTS, frame, "from_party_differs_from_to_party")


def test_deleted_custody_event_is_structurally_valid(valid_tables) -> None:
    # custody_gap_not_flagged anomalies look like this; the engine flags CUSTODY_GAP.
    frame = valid_tables[TableName.CUSTODY_EVENTS].drop(index=2)
    schemas.validate(TableName.CUSTODY_EVENTS, frame)


def test_raw_status_events_cannot_hold_engine_statuses(valid_tables) -> None:
    frame = valid_tables[TableName.SHIPMENT_STATUS_EVENTS]
    frame.loc[5, "status"] = "RELEASED"
    assert_fails(TableName.SHIPMENT_STATUS_EVENTS, frame, "allowed_status_for_layer")


def test_engine_status_events_cannot_hold_generator_statuses(valid_tables) -> None:
    frame = valid_tables[TableName.COMPLIANCE_STATUS_EVENTS]
    frame.loc[0, "status"] = "DELIVERED"
    assert_fails(TableName.COMPLIANCE_STATUS_EVENTS, frame, "allowed_status_for_layer")


# ---------------------------------------------------------------- excursion reports


def test_excursion_first_reading_must_match_start(valid_tables) -> None:
    frame = valid_tables[TableName.EXCURSION_REPORTS]
    frame["first_reading_id"] = f"TEMP-{LOG}-20260310T0900"
    assert_fails(
        TableName.EXCURSION_REPORTS, frame, "first_reading_id_matches_logger_and_started_at"
    )


def test_excursion_duration_must_include_one_interval(valid_tables) -> None:
    frame = valid_tables[TableName.EXCURSION_REPORTS]
    frame["duration_min"] = 10  # = ended - started, missing the + reading_interval_min term
    assert_fails(TableName.EXCURSION_REPORTS, frame, "duration_exceeds_window")


def test_single_reading_excursion(valid_tables) -> None:
    frame = valid_tables[TableName.EXCURSION_REPORTS]
    frame["ended_at"] = frame["started_at"]
    frame["last_reading_id"] = frame["first_reading_id"]
    frame["duration_min"] = 10
    assert_fails(TableName.EXCURSION_REPORTS, frame, "single_reading_iff_zero_length_window")
    frame["reading_count"] = 1
    schemas.validate(TableName.EXCURSION_REPORTS, frame)


def test_excursion_logger_must_belong_to_shipment(valid_tables) -> None:
    frame = valid_tables[TableName.EXCURSION_REPORTS]
    frame["shipment_id"] = "SHP-2026-000002"
    assert_fails(TableName.EXCURSION_REPORTS, frame, "logger_id_belongs_to_shipment")


def test_reading_in_two_reports_rejected(valid_tables) -> None:
    frame = valid_tables[TableName.EXCURSION_REPORT_READINGS]
    extra = frame.iloc[[0]].assign(report_id="EXCR-2026-000002")
    assert_fails(
        TableName.EXCURSION_REPORT_READINGS,
        pd.concat([frame, extra], ignore_index=True),
        "reading_id",
    )


# ---------------------------------------------------------------- compliance decisions


def _decision(valid_tables, **changes) -> pd.DataFrame:
    frame = valid_tables[TableName.COMPLIANCE_DECISIONS]
    for column, value in changes.items():
        frame[column] = pd.Series([value], dtype=frame[column].dtype)
    return frame


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"decided_action": "QUARANTINE"}, "decided_action_matches_status"),
        ({"reason_codes": "DATA_GAP"}, "status_matches_reason_codes"),
        ({"reason_codes": "NEAR_LIMIT_UNCERTAIN|EXCURSION_WITHIN_ALLOWANCE"}, "reason_codes"),
        ({"reason_codes": "LONG_DWELL|LONG_DWELL"}, "reason_codes"),
        ({"reason_codes": "LONG_DWELL|MADE_UP"}, "reason_codes"),
        ({"coverage_pct": 96.78}, "coverage_pct_matches_counts"),
        ({"reading_count": 32, "coverage_pct": 103.23}, "coverage_pct"),
        ({"total_excursion_min": 0}, "excursion_report_ids_iff_excursion_minutes"),
        ({"excursion_report_ids": "EXCR-2026-000001|EXCR-2026-000001"}, "excursion_report_ids"),
        ({"policy_source": "PRODUCT_OVERRIDE"}, "policy_source_matches_mismatch_flag"),
    ],
)
def test_inconsistent_decisions_rejected(valid_tables, changes, check) -> None:
    assert_fails(TableName.COMPLIANCE_DECISIONS, _decision(valid_tables, **changes), check)


def test_compliant_decision_without_reasons(valid_tables) -> None:
    frame = _decision(
        valid_tables,
        status="COMPLIANT",
        reason_codes=None,
        total_excursion_min=0,
        excursion_report_ids=None,
    )
    schemas.validate(TableName.COMPLIANCE_DECISIONS, frame)


def test_carrier_contradiction_is_informational(valid_tables) -> None:
    frame = _decision(
        valid_tables,
        status="COMPLIANT",
        reason_codes="CARRIER_STATUS_CONTRADICTED",
        total_excursion_min=0,
        excursion_report_ids=None,
    )
    schemas.validate(TableName.COMPLIANCE_DECISIONS, frame)


def test_policy_override_with_mismatch_flag(valid_tables) -> None:
    frame = _decision(
        valid_tables,
        policy_source="PRODUCT_OVERRIDE",
        reason_codes="EXCURSION_WITHIN_ALLOWANCE|POLICY_MISMATCH_DECLARED",
    )
    schemas.validate(TableName.COMPLIANCE_DECISIONS, frame)


def test_most_severe_reason_wins(valid_tables) -> None:
    frame = _decision(
        valid_tables,
        status="NON_COMPLIANT",
        decided_action="QUARANTINE",
        reason_codes="SINGLE_DURATION_BREACH|DATA_GAP|LONG_DWELL|CARRIER_STATUS_CONTRADICTED",
    )
    schemas.validate(TableName.COMPLIANCE_DECISIONS, frame)


# ---------------------------------------------------------------- ground truth


def _outcome(valid_tables, **changes) -> pd.DataFrame:
    frame = valid_tables[TableName.EXPECTED_OUTCOMES]
    for column, value in changes.items():
        frame[column] = pd.Series([value], dtype=frame[column].dtype)
    return frame


@pytest.mark.parametrize(
    ("changes", "check"),
    [
        ({"expected_excursion_count": 2}, "excursion_counts_and_total_consistent"),
        ({"expected_total_excursion_min": 30}, "excursion_counts_and_total_consistent"),
        ({"expected_excursion_durations_min": "20|"}, "list_format"),
        ({"expected_excursion_windows": "2026-03-10 09:10/2026-03-10 09:20"}, "list_format"),
        (
            {"expected_excursion_windows": "2026-03-10T09:20:00Z/2026-03-10T09:10:00Z"},
            "excursion_windows_ordered",
        ),
        ({"expected_action": "REVIEW"}, "expected_action_matches_expected_status"),
    ],
)
def test_inconsistent_expected_outcomes_rejected(valid_tables, changes, check) -> None:
    assert_fails(TableName.EXPECTED_OUTCOMES, _outcome(valid_tables, **changes), check)


def test_expected_outcome_with_two_excursions(valid_tables) -> None:
    frame = _outcome(
        valid_tables,
        expected_excursion_count=2,
        expected_total_excursion_min=50,
        expected_excursion_windows=(
            "2026-03-10T09:10:00Z/2026-03-10T09:20:00Z|2026-03-10T10:00:00Z/2026-03-10T10:20:00Z"
        ),
        expected_excursion_durations_min="20|30",
    )
    schemas.validate(TableName.EXPECTED_OUTCOMES, frame)


def test_anomaly_spanning_two_shipments(valid_tables) -> None:
    frame = valid_tables[TableName.ANOMALIES]
    second = frame.assign(shipment_id="SHP-2026-000002")
    schemas.validate(TableName.ANOMALIES, pd.concat([frame, second], ignore_index=True))


def test_anomaly_affected_ids_must_be_valid(valid_tables) -> None:
    frame = valid_tables[TableName.ANOMALIES]
    frame["affected_record_ids"] = _str("CUS-SHP-2026-000001-02|not-an-id")
    assert_fails(TableName.ANOMALIES, frame, "affected_record_ids")


def test_split_list() -> None:
    assert schemas.split_list(None) == []
    assert schemas.split_list(float("nan")) == []
    assert schemas.split_list("") == []
    assert schemas.split_list("A") == ["A"]
    assert schemas.split_list("A|B") == ["A", "B"]


def test_timestamp_helper_builds_project_dtype() -> None:
    assert _ts("2026-01-01 00:00").dtype == schemas.TIMESTAMP_DTYPE
