"""Shared pytest fixtures."""

import copy
from datetime import date
from typing import Any

import pandas as pd
import pytest
import yaml

from coldchain.config import DEFAULT_CONFIG_PATH
from coldchain.enums import TableName
from coldchain.schemas import TIMESTAMP_DTYPE


@pytest.fixture
def raw_config() -> dict[str, Any]:
    """A fresh, mutable copy of the default config as a plain dict."""
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    return copy.deepcopy(data)


# --------------------------------------------------------------------------------------------
# A hand-made, internally consistent one-shipment sample of every table (used by schema tests).
# --------------------------------------------------------------------------------------------

SHP = "SHP-2026-000001"
LOG = "LOG-SHP-2026-000001-01"


def _ts(*values: str | None) -> pd.Series:
    """Timestamps in the project dtype (UTC, us); ``None`` → NaT."""
    return pd.Series(pd.to_datetime(list(values), utc=True)).astype(TIMESTAMP_DTYPE)


def _str(*values: str | None) -> pd.Series:
    return pd.Series(list(values), dtype="str")


def _reading(hhmm: str) -> str:
    return f"TEMP-{LOG}-20260310T{hhmm}"


def build_valid_tables() -> dict[TableName, pd.DataFrame]:
    """Return a fresh dict with one valid sample frame per table."""
    readings_at = ["2026-03-10 09:00", "2026-03-10 09:10", "2026-03-10 09:20"]
    tables = {
        TableName.PARTIES: pd.DataFrame(
            {
                "party_id": [f"PTY-000{i}" for i in range(1, 8)],
                "party_type": [
                    "SUPPLIER",
                    "CARRIER",
                    "WAREHOUSE",
                    "RECEIVER",
                    "PLANNER",
                    "LOGISTICS_ANALYST",
                    "OPERATIONS_MANAGER",
                ],
                "name": [
                    "Hyderabad Biologics Ltd",
                    "Deccan Cold Logistics",
                    "Nagpur Central Cold Store",
                    "Delhi City Hospital",
                    "Planning Desk A",
                    "QA Analyst A",
                    "Ops Manager A",
                ],
                "city": ["Hyderabad", "Hyderabad", "Nagpur", "Delhi", "Mumbai", "Mumbai", "Mumbai"],
                "active_from": [date(2025, 1, 1)] * 7,
            }
        ),
        TableName.THRESHOLD_POLICIES: pd.DataFrame(
            {
                "policy_id": ["THR-REFRIG_2_8-V1", "THR-REFRIG_2_8-V2"],
                "policy_code": ["REFRIG_2_8", "REFRIG_2_8"],
                "version": [1, 2],
                "min_c": [2.0, 2.0],
                "max_c": [8.0, 8.0],
                "abs_min_c": [0.0, 0.0],
                "abs_max_c": [12.0, 12.0],
                "max_single_excursion_min": [30, 20],
                "max_cumulative_excursion_min": [60, 60],
                "max_reading_gap_min": [30, 30],
                "min_coverage_pct": [95.0, 95.0],
                "effective_from": [date(2026, 1, 1), date(2026, 7, 1)],
                "effective_to": pd.Series([date(2026, 7, 1), None], dtype=object),
            }
        ),
        TableName.PRODUCTS: pd.DataFrame(
            {
                "product_id": ["PRD-0001"],
                "name": ["Hepatitis B Vaccine"],
                "category": ["VACCINE"],
                "supplier_party_id": ["PTY-0001"],
                "policy_code": ["REFRIG_2_8"],
                "shelf_life_days": [730],
            }
        ),
        TableName.PACKAGING: pd.DataFrame(
            {
                "packaging_id": ["PKG-0001"],
                "name": ["Passive Gel Shipper 2-8 C"],
                "packaging_type": ["PASSIVE_GEL"],
                "supported_policy_code": ["REFRIG_2_8"],
                "setpoint_c": [5.0],
                "max_hold_hours": [96.0],
                "thermal_response": [0.25],
            }
        ),
        TableName.LOTS: pd.DataFrame(
            {
                "lot_id": ["LOT-2026-000001"],
                "product_id": ["PRD-0001"],
                "manufactured_on": [date(2026, 1, 15)],
                "expires_on": [date(2028, 1, 15)],
                "quantity_units": [5000],
                "lot_status": ["RELEASED"],
            }
        ),
        TableName.DEVICES: pd.DataFrame(
            {
                "device_id": ["DEV-000001"],
                "model": ["TL-400"],
                "serial_number": ["SN-0000001"],
                "calibrated_on": [date(2026, 1, 5)],
                "calibration_due_on": [date(2027, 1, 5)],
                "uncertainty_c": [0.3],
                "reading_interval_min": [10],
            }
        ),
        TableName.SHIPMENTS: pd.DataFrame(
            {
                "shipment_id": [SHP],
                "supplier_party_id": ["PTY-0001"],
                "carrier_party_id": ["PTY-0002"],
                "receiver_party_id": ["PTY-0004"],
                "planner_party_id": ["PTY-0005"],
                "packaging_id": ["PKG-0001"],
                "declared_policy_id": ["THR-REFRIG_2_8-V1"],
                "planned_departure_at": _ts("2026-03-10 08:00"),
                "planned_arrival_at": _ts("2026-03-10 12:00"),
                "actual_departure_at": _ts("2026-03-10 08:00"),
                "actual_arrival_at": _ts("2026-03-10 12:00"),
                "carrier_reported_status": ["PASS"],
            }
        ),
        TableName.SHIPMENT_LOTS: pd.DataFrame(
            {"shipment_id": [SHP], "lot_id": ["LOT-2026-000001"], "quantity_units": [1200]}
        ),
        TableName.LOGGERS: pd.DataFrame(
            {
                "logger_id": [LOG],
                "shipment_id": [SHP],
                "device_id": ["DEV-000001"],
                "assigned_at": _ts("2026-03-10 07:30"),
                "unassigned_at": _ts("2026-03-10 12:30"),
            }
        ),
        TableName.TEMPERATURE_READINGS: pd.DataFrame(
            {
                "reading_id": [_reading("0900"), _reading("0910"), _reading("0920")],
                "logger_id": [LOG] * 3,
                "device_id": ["DEV-000001"] * 3,
                "recorded_at": _ts(*readings_at),
                "temperature_c": [7.85, 8.5, 9.0],
            }
        ),
        TableName.ROUTE_STOPS: pd.DataFrame(
            {
                "stop_id": [f"RST-{SHP}-0{i}" for i in (1, 2, 3)],
                "shipment_id": [SHP] * 3,
                "sequence": [1, 2, 3],
                "stop_type": ["ORIGIN", "WAREHOUSE", "DESTINATION"],
                "party_id": ["PTY-0001", "PTY-0003", "PTY-0004"],
                "arrived_at": _ts(None, "2026-03-10 10:00", "2026-03-10 12:00"),
                "departed_at": _ts("2026-03-10 08:00", "2026-03-10 10:30", None),
                "dwell_min": pd.array([pd.NA, 30, pd.NA], dtype="Int64"),
            }
        ),
        TableName.CUSTODY_EVENTS: pd.DataFrame(
            {
                "custody_event_id": [f"CUS-{SHP}-0{i}" for i in (1, 2, 3, 4)],
                "shipment_id": [SHP] * 4,
                "stop_id": [f"RST-{SHP}-0{i}" for i in (1, 2, 2, 3)],
                "sequence": [1, 2, 3, 4],
                "event_type": ["PICKUP", "HANDOFF", "HANDOFF", "DELIVERY"],
                "from_party_id": ["PTY-0001", "PTY-0002", "PTY-0003", "PTY-0002"],
                "to_party_id": ["PTY-0002", "PTY-0003", "PTY-0002", "PTY-0004"],
                "event_at": _ts(
                    "2026-03-10 08:00", "2026-03-10 10:00", "2026-03-10 10:30", "2026-03-10 12:00"
                ),
            }
        ),
        TableName.SHIPMENT_STATUS_EVENTS: pd.DataFrame(
            {
                "status_event_id": [f"STE-{SHP}-0{i}" for i in range(1, 7)],
                "shipment_id": [SHP] * 6,
                "sequence": [1, 2, 3, 4, 5, 6],
                "status": [
                    "PLANNED",
                    "PICKED_UP",
                    "IN_TRANSIT",
                    "AT_STOP",
                    "IN_TRANSIT",
                    "DELIVERED",
                ],
                "event_at": _ts(
                    "2026-03-09 09:00",
                    "2026-03-10 08:00",
                    "2026-03-10 08:00",
                    "2026-03-10 10:00",
                    "2026-03-10 10:30",
                    "2026-03-10 12:00",
                ),
                "actor_party_id": [
                    "PTY-0005",
                    "PTY-0002",
                    "PTY-0002",
                    "PTY-0003",
                    "PTY-0002",
                    "PTY-0004",
                ],
            }
        ),
        TableName.EXCURSION_REPORTS: pd.DataFrame(
            {
                "report_id": ["EXCR-2026-000001"],
                "shipment_id": [SHP],
                "logger_id": [LOG],
                "policy_id": ["THR-REFRIG_2_8-V1"],
                "direction": ["HIGH"],
                "started_at": _ts("2026-03-10 09:10"),
                "ended_at": _ts("2026-03-10 09:20"),
                "duration_min": [20],
                "peak_c": [9.0],
                "reading_count": [2],
                "first_reading_id": [_reading("0910")],
                "last_reading_id": [_reading("0920")],
                "breach_type": ["WITHIN_ALLOWANCE"],
            }
        ),
        TableName.EXCURSION_REPORT_READINGS: pd.DataFrame(
            {
                "report_id": ["EXCR-2026-000001"] * 2,
                "reading_id": [_reading("0910"), _reading("0920")],
            }
        ),
        TableName.COMPLIANCE_DECISIONS: pd.DataFrame(
            {
                "shipment_id": [SHP],
                "applied_policy_id": ["THR-REFRIG_2_8-V1"],
                "policy_source": ["DECLARED_CONFIRMED"],
                "status": ["COMPLIANT_WITH_WARNINGS"],
                "reason_codes": _str("EXCURSION_WITHIN_ALLOWANCE|NEAR_LIMIT_UNCERTAIN"),
                "reading_count": [30],
                "expected_reading_count": [31],
                "coverage_pct": [96.77],
                "max_gap_min": [20],
                "total_excursion_min": [20],
                "excursion_report_ids": _str("EXCR-2026-000001"),
                "decided_action": ["RELEASE"],
            }
        ),
        TableName.COMPLIANCE_STATUS_EVENTS: pd.DataFrame(
            {
                "status_event_id": [f"STE-{SHP}-07", f"STE-{SHP}-08"],
                "shipment_id": [SHP] * 2,
                "sequence": [7, 8],
                "status": ["UNDER_REVIEW", "RELEASED"],
                "event_at": _ts("2026-03-10 12:30", "2026-03-10 12:30"),
                "actor_party_id": ["PTY-0006", "PTY-0007"],
            }
        ),
        TableName.EXPECTED_OUTCOMES: pd.DataFrame(
            {
                "shipment_id": [SHP],
                "scenario": ["MINOR_SPIKE_WITHIN_ALLOWANCE"],
                "issue_type": _str(None),
                "expected_status": ["COMPLIANT_WITH_WARNINGS"],
                "expected_action": ["RELEASE"],
                "expected_reason_codes": _str("EXCURSION_WITHIN_ALLOWANCE|NEAR_LIMIT_UNCERTAIN"),
                "expected_excursion_count": [1],
                "expected_total_excursion_min": [20],
                "expected_excursion_windows": _str("2026-03-10T09:10:00Z/2026-03-10T09:20:00Z"),
                "expected_excursion_durations_min": _str("20"),
            }
        ),
        TableName.DEVICE_TRUTH: pd.DataFrame(
            {"device_id": ["DEV-000001"], "systematic_offset_c": [-0.12]}
        ),
        TableName.ANOMALIES: pd.DataFrame(
            {
                "anomaly_id": ["ANOM-000001"],
                "shipment_id": [SHP],
                "issue_type": ["custody_gap_not_flagged"],
                "description": ["Deleted the middle HANDOFF so the custody chain breaks."],
                "affected_record_ids": _str(f"CUS-{SHP}-02|CUS-{SHP}-04"),
                "removed_record_ids": _str(f"CUS-{SHP}-03"),
                "expected_status": ["INCONCLUSIVE"],
                "expected_reason_codes": _str("CUSTODY_GAP"),
                "expected_excursion_durations_min": _str(None),
            }
        ),
    }
    for frame in tables.values():
        for column in frame.columns:
            if frame[column].dtype == object and isinstance(frame[column].dropna().iloc[0], str):
                frame[column] = frame[column].astype("str")
    return tables


@pytest.fixture
def valid_tables() -> dict[TableName, pd.DataFrame]:
    """A fresh, valid one-shipment sample of every table."""
    return build_valid_tables()


# --------------------------------------------------------------------------------------------
# Generated starter-scale data (Phase 2+). Session-scoped: tests must not mutate the frames.
# --------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def app_config():
    """The validated default configuration."""
    from coldchain.config import load_config

    return load_config()


@pytest.fixture(scope="session")
def reference():
    """The validated default reference data."""
    from coldchain.config import load_reference

    return load_reference()


@pytest.fixture(scope="session")
def starter_tables(app_config, reference) -> dict[TableName, pd.DataFrame]:
    """Every generated table (raw + expected outcomes) at starter scale with the default seed."""
    from coldchain.generate import generate_tables

    return generate_tables(app_config, reference, "starter")
