"""Plotly figures of the viewer (Phase 9).

The temperature-chart tests check the serialised figure (``plotly.io.to_json``, what streamlit
sends to the browser), not only the Python objects: a chart whose x-axis plotly.js typed as
linear passed the first version of these tests (Phase 9 review, DECISIONS.md #129).
"""

import pandas as pd
import pytest

from conftest import LOG, WINDOW_TIMES, readings_frame

pytest.importorskip("plotly")

from coldchain.viewer import charts, views
from plotly_checks import (
    assert_date_chart,
    assert_labels_do_not_overlap,
    first_non_empty_x_trace,
    spec_of,
    trace,
    window_labels,
)

POLICY = pd.Series(
    {"min_c": 2.0, "max_c": 8.0, "abs_min_c": 0.0, "abs_max_c": 12.0, "max_reading_gap_min": 30}
)
WINDOW = {
    "window_start": WINDOW_TIMES[0],
    "window_end": WINDOW_TIMES[-1],
    "device_id": "DEV-000001",
    "interval_min": 10,
}
EMPTY_TIMES = pd.Series([], dtype="datetime64[us, UTC]")


def _excursions(*windows: tuple[int, int]) -> pd.DataFrame:
    """Excursion reports spanning the given WINDOW_TIMES index ranges."""
    reports = pd.DataFrame(
        {
            "report_id": [f"EXCR-2026-{i:06d}" for i in range(1, len(windows) + 1)],
            "started_at": pd.Series([WINDOW_TIMES[a] for a, _ in windows], dtype=EMPTY_TIMES.dtype),
            "ended_at": pd.Series([WINDOW_TIMES[b] for _, b in windows], dtype=EMPTY_TIMES.dtype),
            "duration_min": [10 * (b - a + 1) for a, b in windows],
        }
    )
    return views.excursion_spans(reports, 10)


def _chart(readings: pd.DataFrame, excursions: pd.DataFrame):
    """Build the chart exactly as the page does; return (figure, classified, gaps)."""
    classified = views.classify_readings(readings, WINDOW)
    trusted = classified[classified["reading_class"] == views.ReadingClass.TRUSTED.value]
    gaps = views.gap_spans(
        trusted["recorded_at"], WINDOW["window_start"], WINDOW["window_end"], 10, 30
    )
    figure = charts.temperature_chart(classified, POLICY, excursions, gaps, WINDOW)
    return figure, classified, gaps


def _expected_windows(excursions: pd.DataFrame) -> list[tuple[str, str]]:
    return [
        (charts.plotly_time(r.started_at), charts.plotly_time(r.span_end))
        for r in excursions.itertuples()
    ]


# --------------------------------------------------------------------------------------------
# Time conversion
# --------------------------------------------------------------------------------------------


def test_plotly_time_is_naive_utc_iso_text() -> None:
    assert charts.plotly_time(pd.Timestamp("2026-04-14 02:00", tz="UTC")) == "2026-04-14T02:00:00"
    ist = pd.Timestamp("2026-04-14 07:30", tz="Asia/Kolkata")
    assert charts.plotly_time(ist) == "2026-04-14T02:00:00"
    assert charts.plotly_times(pd.Series(WINDOW_TIMES[:2])) == [
        "2026-03-10T07:30:00",
        "2026-03-10T07:40:00",
    ]


@pytest.mark.parametrize("value", [pd.Timestamp("2026-04-14 02:00"), pd.NaT])
def test_plotly_time_refuses_naive_or_missing_timestamps(value) -> None:
    with pytest.raises(ValueError, match="time-zone-aware"):
        charts.plotly_time(value)


def test_plotly_times_refuses_a_naive_column() -> None:
    with pytest.raises(ValueError, match="time-zone-aware"):
        charts.plotly_times(pd.Series(pd.to_datetime(["2026-04-14 02:00"])))


# --------------------------------------------------------------------------------------------
# Bar charts
# --------------------------------------------------------------------------------------------


def test_status_bar_uses_the_status_palette_in_severity_order() -> None:
    decisions = pd.DataFrame({"status": ["COMPLIANT", "NON_COMPLIANT", "NON_COMPLIANT"]})
    spec = spec_of(charts.status_bar(views.status_counts(decisions)))
    bar = spec["data"][0]
    assert bar["y"] == list(charts.STATUS_COLORS)
    assert bar["marker"]["color"] == list(charts.STATUS_COLORS.values())
    assert bar["text"][3] == "2 (66.7 %)"
    assert spec["layout"]["yaxis"]["type"] == "category"
    assert spec["layout"]["xaxis"]["type"] == "linear"


def test_reason_bar_lists_every_code_on_explicit_axes() -> None:
    counts = pd.DataFrame({"reason_code": ["DATA_GAP", "LONG_DWELL"], "shipments": [2, 1]})
    spec = spec_of(charts.reason_bar(counts))
    assert spec["data"][0]["y"] == ["DATA_GAP", "LONG_DWELL"]
    assert spec["layout"]["yaxis"]["type"] == "category"
    assert spec["layout"]["xaxis"]["type"] == "linear"


# --------------------------------------------------------------------------------------------
# Temperature chart
# --------------------------------------------------------------------------------------------


def test_temperature_chart_reaches_plotly_js_as_dates() -> None:
    readings = readings_frame(LOG, "DEV-000001", WINDOW_TIMES)
    readings.loc[4, "device_id"] = "DEV-000009"
    excursions = _excursions((10, 13), (20, 20))
    figure, classified, gaps = _chart(readings, excursions)
    trusted = int((classified["reading_class"] == views.ReadingClass.TRUSTED.value).sum())

    spec = spec_of(figure)
    assert_date_chart(spec, trusted, len(readings), _expected_windows(excursions))
    assert first_non_empty_x_trace(spec)["name"] == "Readings (trusted)"
    assert trace(spec, "Readings (trusted)")["x"][0] == "2026-03-10T07:30:00"
    assert len(trace(spec, "Untrusted: wrong device")["x"]) == 1
    assert len(gaps) == 1


def test_temperature_chart_draws_band_limits_excursions_and_gaps() -> None:
    readings = readings_frame(LOG, "DEV-000001", WINDOW_TIMES)
    readings.loc[4, "device_id"] = "DEV-000009"
    figure, _, gaps = _chart(readings, _excursions((10, 13)))
    shapes = spec_of(figure)["layout"]["shapes"]

    band = [s for s in shapes if s["type"] == "rect" and s["yref"] == "y"]
    assert (band[0]["y0"], band[0]["y1"]) == (2.0, 8.0)
    dashed = [s for s in shapes if s["type"] == "line" and s["line"].get("dash") == "dash"]
    assert sorted(s["y0"] for s in dashed) == [0.0, 12.0]
    vertical = [s for s in shapes if s["type"] == "rect" and s["yref"] != "y"]
    colors = [s["fillcolor"] for s in vertical]
    assert colors.count(charts.EXCURSION_COLOR) == 1
    assert colors.count(charts.GAP_COLOR) == len(gaps) == 1
    names = [t.name for t in figure.data]
    for name in (
        "Readings (trusted)",
        "Untrusted: wrong device",
        "Excursion (engine report)",
        "Gap in trusted readings",
    ):
        assert name in names


def test_legend_only_traces_come_after_every_data_trace() -> None:
    figure, _, _ = _chart(readings_frame(LOG, "DEV-000001", WINDOW_TIMES), _excursions((3, 4)))
    empty = [i for i, t in enumerate(figure.data) if list(t.x) == [None]]
    data = [i for i, t in enumerate(figure.data) if list(t.x) != [None]]
    assert empty
    assert max(data) < min(empty)


def test_temperature_chart_without_readings_or_excursions() -> None:
    readings = readings_frame(LOG, "DEV-000001", WINDOW_TIMES).head(0)
    figure, _, gaps = _chart(readings, _excursions())
    spec = spec_of(figure)
    assert_date_chart(spec, 0, 0, [])
    names = [t["name"] for t in spec["data"]]
    assert "Excursion (engine report)" not in names
    assert "Gap in trusted readings" in names
    assert trace(spec, "Readings (trusted)")["x"] == []
    assert len(gaps) == 1  # the whole window


# --------------------------------------------------------------------------------------------
# Window labels (Phase 9 approval polish: overlapping excursion labels on SHP-2026-000049)
# --------------------------------------------------------------------------------------------

HOUR = pd.Timedelta(hours=1)
T0 = pd.Timestamp("2026-10-19 00:00", tz="UTC")


def test_isolated_labels_all_stay_in_the_first_lane() -> None:
    starts = [T0, T0 + 10 * HOUR, T0 + 20 * HOUR]
    assert charts.label_lanes(starts, 30 * HOUR) == [0, 0, 0]


def test_close_labels_step_down_one_lane_each() -> None:
    starts = [T0 + 11 * HOUR, T0 + 12.8 * HOUR, T0 + 14.7 * HOUR, T0 + 16.5 * HOUR]
    assert charts.label_lanes(starts, 37 * HOUR) == [0, 1, 2, 3]


def test_a_lane_is_reused_once_its_label_has_ended() -> None:
    # width = 0.18 x 10 h = 1.8 h: the third label starts after the first one ends
    starts = [T0, T0 + HOUR, T0 + 2 * HOUR]
    assert charts.label_lanes(starts, 10 * HOUR) == [0, 1, 0]


def test_lanes_follow_time_order_not_input_order() -> None:
    starts = [T0 + HOUR, T0]
    assert charts.label_lanes(starts, 10 * HOUR) == [1, 0]


def test_lanes_wrap_after_the_maximum_and_survive_a_zero_span() -> None:
    starts = [T0] * (charts.MAX_LANES + 1)
    assert charts.label_lanes(starts, pd.Timedelta(0)) == [*range(charts.MAX_LANES), 0]
    assert charts.label_lanes([], 10 * HOUR) == []


def test_close_excursion_labels_are_staggered_and_readable() -> None:
    # a label covers 0.18 x 5 h = 54 min, so four windows 10 min apart need four lanes
    excursions = _excursions((14, 14), (15, 15), (16, 16), (17, 17))
    figure, _, _ = _chart(readings_frame(LOG, "DEV-000001", WINDOW_TIMES), excursions)
    spec = spec_of(figure)
    labels = window_labels(spec, "EXCR-")
    assert [a["x"] for a in labels] == [x0 for x0, _ in _expected_windows(excursions)]
    assert len({a["y"] for a in labels}) == 4
    assert all(a["yref"] == "paper" and 0 < a["y"] <= 1 for a in labels)
    assert_labels_do_not_overlap(spec, "EXCR-")


def test_far_apart_excursion_labels_share_the_top_lane() -> None:
    excursions = _excursions((2, 2), (28, 28))
    figure, _, _ = _chart(readings_frame(LOG, "DEV-000001", WINDOW_TIMES), excursions)
    labels = window_labels(spec_of(figure), "EXCR-")
    assert [a["y"] for a in labels] == [1.0, 1.0]


def test_gap_labels_are_staggered_from_the_bottom() -> None:
    times = list(WINDOW_TIMES)
    kept = [t for i, t in enumerate(times) if i not in {3, 4, 7, 8}]
    readings = readings_frame(LOG, "DEV-000001", pd.DatetimeIndex(kept))
    figure, _, gaps = _chart(readings, _excursions())
    spec = spec_of(figure)
    labels = window_labels(spec, "gap")
    assert len(labels) == len(gaps) == 2
    assert sorted(a["y"] for a in labels) == [0.0, charts.LANE_STEP]
    assert all(a["yanchor"] == "bottom" for a in labels)
    assert_labels_do_not_overlap(spec, "gap")
