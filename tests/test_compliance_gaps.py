"""Coverage and gaps (compliance_rules.md §1.3, §1.4; DECISIONS.md #14)."""

import pandas as pd
import pytest

from coldchain.compliance import gaps, logger_check, policy
from coldchain.enums import TableName
from conftest import SHP, _ts, drop_readings


def _coverage(raw) -> pd.Series:
    windows = logger_check.assignment_windows(
        raw[TableName.SHIPMENTS], raw[TableName.LOGGERS], raw[TableName.DEVICES], 10
    )
    screened = logger_check.screen_readings(raw[TableName.TEMPERATURE_READINGS], windows)
    policies = policy.resolve_policies(
        raw[TableName.SHIPMENTS],
        raw[TableName.SHIPMENT_LOTS],
        raw[TableName.LOTS],
        raw[TableName.PRODUCTS],
        raw[TableName.THRESHOLD_POLICIES],
    )
    trusted = logger_check.trusted_readings(screened)
    return gaps.coverage_and_gaps(windows, trusted, policies).loc[SHP]


def test_complete_stream(raw_one) -> None:
    row = _coverage(raw_one)
    assert row["reading_count"] == 31 and row["expected_reading_count"] == 31
    assert row["coverage_pct"] == 100.0
    assert row["max_gap_min"] == 10  # = i for a complete stream
    assert not row["DATA_GAP"] and not row["LOW_COVERAGE"]


@pytest.mark.parametrize(
    ("missing", "gap", "breach"),
    [
        ((), 10, False),
        (("0900",), 20, False),
        (("0900", "0910"), 30, False),  # boundary: exactly max_reading_gap_min passes
        (("0900", "0910", "0920"), 40, True),
    ],
)
def test_gap_boundary(raw_one, missing, gap, breach) -> None:
    drop_readings(raw_one, *missing)
    row = _coverage(raw_one)
    assert row["max_gap_min"] == gap
    assert row["DATA_GAP"] == breach


@pytest.mark.parametrize(
    ("missing", "gap", "breach"),
    [
        (("0730", "0740"), 30, False),  # late start: the virtual reading at 07:20 counts
        (("0730", "0740", "0750"), 40, True),
        (("1220", "1230"), 30, False),  # early stop: the virtual reading at 12:40 counts
        (("1210", "1220", "1230"), 40, True),
    ],
)
def test_window_edges_count_as_gaps(raw_one, missing, gap, breach) -> None:
    drop_readings(raw_one, *missing)
    row = _coverage(raw_one)
    assert row["max_gap_min"] == gap
    assert row["DATA_GAP"] == breach


def test_coverage_boundary_is_strict(raw_one) -> None:
    """19 of 20 expected readings = 95.00 % passes; 18 of 20 = 90.00 % fails."""
    raw_one[TableName.LOGGERS]["unassigned_at"] = _ts("2026-03-10 10:40")  # 07:30..10:40 = 20
    drop_readings(raw_one, "0900")
    row = _coverage(raw_one)
    assert row["expected_reading_count"] == 20 and row["reading_count"] == 19
    assert row["coverage_pct"] == 95.0 and not row["LOW_COVERAGE"]
    drop_readings(raw_one, "0910")
    row = _coverage(raw_one)
    assert row["coverage_pct"] == 90.0 and row["LOW_COVERAGE"]


def test_coverage_is_rounded_to_two_decimals(raw_one) -> None:
    drop_readings(raw_one, "0900")
    assert _coverage(raw_one)["coverage_pct"] == round(100 * 30 / 31, 2) == 96.77


def test_zero_trusted_readings(raw_one) -> None:
    raw_one[TableName.TEMPERATURE_READINGS] = raw_one[TableName.TEMPERATURE_READINGS].iloc[0:0]
    row = _coverage(raw_one)
    assert row["reading_count"] == 0 and row["coverage_pct"] == 0.0
    assert row["max_gap_min"] == 300 + 2 * 10  # window + 2i
    assert row["DATA_GAP"] and row["LOW_COVERAGE"]


def test_no_logger_fallback_window(raw_one) -> None:
    """DECISIONS.md #59: 08:00..12:00 → floor(240 / 10) + 1 = 25 expected, zero trusted."""
    raw_one[TableName.LOGGERS] = raw_one[TableName.LOGGERS].iloc[0:0]
    raw_one[TableName.SHIPMENTS]["actual_arrival_at"] = _ts("2026-03-10 12:05")  # off the grid
    row = _coverage(raw_one)
    assert row["expected_reading_count"] == 25
    assert row["reading_count"] == 0 and row["max_gap_min"] == 245 + 20


def test_untrusted_readings_do_not_count(raw_one) -> None:
    readings = raw_one[TableName.TEMPERATURE_READINGS]
    stamps = readings["recorded_at"].dt.strftime("%H%M")
    readings.loc[stamps.isin(["0900", "0910", "0920"]), "device_id"] = "DEV-000002"
    row = _coverage(raw_one)
    assert row["reading_count"] == 28 and row["max_gap_min"] == 40 and row["DATA_GAP"]
