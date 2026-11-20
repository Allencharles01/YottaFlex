"""Validation checks on tiny hand-built tables (Phase 6).

The base is the valid one-shipment sample (``conftest.build_raw_one_shipment``: a complete
10-minute stream, one hub) plus the engine's output for it. Every test breaks one thing and
asserts that the matching check reports exactly the broken record.
"""

import pandas as pd
import pytest

from coldchain.compliance import check_tables
from coldchain.enums import CheckCategory, CheckStatus, CheckTier, TableName
from coldchain.errors import ValidationError
from coldchain.schemas import FOREIGN_KEYS
from coldchain.validation import COMPLIANCE_INPUTS, RAW_INPUTS, validate_tables
from coldchain.validation.checks import (
    BUSINESS_RULE_CHECKS,
    CHECKS,
    RULE_ASSIGNED_DEVICE,
    RULE_CARRIER_CLAIM,
    RULE_COMPLETE_STREAM,
    RULE_CUSTODY_CHAIN,
    RULE_CUSTODY_COVERAGE,
    RULE_DECLARED_POLICY,
    RULE_DEVICE_OVERLAP,
    RULE_HAS_LOGGER,
    VALIDATED_TABLES,
    Check,
    Outcome,
)
from conftest import (
    LOG,
    LOG2,
    SHP,
    SHP2,
    add_back_to_back_shipment,
    build_raw_one_shipment,
    drop_readings,
    set_temperatures,
)

T = TableName


def _raw() -> dict[TableName, pd.DataFrame]:
    """The sample's raw layer, with its PLANNED event inside the 24-336 h planning lead."""
    raw = build_raw_one_shipment()
    events = raw[T.SHIPMENT_STATUS_EVENTS]
    events.loc[events["status"] == "PLANNED", "event_at"] = pd.Timestamp("2026-03-09 08:00Z")
    return raw


def _with_engine(app_config, raw: dict[TableName, pd.DataFrame]) -> dict[TableName, pd.DataFrame]:
    return {**raw, **check_tables(app_config, raw)}


def _run(app_config, reference, tables, *only: str):
    """Validate; with ``only``, run just those checks (integrity tests stay fast)."""
    checks = tuple(c for c in CHECKS if c.check_id in only) if only else CHECKS
    assert len(checks) == (len(only) or len(CHECKS)), only
    return validate_tables(app_config, reference, tables, checks=checks)


def _failures(result) -> dict[str, list[str]]:
    """check_id → failing record IDs (FAIL and ERROR checks only)."""
    found = result.findings.groupby("check_id")["record_id"].agg(sorted).to_dict()
    for r in result.results:
        if r.status is CheckStatus.ERROR:
            found[r.check.check_id] = ["<error>"]
    return found


@pytest.fixture
def clean(app_config) -> dict[TableName, pd.DataFrame]:
    """A clean one-shipment input: raw tables plus the engine's output."""
    return _with_engine(app_config, _raw())


# --------------------------------------------------------------------------------------------
# Registry and runner
# --------------------------------------------------------------------------------------------


def test_check_ids_are_unique_and_sorted_integrity_first() -> None:
    ids = [c.check_id for c in CHECKS]
    assert len(ids) == len(set(ids))
    tiers = [c.tier for c in CHECKS]
    assert tiers == sorted(tiers, key=lambda t: t is CheckTier.BUSINESS_RULE)


def test_every_validated_table_has_schema_and_primary_key_checks() -> None:
    ids = {c.check_id for c in CHECKS}
    for table in VALIDATED_TABLES:
        assert f"schema.{table.value}" in ids
        assert f"pk.{table.value}" in ids
    assert set(VALIDATED_TABLES) == set(RAW_INPUTS) | set(COMPLIANCE_INPUTS)


def test_every_foreign_key_between_validated_tables_has_a_check() -> None:
    ids = {c.check_id for c in CHECKS}
    for child, column, parent, _ in FOREIGN_KEYS:
        if child in VALIDATED_TABLES and parent in VALIDATED_TABLES:
            assert f"fk.{child.value}.{column}" in ids


def test_business_rule_constants_are_registered_as_business_rules() -> None:
    constants = {
        RULE_ASSIGNED_DEVICE,
        RULE_CARRIER_CLAIM,
        RULE_COMPLETE_STREAM,
        RULE_CUSTODY_CHAIN,
        RULE_CUSTODY_COVERAGE,
        RULE_DECLARED_POLICY,
        RULE_DEVICE_OVERLAP,
        RULE_HAS_LOGGER,
    }
    assert constants == BUSINESS_RULE_CHECKS


def test_every_check_needs_its_own_table_and_only_validated_tables() -> None:
    for check in CHECKS:
        assert check.table in check.needs, check.check_id
        assert set(check.needs) <= set(VALIDATED_TABLES), check.check_id


def test_clean_sample_passes_every_check(app_config, reference, clean) -> None:
    result = _run(app_config, reference, clean)
    assert _failures(result) == {}
    assert result.count(CheckStatus.PASS) == len(CHECKS)
    assert result.passed


def test_raw_only_input_skips_the_engine_output_checks(app_config, reference) -> None:
    result = _run(app_config, reference, _raw())
    skipped = [r for r in result.results if r.status is CheckStatus.SKIPPED]
    assert skipped and result.findings.empty and result.passed
    for r in skipped:
        assert set(r.check.needs) & set(COMPLIANCE_INPUTS), r.check.check_id


def test_a_check_that_crashes_is_an_integrity_error(app_config, reference, clean) -> None:
    def boom(ctx) -> Outcome:
        raise ValueError("bad data")

    check = Check(
        "custom.boom",
        CheckCategory.CONSISTENCY,
        CheckTier.INTEGRITY,
        T.SHIPMENTS,
        (T.SHIPMENTS,),
        "x",
        boom,
    )
    result = validate_tables(app_config, reference, clean, checks=(check,))
    assert result.results[0].status is CheckStatus.ERROR
    assert "bad data" in result.results[0].message
    assert not result.passed


def test_missing_raw_table_is_rejected(app_config, reference, clean) -> None:
    del clean[T.LOTS]
    with pytest.raises(ValidationError, match="lots"):
        _run(app_config, reference, clean)


def test_partial_engine_output_is_rejected(app_config, reference, clean) -> None:
    del clean[T.EXCURSION_REPORTS]
    with pytest.raises(ValidationError, match="all compliance tables or none"):
        _run(app_config, reference, clean)


def test_answer_key_tables_are_rejected(app_config, reference, clean, valid_tables) -> None:
    clean[T.EXPECTED_OUTCOMES] = valid_tables[T.EXPECTED_OUTCOMES]
    with pytest.raises(ValidationError, match="raw and compliance tables only"):
        _run(app_config, reference, clean)


# --------------------------------------------------------------------------------------------
# Integrity checks: each broken toy table is caught by its check
# --------------------------------------------------------------------------------------------


def test_duplicate_primary_key(app_config, reference, clean) -> None:
    readings = clean[T.TEMPERATURE_READINGS]
    clean[T.TEMPERATURE_READINGS] = pd.concat([readings, readings.iloc[[5]]], ignore_index=True)
    found = _failures(
        _run(
            app_config,
            reference,
            clean,
            "pk.temperature_readings",
            "dup.temperature_readings.logger_id_recorded_at",
        )
    )
    reading = readings["reading_id"].iloc[5]
    assert found["pk.temperature_readings"] == [reading, reading]
    assert found["dup.temperature_readings.logger_id_recorded_at"] == [reading, reading]


def test_null_primary_key(app_config, reference, clean) -> None:
    clean[T.PARTIES].loc[6, "party_id"] = None
    found = _failures(_run(app_config, reference, clean, "pk.parties", "schema.parties"))
    assert "pk.parties" in found and "schema.parties" in found


def test_foreign_key_uses_a_duckdb_anti_join(app_config, reference, clean) -> None:
    clean[T.LOGGERS]["device_id"] = "DEV-000999"
    found = _failures(_run(app_config, reference, clean, "fk.loggers.device_id"))
    assert found["fk.loggers.device_id"] == [LOG]


def test_bad_enum_value(app_config, reference, clean) -> None:
    clean[T.SHIPMENTS]["carrier_reported_status"] = "MAYBE"
    found = _failures(_run(app_config, reference, clean, "enum.shipments", "schema.shipments"))
    assert found["enum.shipments"] == [SHP]
    assert found["schema.shipments"] == [SHP]


def test_unknown_reason_code_in_a_list(app_config, reference, clean) -> None:
    clean[T.COMPLIANCE_DECISIONS]["reason_codes"] = "NOT_A_CODE"
    found = _failures(_run(app_config, reference, clean, "enum.compliance_decisions"))
    assert found["enum.compliance_decisions"] == [SHP]


def test_schema_violation_becomes_a_finding(app_config, reference, clean) -> None:
    clean[T.TEMPERATURE_READINGS].loc[3, "temperature_c"] = 5.123
    found = _failures(_run(app_config, reference, clean, "schema.temperature_readings"))
    assert found["schema.temperature_readings"] == [clean[T.TEMPERATURE_READINGS]["reading_id"][3]]


def test_duplicate_natural_key(app_config, reference, clean) -> None:
    devices = clean[T.DEVICES]
    twin = devices.assign(device_id="DEV-000002")
    clean[T.DEVICES] = pd.concat([devices, twin], ignore_index=True)
    found = _failures(_run(app_config, reference, clean, "dup.devices.serial_number"))
    assert found["dup.devices.serial_number"] == ["DEV-000001", "DEV-000002"]


def test_logger_assigned_after_departure(app_config, reference) -> None:
    raw = _raw()
    raw[T.LOGGERS]["assigned_at"] = pd.Timestamp("2026-03-10 08:10Z")
    drop_readings(raw, "0730", "0740", "0750", "0800")
    found = _failures(
        _run(app_config, reference, _with_engine(app_config, raw), "dates.loggers.window")
    )
    assert found["dates.loggers.window"] == [LOG]


def test_reading_off_the_grid_or_outside_the_window(app_config, reference, clean) -> None:
    readings = clean[T.TEMPERATURE_READINGS]
    late = readings.iloc[[-1]].assign(
        recorded_at=pd.Timestamp("2026-03-10 12:45Z"),
        reading_id=f"TEMP-{LOG}-20260310T1245",
    )
    clean[T.TEMPERATURE_READINGS] = pd.concat([readings, late], ignore_index=True)
    found = _failures(_run(app_config, reference, clean, "dates.temperature_readings.window_grid"))
    assert found["dates.temperature_readings.window_grid"] == [f"TEMP-{LOG}-20260310T1245"]


def test_route_stops_out_of_order(app_config, reference, clean) -> None:
    stops = clean[T.ROUTE_STOPS]
    stops.loc[1, ["arrived_at", "departed_at"]] = [
        pd.Timestamp("2026-03-10 07:00Z"),
        pd.Timestamp("2026-03-10 07:30Z"),
    ]
    found = _failures(_run(app_config, reference, clean, "dates.route_stops.chronological"))
    assert found["dates.route_stops.chronological"] == [f"RST-{SHP}-02"]


def test_dwell_does_not_reconcile(app_config, reference, clean) -> None:
    clean[T.ROUTE_STOPS].loc[1, "dwell_min"] = 31
    found = _failures(_run(app_config, reference, clean, "recon.route_stops.dwell_min"))
    assert found["recon.route_stops.dwell_min"] == [f"RST-{SHP}-02"]


def test_lot_over_shipped(app_config, reference, clean) -> None:
    clean[T.SHIPMENT_LOTS]["quantity_units"] = 5001
    found = _failures(_run(app_config, reference, clean, "recon.lots.quantity"))
    assert found["recon.lots.quantity"] == ["LOT-2026-000001"]


def test_lot_expiry_must_match_shelf_life(app_config, reference, clean) -> None:
    clean[T.PRODUCTS]["shelf_life_days"] = 700
    found = _failures(_run(app_config, reference, clean, "recon.lots.shelf_life"))
    assert found["recon.lots.shelf_life"] == ["LOT-2026-000001"]


def test_illegal_status_transition(app_config, reference, clean) -> None:
    events = clean[T.SHIPMENT_STATUS_EVENTS]
    events.loc[events["sequence"] == 2, "status"] = "AT_STOP"
    found = _failures(
        _run(app_config, reference, clean, "lifecycle.shipment_status_events.transitions")
    )
    assert f"STE-{SHP}-02" in found["lifecycle.shipment_status_events.transitions"]


def test_engine_history_must_match_the_action(app_config, reference, clean) -> None:
    events = clean[T.COMPLIANCE_STATUS_EVENTS]
    clean[T.COMPLIANCE_STATUS_EVENTS] = events[events["status"] != "RELEASED"]
    found = _failures(
        _run(app_config, reference, clean, "lifecycle.compliance_status_events.transitions")
    )
    assert found["lifecycle.compliance_status_events.transitions"] == [f"STE-{SHP}-07"]


def test_tampered_excursion_duration_is_recomputed_from_the_readings(app_config, reference) -> None:
    raw = _raw()
    set_temperatures(raw, {"0900": 8.5, "0910": 8.6, "0920": 8.7})
    tables = _with_engine(app_config, raw)
    report = tables[T.EXCURSION_REPORTS]
    assert report["duration_min"].tolist() == [30]
    report["duration_min"] = 40
    tables[T.COMPLIANCE_DECISIONS]["total_excursion_min"] = 40  # keep the decision consistent
    result = _run(app_config, reference, tables, "recon.excursion_reports.from_readings")
    detail = result.findings.set_index("check_id").loc[
        "recon.excursion_reports.from_readings", "detail"
    ]
    assert detail == "duration_min does not reconcile"


def test_unreported_excursion_is_found_from_the_readings(app_config, reference, clean) -> None:
    readings = clean[T.TEMPERATURE_READINGS]
    readings.loc[readings["recorded_at"].dt.strftime("%H%M") == "1100", "temperature_c"] = 9.5
    found = _failures(
        _run(
            app_config,
            reference,
            clean,
            "recon.excursion_reports.from_readings",
            "recon.excursion_report_readings.links",
        )
    )
    reading = f"TEMP-{LOG}-20260310T1100"
    assert found["recon.excursion_reports.from_readings"] == [reading]
    assert found["recon.excursion_report_readings.links"] == [reading]


def test_tampered_coverage_and_gap(app_config, reference, clean) -> None:
    decisions = clean[T.COMPLIANCE_DECISIONS]
    decisions["max_gap_min"] = 20
    found = _failures(_run(app_config, reference, clean, "recon.compliance_decisions.coverage"))
    assert found["recon.compliance_decisions.coverage"] == [SHP]


def test_reason_code_without_evidence(app_config, reference, clean) -> None:
    clean[T.COMPLIANCE_DECISIONS]["reason_codes"] = "LONG_DWELL"
    clean[T.COMPLIANCE_DECISIONS]["status"] = "COMPLIANT_WITH_WARNINGS"
    found = _failures(
        _run(app_config, reference, clean, "recon.compliance_decisions.evidence_codes")
    )
    assert found["recon.compliance_decisions.evidence_codes"] == [SHP]


def test_party_with_wrong_role(app_config, reference, clean) -> None:
    clean[T.SHIPMENTS]["carrier_party_id"] = "PTY-0003"  # a WAREHOUSE party
    found = _failures(_run(app_config, reference, clean, "fk.shipments.party_roles"))
    assert found["fk.shipments.party_roles"] == [SHP]


def test_party_used_before_it_was_active(app_config, reference, clean) -> None:
    from datetime import date

    clean[T.PARTIES].loc[3, "active_from"] = date(2026, 6, 1)
    found = _failures(_run(app_config, reference, clean, "dates.parties.active_from"))
    assert found["dates.parties.active_from"] == ["PTY-0004"]


# --------------------------------------------------------------------------------------------
# Business rules: each injected-style defect is a business finding, never an integrity one
# --------------------------------------------------------------------------------------------


def _business_only(result, check_id: str) -> list[str]:
    assert result.passed, [r.check.check_id for r in result.integrity_failures]
    found = _failures(result)
    assert set(found) <= {check_id, RULE_CARRIER_CLAIM}, found
    return found[check_id]


def test_wrong_device_on_the_readings(app_config, reference) -> None:
    raw = _raw()
    raw[T.DEVICES] = pd.concat(
        [raw[T.DEVICES], raw[T.DEVICES].assign(device_id="DEV-000002", serial_number="SN-0000002")],
        ignore_index=True,
    )
    raw[T.TEMPERATURE_READINGS]["device_id"] = "DEV-000002"
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert len(_business_only(result, RULE_ASSIGNED_DEVICE)) == 31
    assert _failures(result)[RULE_CARRIER_CLAIM] == [SHP]  # PASS claimed, REVIEW decided


def test_missing_readings(app_config, reference) -> None:
    raw = _raw()
    drop_readings(raw, "0900", "0910", "0920")
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert _business_only(result, RULE_COMPLETE_STREAM) == [LOG]
    detail = result.findings.set_index("check_id").loc[RULE_COMPLETE_STREAM, "detail"]
    assert detail == "3 of 31 readings missing, largest gap 40 min"


def test_deleted_handoff_breaks_the_custody_chain(app_config, reference) -> None:
    raw = _raw()
    custody = raw[T.CUSTODY_EVENTS]
    raw[T.CUSTODY_EVENTS] = custody[custody["sequence"] != 2].reset_index(drop=True)
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert result.passed
    found = _failures(result)
    assert found[RULE_CUSTODY_CHAIN] == [f"CUS-{SHP}-03"]
    assert found[RULE_CUSTODY_COVERAGE] == [f"RST-{SHP}-02"]
    assert set(found) == {RULE_CUSTODY_CHAIN, RULE_CUSTODY_COVERAGE, RULE_CARRIER_CLAIM}


def test_wrong_declared_policy(app_config, reference) -> None:
    raw = _raw()
    raw[T.SHIPMENTS]["declared_policy_id"] = "THR-REFRIG_2_8-V2"
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert _business_only(result, RULE_DECLARED_POLICY) == [SHP]


def test_carrier_claim_contradicting_the_decision(app_config, reference) -> None:
    raw = _raw()
    raw[T.SHIPMENTS]["carrier_reported_status"] = "FAIL"
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert _business_only(result, RULE_CARRIER_CLAIM) == [SHP]


def test_overlapping_device_assignments(app_config, reference) -> None:
    raw = _raw()
    add_back_to_back_shipment(raw, shift_min=200)  # second window starts before the first ends
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert _business_only(result, RULE_DEVICE_OVERLAP) == [LOG2]


def test_back_to_back_assignments_do_not_overlap(app_config, reference) -> None:
    raw = _raw()
    add_back_to_back_shipment(raw)
    tables = _with_engine(app_config, raw)
    assert set(tables[T.COMPLIANCE_DECISIONS]["shipment_id"]) == {SHP, SHP2}
    assert _failures(_run(app_config, reference, tables)) == {}


def test_shipment_without_a_logger(app_config, reference) -> None:
    raw = _raw()
    raw[T.LOGGERS] = raw[T.LOGGERS].iloc[0:0]
    raw[T.TEMPERATURE_READINGS] = raw[T.TEMPERATURE_READINGS].iloc[0:0]
    result = _run(app_config, reference, _with_engine(app_config, raw))
    assert _business_only(result, RULE_HAS_LOGGER) == [SHP]
