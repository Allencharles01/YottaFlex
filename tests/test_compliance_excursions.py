"""Excursions and calibration uncertainty (compliance_rules.md §1.5, §1.6)."""

import pandas as pd
import pytest

from coldchain.compliance import excursions, logger_check, policy
from coldchain.enums import TableName
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


def _run(raw) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    windows = logger_check.assignment_windows(
        raw[TableName.SHIPMENTS], raw[TableName.LOGGERS], raw[TableName.DEVICES], 10
    )
    screened = logger_check.screen_readings(raw[TableName.TEMPERATURE_READINGS], windows)
    trusted = logger_check.trusted_readings(screened)
    policies = policy.resolve_policies(
        raw[TableName.SHIPMENTS],
        raw[TableName.SHIPMENT_LOTS],
        raw[TableName.LOTS],
        raw[TableName.PRODUCTS],
        raw[TableName.THRESHOLD_POLICIES],
    )
    reports, bridge = excursions.detect_excursions(trusted, windows, policies)
    summary = excursions.excursion_summary(reports, policies)
    near = excursions.near_limit_flags(trusted, windows, policies)
    return reports, bridge, summary, near


def _run_of(start: int, count: int, value: float) -> dict[str, float]:
    """``count`` consecutive readings at ``value`` from minute ``start`` after 08:00."""
    stamps = pd.date_range("2026-03-10 08:00", periods=60, freq="10min")[start : start + count]
    return {t.strftime("%H%M"): value for t in stamps}


def test_compliant_stream_has_no_excursion(raw_one) -> None:
    reports, bridge, summary, near = _run(raw_one)
    assert reports.empty and bridge.empty
    assert summary.loc[SHP, "total_excursion_min"] == 0
    assert summary.loc[SHP, "excursion_report_ids"] is None
    assert not summary.loc[SHP].drop(["total_excursion_min", "excursion_report_ids"]).any()
    assert not near.loc[SHP]


@pytest.mark.parametrize(("value", "out"), [(8.0, False), (8.01, True), (2.0, False), (1.99, True)])
def test_limits_are_in_range(raw_one, value, out) -> None:
    set_temperatures(raw_one, {"0900": value})
    reports, *_ = _run(raw_one)
    assert len(reports) == int(out)


@pytest.mark.parametrize(
    ("readings", "duration", "breach"),
    [
        (1, 10, "WITHIN_ALLOWANCE"),
        (3, 30, "WITHIN_ALLOWANCE"),  # boundary: exactly the 30-min allowance passes
        (4, 40, "SINGLE_DURATION_BREACH"),
    ],
)
def test_single_duration_boundary_v1(raw_one, readings, duration, breach) -> None:
    set_temperatures(raw_one, _run_of(6, readings, 8.5))
    reports, bridge, summary, _ = _run(raw_one)
    report = reports.iloc[0]
    assert len(reports) == 1 and report["duration_min"] == duration
    assert report["breach_type"] == breach and report["reading_count"] == readings
    assert len(bridge) == readings
    assert summary.loc[SHP, "SINGLE_DURATION_BREACH"] == (breach == "SINGLE_DURATION_BREACH")
    assert summary.loc[SHP, "EXCURSION_WITHIN_ALLOWANCE"] == (breach == "WITHIN_ALLOWANCE")


def test_same_run_breaches_under_v2(raw_one) -> None:
    """Three readings (30 min) pass V1 (30) but breach V2 (20)."""
    shipments = raw_one[TableName.SHIPMENTS]
    shipments["actual_departure_at"] = _ts("2026-07-01 00:00")
    shipments["actual_arrival_at"] = _ts("2026-07-01 04:00")
    set_temperatures(raw_one, _run_of(6, 3, 8.5))
    reports, *_ = _run(raw_one)
    assert reports.iloc[0]["policy_id"] == "THR-REFRIG_2_8-V2"
    assert reports.iloc[0]["breach_type"] == "SINGLE_DURATION_BREACH"


@pytest.mark.parametrize(
    ("value", "breach"),
    [
        (12.0, "WITHIN_ALLOWANCE"),  # exactly abs_max_c is not beyond it
        (12.01, "ABSOLUTE_LIMIT_BREACH"),
        (0.0, "WITHIN_ALLOWANCE"),
        (-0.01, "ABSOLUTE_LIMIT_BREACH"),
    ],
)
def test_absolute_limit_boundary(raw_one, value, breach) -> None:
    set_temperatures(raw_one, {"0900": value})
    reports, _, summary, _ = _run(raw_one)
    assert reports.iloc[0]["breach_type"] == breach
    assert summary.loc[SHP, "ABSOLUTE_LIMIT_BREACH"] == (breach == "ABSOLUTE_LIMIT_BREACH")


def test_absolute_breach_wins_over_duration(raw_one) -> None:
    set_temperatures(raw_one, {**_run_of(0, 5, 9.0), "0820": 12.5})
    reports, _, summary, _ = _run(raw_one)
    report = reports.iloc[0]
    assert report["breach_type"] == "ABSOLUTE_LIMIT_BREACH" and report["duration_min"] == 50
    assert report["peak_c"] == 12.5
    assert not summary.loc[SHP, "SINGLE_DURATION_BREACH"]


def test_report_fields_and_bridge(raw_one) -> None:
    set_temperatures(raw_one, {"0900": 1.5, "0910": 0.7, "0920": 1.9})
    reports, bridge, summary, _ = _run(raw_one)
    report = reports.iloc[0]
    assert report["report_id"] == "EXCR-2026-000001" and report["direction"] == "LOW"
    assert report["shipment_id"] == SHP and report["logger_id"] == LOG
    assert report["policy_id"] == "THR-REFRIG_2_8-V1"
    assert report["peak_c"] == 0.7  # minimum for LOW
    assert report["started_at"] == pd.Timestamp("2026-03-10 09:00", tz="UTC")
    assert report["ended_at"] == pd.Timestamp("2026-03-10 09:20", tz="UTC")
    assert report["first_reading_id"] == f"TEMP-{LOG}-20260310T0900"
    assert report["last_reading_id"] == f"TEMP-{LOG}-20260310T0920"
    assert list(bridge["reading_id"]) == [f"TEMP-{LOG}-20260310T09{m}0" for m in "012"]
    assert set(bridge["report_id"]) == {"EXCR-2026-000001"}
    assert summary.loc[SHP, "excursion_report_ids"] == "EXCR-2026-000001"


def test_in_range_reading_ends_the_run(raw_one) -> None:
    set_temperatures(raw_one, {"0900": 8.5, "0910": 5.0, "0920": 8.5})
    reports, *_ = _run(raw_one)
    assert list(reports["duration_min"]) == [10, 10]


def test_direction_change_ends_the_run(raw_one) -> None:
    set_temperatures(raw_one, {"0900": 8.5, "0910": 1.5})
    reports, *_ = _run(raw_one)
    assert list(reports["direction"]) == ["HIGH", "LOW"]
    assert list(reports["report_id"]) == ["EXCR-2026-000001", "EXCR-2026-000002"]


def test_gap_does_not_end_the_run(raw_one) -> None:
    """DECISIONS.md #9: the missing time counts as excursion time."""
    set_temperatures(raw_one, {"0900": 8.5, "0920": 8.5})
    drop_readings(raw_one, "0910")
    reports, bridge, *_ = _run(raw_one)
    report = reports.iloc[0]
    assert len(reports) == 1 and report["reading_count"] == 2 and report["duration_min"] == 30
    assert len(bridge) == 2


def test_untrusted_reading_is_excluded_from_excursions(raw_one) -> None:
    set_temperatures(raw_one, {"0900": 8.5, "0910": 9.0, "0920": 8.5})
    readings = raw_one[TableName.TEMPERATURE_READINGS]
    readings.loc[readings["recorded_at"].dt.strftime("%H%M") == "0910", "device_id"] = "DEV-000002"
    reports, bridge, *_ = _run(raw_one)
    assert len(reports) == 1 and reports.iloc[0]["reading_count"] == 2
    assert reports.iloc[0]["peak_c"] == 8.5
    assert f"TEMP-{LOG}-20260310T0910" not in set(bridge["reading_id"])


@pytest.mark.parametrize(("extra", "breach"), [(0, False), (1, True)])
def test_cumulative_boundary(raw_one, extra, breach) -> None:
    """Two 30-min excursions = 60 min = the allowance; one more reading breaches it."""
    set_temperatures(
        raw_one, {**_run_of(0, 3, 8.5), **_run_of(6, 3, 8.5), **_run_of(12, extra, 8.5)}
    )
    reports, _, summary, _ = _run(raw_one)
    assert summary.loc[SHP, "total_excursion_min"] == 60 + 10 * extra
    assert summary.loc[SHP, "CUMULATIVE_DURATION_BREACH"] == breach
    assert (reports["breach_type"] == "WITHIN_ALLOWANCE").all()
    assert summary.loc[SHP, "excursion_report_ids"] == "|".join(reports["report_id"])


def test_cumulative_counts_both_directions(raw_one) -> None:
    set_temperatures(raw_one, {**_run_of(0, 3, 8.5), **_run_of(6, 4, 1.5)})
    _, _, summary, _ = _run(raw_one)
    assert summary.loc[SHP, "total_excursion_min"] == 70
    assert summary.loc[SHP, "CUMULATIVE_DURATION_BREACH"]


def test_back_to_back_shipments_on_one_device_are_not_merged(raw_one) -> None:
    """A warm end of SHP1 and a warm start of SHP2 on the same device stay two excursions.

    Grouped by device they would form one 50-min run (a single-duration breach).
    """
    add_back_to_back_shipment(raw_one)
    set_temperatures_of(raw_one, LOG, {"1210": 9.0, "1220": 9.0, "1230": 9.0})
    set_temperatures_of(raw_one, LOG2, {"1240": 9.0, "1250": 9.0})
    reports, _, summary, _ = _run(raw_one)
    by_ship = reports.set_index("shipment_id")
    assert by_ship.loc[SHP, "duration_min"] == 30 and by_ship.loc[SHP2, "duration_min"] == 20
    assert (reports["breach_type"] == "WITHIN_ALLOWANCE").all()
    assert summary.loc[[SHP, SHP2], "total_excursion_min"].tolist() == [30, 20]


def test_report_ids_follow_shipment_then_start_time(raw_one) -> None:
    add_back_to_back_shipment(raw_one)
    set_temperatures_of(raw_one, LOG2, {"1300": 9.0})  # SHP2 first in time of writing below
    set_temperatures_of(raw_one, LOG, {"1100": 9.0, "0900": 1.0})
    reports, *_ = _run(raw_one)
    ordered = reports.sort_values("report_id")
    assert list(ordered["shipment_id"]) == [SHP, SHP, SHP2]
    assert list(ordered["started_at"].dt.strftime("%H%M")) == ["0900", "1100", "1300"]


@pytest.mark.parametrize(
    ("value", "near"),
    [
        (5.0, False),
        (7.7, True),  # |7.7 - 8| = 0.30 <= u = 0.3 (inclusive, after rounding)
        (7.69, False),
        (8.0, True),  # at the limit: in range but uncertain
        (8.3, True),  # out of range, still within u of the limit
        (2.3, True),
        (2.31, False),
    ],
)
def test_near_limit_uncertainty(raw_one, value, near) -> None:
    set_temperatures(raw_one, {"0900": value})
    *_, flags = _run(raw_one)
    assert flags.loc[SHP] == near


def test_near_limit_ignores_untrusted_readings(raw_one) -> None:
    set_temperatures(raw_one, {"0900": 8.0})
    readings = raw_one[TableName.TEMPERATURE_READINGS]
    readings.loc[readings["recorded_at"].dt.strftime("%H%M") == "0900", "device_id"] = "DEV-000002"
    *_, flags = _run(raw_one)
    assert not flags.loc[SHP]


def test_empty_readings(raw_one) -> None:
    raw_one[TableName.TEMPERATURE_READINGS] = raw_one[TableName.TEMPERATURE_READINGS].iloc[0:0]
    reports, bridge, summary, near = _run(raw_one)
    assert reports.empty and bridge.empty
    assert summary.loc[SHP, "total_excursion_min"] == 0 and not near.loc[SHP]
