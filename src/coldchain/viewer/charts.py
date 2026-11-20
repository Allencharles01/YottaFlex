"""Plotly figures for the viewer (pure: tables in, ``plotly.graph_objects.Figure`` out).

Colours: the four compliance statuses use a fixed status palette in severity order (good,
warning, serious, critical) and always appear next to their text label. The temperature chart
draws the readings in one blue, the allowed band in green, excursions in red and gaps in grey.

Every axis type is set explicitly. plotly.js otherwise guesses an axis type from the *first*
trace that has any values on it, and a legend-only trace (``x=[None]``) drawn first made the
time axis linear, so no date could be placed on it (Phase 9 review, DECISIONS.md #129). Every
timestamp is passed as naive-UTC ISO text (:func:`plotly_time`), so neither plotly.py nor
plotly.js applies a time-zone conversion.
"""

from typing import Any

import pandas as pd
import plotly.graph_objects as go

from coldchain.enums import ComplianceStatus
from coldchain.viewer.views import ReadingClass

STATUS_COLORS: dict[str, str] = {
    ComplianceStatus.COMPLIANT.value: "#0ca30c",
    ComplianceStatus.COMPLIANT_WITH_WARNINGS.value: "#fab219",
    ComplianceStatus.INCONCLUSIVE.value: "#ec835a",
    ComplianceStatus.NON_COMPLIANT.value: "#d03b3b",
}
"""Status → colour (good / warning / serious / critical)."""

READING_COLOR = "#2a78d6"
UNTRUSTED_COLOR = "#8a8984"
BAND_COLOR = "#0ca30c"
EXCURSION_COLOR = "#d03b3b"
GAP_COLOR = "#8a8984"
LIMIT_COLOR = "#52514e"

_FONT = {"size": 13}
_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S"

LABEL_WIDTH_FRACTION = 0.18
"""Share of the chart's time span a window label is assumed to cover (about 26 characters)."""
LANE_STEP = 0.065
"""Vertical distance between label lanes, as a fraction of the plot height."""
MAX_LANES = 6
"""Labels beyond this many lanes wrap back to the first lane."""


def label_lanes(starts: list[pd.Timestamp], span: pd.Timedelta) -> list[int]:
    """Assign each window label a lane so labels that would overlap sit at different heights.

    A label is assumed to cover ``[start, start + LABEL_WIDTH_FRACTION x span)``. Labels are
    placed in time order, each in the lowest lane whose previous label has ended by its start
    (greedy interval colouring), so isolated labels all stay in lane 0.

    Args:
        starts: Label anchor times (any order).
        span: Time span of the x-axis (non-positive spans are treated as one minute).

    Returns:
        The lane (0 = outermost) of each label, in the order of ``starts``.
    """
    width = max(span, pd.Timedelta(minutes=1)) * LABEL_WIDTH_FRACTION
    lanes = [0] * len(starts)
    lane_ends: list[pd.Timestamp] = []
    for index in sorted(range(len(starts)), key=lambda i: (starts[i], i)):
        start = starts[index]
        lane = next((n for n, end in enumerate(lane_ends) if end <= start), len(lane_ends))
        if lane == len(lane_ends):
            lane_ends.append(start)
        lane_ends[lane] = start + width
        lanes[index] = lane % MAX_LANES
    return lanes


def _window_labels(
    figure: go.Figure,
    starts: list[pd.Timestamp],
    texts: list[str],
    span: pd.Timedelta,
    *,
    top: bool,
) -> None:
    """Add window labels inside the top (or bottom) edge of the plot, staggered by lane."""
    for start, text, lane in zip(starts, texts, label_lanes(starts, span), strict=True):
        offset = lane * LANE_STEP
        figure.add_annotation(
            x=plotly_time(start),
            y=1.0 - offset if top else offset,
            xref="x",
            yref="paper",
            text=text,
            showarrow=False,
            xanchor="left",
            yanchor="top" if top else "bottom",
            font={"size": 11},
        )


def plotly_time(value: pd.Timestamp) -> str:
    """Return a timestamp as naive-UTC ISO text, e.g. ``2026-04-14T02:00:00``.

    Raises:
        ValueError: If ``value`` is missing (``NaT``) or not time-zone aware.
    """
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(f"expected a time-zone-aware timestamp, got {value!r}")
    return stamp.tz_convert("UTC").strftime(_TIME_FORMAT)


def plotly_times(values: pd.Series) -> list[str]:
    """Return a time-zone-aware timestamp column as naive-UTC ISO text (see :func:`plotly_time`)."""
    if values.dt.tz is None:
        raise ValueError("expected a time-zone-aware timestamp column")
    return values.dt.tz_convert("UTC").dt.strftime(_TIME_FORMAT).tolist()


def status_bar(counts: pd.DataFrame) -> go.Figure:
    """Horizontal bar chart of shipments per status (from :func:`views.status_counts`)."""
    figure = go.Figure(
        go.Bar(
            x=counts["shipments"],
            y=counts["status"],
            orientation="h",
            marker={"color": [STATUS_COLORS[s] for s in counts["status"]], "cornerradius": 4},
            text=[
                f"{n} ({p:g} %)"
                for n, p in zip(counts["shipments"], counts["share_pct"], strict=True)
            ],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x} shipments<extra></extra>",
        )
    )
    figure.update_layout(
        font=_FONT,
        height=260,
        margin={"l": 10, "r": 80, "t": 10, "b": 30},
        yaxis={"type": "category", "autorange": "reversed", "title": None},
        xaxis={
            "type": "linear",
            "title": "Shipments",
            "showgrid": True,
            "gridcolor": "rgba(128,128,128,0.2)",
        },
        bargap=0.35,
        showlegend=False,
    )
    return figure


def reason_bar(counts: pd.DataFrame) -> go.Figure:
    """Horizontal bar chart of shipments per reason code (from :func:`views.reason_code_counts`)."""
    figure = go.Figure(
        go.Bar(
            x=counts["shipments"],
            y=counts["reason_code"],
            orientation="h",
            marker={"color": READING_COLOR, "cornerradius": 4},
            text=counts["shipments"],
            textposition="outside",
            cliponaxis=False,
            hovertemplate="%{y}: %{x} shipments<extra></extra>",
        )
    )
    figure.update_layout(
        font=_FONT,
        height=max(200, 34 * len(counts) + 60),
        margin={"l": 10, "r": 50, "t": 10, "b": 30},
        yaxis={"type": "category", "autorange": "reversed", "title": None},
        xaxis={"type": "linear", "title": "Shipments", "gridcolor": "rgba(128,128,128,0.2)"},
        showlegend=False,
    )
    return figure


def _legend_only(figure: go.Figure, name: str, **line: Any) -> None:
    """Add a legend entry for a shape type (a trace with no visible point).

    Called only after the data traces, and the x-axis type is explicit, so it can never decide
    the axis type.
    """
    figure.add_trace(
        go.Scatter(x=[None], y=[None], mode="lines", name=name, line=line, hoverinfo="skip")
    )


def _readings_trace(readings: pd.DataFrame, **style: Any) -> go.Scatter:
    """Return a scatter trace of readings with naive-UTC ISO x values."""
    return go.Scatter(
        x=plotly_times(readings["recorded_at"]),
        y=readings["temperature_c"].tolist(),
        customdata=readings[["reading_id", "device_id"]].to_numpy().tolist(),
        **style,
    )


def temperature_chart(
    readings: pd.DataFrame,
    policy: pd.Series,
    excursions: pd.DataFrame,
    gaps: pd.DataFrame,
    window: dict[str, Any],
) -> go.Figure:
    """Line chart of one shipment's readings against its applied policy.

    Args:
        readings: The shipment's readings with ``reading_class``
            (:func:`views.classify_readings`).
        policy: The applied ``threshold_policies`` row.
        excursions: The engine's excursion reports with ``span_end``
            (:func:`views.excursion_spans`).
        gaps: :func:`views.gap_spans` of the trusted readings.
        window: :func:`views.assignment_window` of the shipment.

    Returns:
        A figure on a date x-axis with the readings line (trusted readings), grey crosses for
        untrusted readings, the green ``min_c``-``max_c`` band, dashed absolute limits, red
        excursion windows, grey gap windows (darker when the gap breaches the policy) and the
        logger window edges.
    """
    figure = go.Figure()
    classes = readings["reading_class"]
    figure.add_trace(
        _readings_trace(
            readings[classes == ReadingClass.TRUSTED.value],
            mode="lines",
            name="Readings (trusted)",
            line={"color": READING_COLOR, "width": 2},
            hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>%{y:.2f} °C<br>%{customdata[0]}"
            "<extra></extra>",
            connectgaps=False,
        )
    )
    for reading_class, name in (
        (ReadingClass.WRONG_DEVICE, "Untrusted: wrong device"),
        (ReadingClass.OUTSIDE_WINDOW, "Ignored: outside logger window"),
    ):
        subset = readings[classes == reading_class.value]
        if subset.empty:
            continue
        figure.add_trace(
            _readings_trace(
                subset,
                mode="markers",
                name=name,
                marker={"color": UNTRUSTED_COLOR, "size": 8, "symbol": "x"},
                hovertemplate="%{x|%Y-%m-%d %H:%M} UTC<br>%{y:.2f} °C<br>%{customdata[0]}"
                "<br>device %{customdata[1]}<extra></extra>",
            )
        )

    figure.add_hrect(
        y0=policy["min_c"], y1=policy["max_c"], fillcolor=BAND_COLOR, opacity=0.13, line_width=0
    )
    _legend_only(
        figure,
        f"Allowed range {policy['min_c']:g} to {policy['max_c']:g} °C",
        color=BAND_COLOR,
        width=10,
    )
    for value, label in ((policy["abs_min_c"], "abs min"), (policy["abs_max_c"], "abs max")):
        figure.add_hline(
            y=value,
            line={"color": LIMIT_COLOR, "dash": "dash", "width": 1},
            annotation_text=f"{label} {value:g} °C",
            annotation_position="top left" if label == "abs max" else "bottom left",
            annotation_font_size=11,
        )
    _legend_only(figure, "Absolute limits", color=LIMIT_COLOR, dash="dash", width=1)

    span = pd.Timestamp(window["window_end"]) - pd.Timestamp(window["window_start"])
    for row in excursions.itertuples():
        figure.add_vrect(
            x0=plotly_time(row.started_at),
            x1=plotly_time(row.span_end),
            fillcolor=EXCURSION_COLOR,
            opacity=0.22,
            line_width=0,
        )
    _window_labels(
        figure,
        list(excursions["started_at"]),
        [f"{r.report_id} · {r.duration_min} min" for r in excursions.itertuples()],
        span,
        top=True,
    )
    if not excursions.empty:
        _legend_only(figure, "Excursion (engine report)", color=EXCURSION_COLOR, width=10)

    for row in gaps.itertuples():
        figure.add_vrect(
            x0=plotly_time(row.gap_start),
            x1=plotly_time(row.gap_end),
            fillcolor=GAP_COLOR,
            opacity=0.40 if row.breach else 0.18,
            line_width=0,
        )
    _window_labels(
        figure,
        list(gaps["gap_start"]),
        [f"gap {r.gap_min} min" + (" (breach)" if r.breach else "") for r in gaps.itertuples()],
        span,
        top=False,
    )
    if not gaps.empty:
        _legend_only(figure, "Gap in trusted readings", color=GAP_COLOR, width=10)

    for edge, label, anchor in (
        (window["window_start"], "logger window start", "left"),
        (window["window_end"], "logger window end", "right"),
    ):
        figure.add_vline(
            x=plotly_time(edge), line={"color": LIMIT_COLOR, "dash": "dot", "width": 1}
        )
        figure.add_annotation(
            x=plotly_time(edge),
            y=1.0,
            xref="x",
            yref="paper",
            text=label,
            showarrow=False,
            xanchor=anchor,
            yanchor="bottom",
            font={"size": 10, "color": LIMIT_COLOR},
        )

    figure.update_layout(
        font=_FONT,
        height=480,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
        hovermode="closest",
        yaxis={"type": "linear", "title": "Temperature (°C)", "gridcolor": "rgba(128,128,128,0.2)"},
        xaxis={"type": "date", "title": "Recorded at (UTC)", "showgrid": False},
        legend={"orientation": "h", "yanchor": "top", "y": -0.18, "x": 0},
    )
    return figure
