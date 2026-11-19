"""Logger assignment, trusted readings and calibration (compliance_rules.md §1.2, §1.7)."""

from datetime import date

import pandas as pd
import pytest

from coldchain.compliance import logger_check
from coldchain.enums import TableName
from coldchain.errors import ComplianceError
from conftest import LOG, SHP, _ts, readings_frame


def _windows(raw) -> pd.DataFrame:
    return logger_check.assignment_windows(
        raw[TableName.SHIPMENTS], raw[TableName.LOGGERS], raw[TableName.DEVICES], 10
    )


def _evaluate(raw) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    windows = _windows(raw)
    screened = logger_check.screen_readings(raw[TableName.TEMPERATURE_READINGS], windows)
    return windows, screened, logger_check.logger_flags(windows, screened)


def test_assigned_window_device_and_interval(raw_one) -> None:
    row = _windows(raw_one).iloc[0]
    assert row["has_logger"] and row["logger_id"] == LOG and row["device_id"] == "DEV-000001"
    assert row["window_start"] == pd.Timestamp("2026-03-10 07:30", tz="UTC")
    assert row["window_end"] == pd.Timestamp("2026-03-10 12:30", tz="UTC")
    assert row["interval_min"] == 10 and row["uncertainty_c"] == 0.3


def test_complete_stream_is_trusted_and_unflagged(raw_one) -> None:
    _, screened, flags = _evaluate(raw_one)
    assert len(screened) == 31 and screened["trusted"].all()
    assert not flags.loc[SHP].any()


def test_readings_outside_the_window_are_ignored(raw_one) -> None:
    extra = readings_frame(LOG, "DEV-000001", pd.DatetimeIndex(["2026-03-10 07:20"], tz="UTC"))
    raw_one[TableName.TEMPERATURE_READINGS] = pd.concat(
        [raw_one[TableName.TEMPERATURE_READINGS], extra], ignore_index=True
    )
    _, screened, _ = _evaluate(raw_one)
    assert len(screened) == 31
    assert screened["recorded_at"].min() == pd.Timestamp("2026-03-10 07:30", tz="UTC")


def test_both_window_ends_are_inclusive(raw_one) -> None:
    _, screened, _ = _evaluate(raw_one)
    times = set(screened["recorded_at"].dt.strftime("%H%M"))
    assert {"0730", "1230"} <= times


def test_wrong_device_inside_window_is_untrusted_and_flagged(raw_one) -> None:
    readings = raw_one[TableName.TEMPERATURE_READINGS]
    readings.loc[readings["recorded_at"].dt.strftime("%H%M") == "0900", "device_id"] = "DEV-000002"
    _, screened, flags = _evaluate(raw_one)
    trusted = logger_check.trusted_readings(screened)
    assert len(trusted) == 30
    assert "0900" not in set(trusted["recorded_at"].dt.strftime("%H%M"))
    assert flags.loc[SHP, "LOGGER_NOT_ASSIGNED"]


def test_wrong_device_outside_window_is_ignored(raw_one) -> None:
    """DECISIONS.md #60: the device check applies inside the window only."""
    foreign = readings_frame(LOG, "DEV-000002", pd.DatetimeIndex(["2026-03-10 12:40"], tz="UTC"))
    raw_one[TableName.TEMPERATURE_READINGS] = pd.concat(
        [raw_one[TableName.TEMPERATURE_READINGS], foreign], ignore_index=True
    )
    _, _, flags = _evaluate(raw_one)
    assert not flags.loc[SHP, "LOGGER_NOT_ASSIGNED"]


def test_readings_of_another_logger_are_not_attributed(raw_one) -> None:
    other = readings_frame(
        "LOG-SHP-2026-000002-01", "DEV-000001", pd.DatetimeIndex(["2026-03-10 09:00"], tz="UTC")
    )
    raw_one[TableName.TEMPERATURE_READINGS] = pd.concat(
        [raw_one[TableName.TEMPERATURE_READINGS], other], ignore_index=True
    )
    _, screened, _ = _evaluate(raw_one)
    assert set(screened["logger_id"]) == {LOG}


def test_no_logger_falls_back_to_actual_trip(raw_one) -> None:
    """DECISIONS.md #59: actual departure/arrival, config interval, no device."""
    raw_one[TableName.LOGGERS] = raw_one[TableName.LOGGERS].iloc[0:0]
    windows, screened, flags = _evaluate(raw_one)
    row = windows.iloc[0]
    assert not row["has_logger"]
    assert row["window_start"] == pd.Timestamp("2026-03-10 08:00", tz="UTC")
    assert row["window_end"] == pd.Timestamp("2026-03-10 12:00", tz="UTC")
    assert row["interval_min"] == 10 and pd.isna(row["uncertainty_c"])
    assert screened.empty
    assert flags.loc[SHP, "LOGGER_NOT_ASSIGNED"]
    assert not flags.loc[SHP, "CALIBRATION_EXPIRED"]  # no device to judge


def test_logger_with_unknown_device_raises(raw_one) -> None:
    raw_one[TableName.LOGGERS]["device_id"] = "DEV-000099"
    with pytest.raises(ComplianceError, match="unknown devices"):
        _windows(raw_one)


@pytest.mark.parametrize(
    ("calibrated_on", "due_on", "expired"),
    [
        (date(2026, 1, 5), date(2027, 1, 5), False),
        (date(2026, 1, 5), date(2026, 3, 10), False),  # due on the last day: still valid
        (date(2026, 1, 5), date(2026, 3, 9), True),  # due the day before
        (date(2026, 3, 10), date(2027, 3, 10), False),  # calibrated on the first day
        (date(2026, 3, 11), date(2027, 3, 11), True),  # calibrated after the start
    ],
)
def test_calibration_must_cover_the_window(raw_one, calibrated_on, due_on, expired) -> None:
    devices = raw_one[TableName.DEVICES]
    devices["calibrated_on"] = [calibrated_on]
    devices["calibration_due_on"] = [due_on]
    _, _, flags = _evaluate(raw_one)
    assert flags.loc[SHP, "CALIBRATION_EXPIRED"] == expired


def test_calibration_uses_the_unassignment_date_across_midnight(raw_one) -> None:
    loggers = raw_one[TableName.LOGGERS]
    loggers["unassigned_at"] = _ts("2026-03-11 00:10")
    raw_one[TableName.DEVICES]["calibration_due_on"] = [date(2026, 3, 10)]
    _, _, flags = _evaluate(raw_one)
    assert flags.loc[SHP, "CALIBRATION_EXPIRED"]


def test_empty_inputs(raw_one) -> None:
    for name in (TableName.SHIPMENTS, TableName.LOGGERS, TableName.TEMPERATURE_READINGS):
        raw_one[name] = raw_one[name].iloc[0:0]
    windows, screened, flags = _evaluate(raw_one)
    assert windows.empty and screened.empty and flags.empty
