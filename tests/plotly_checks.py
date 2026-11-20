"""Assertions on a serialised temperature chart: what the browser actually receives.

Phase 9 review: the chart drew no readings and no excursion windows, and the x-axis showed
-1 … 6. plotly.js decides an axis type from the first trace that has any x value
(``getFirstNonEmptyTrace``), and a legend-only trace (``x=[None]``) came first, so the time axis
became linear and no date could be placed on it. These checks look at the JSON (as produced by
``plotly.io.to_json``, which streamlit sends) rather than at the Python figure object.
"""

import itertools
import json
from datetime import datetime
from typing import Any

import plotly.io as pio

from coldchain.viewer import charts

YEAR = 2026


def spec_of(figure_or_json: Any) -> dict[str, Any]:
    """Return the JSON spec of a figure (or parse a JSON string streamlit sent)."""
    text = figure_or_json if isinstance(figure_or_json, str) else pio.to_json(figure_or_json)
    return json.loads(text)


def as_date(value: Any) -> datetime:
    """Parse a plotly x value as a date; fail on anything else (numbers, ``None``)."""
    assert isinstance(value, str), f"x value is not a date string: {value!r}"
    parsed = datetime.fromisoformat(value)
    assert parsed.year == YEAR, value
    return parsed


def first_non_empty_x_trace(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Mimic plotly.js ``getFirstNonEmptyTrace`` for the ``x`` axis (plotly.js 4.1.1)."""
    for trace in spec["data"]:
        if trace.get("xaxis", "x") == "x" and (len(trace.get("x") or []) or trace.get("x0")):
            return trace
    return None


def trace(spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Return the trace with this legend name, or ``None``."""
    return next((t for t in spec["data"] if t.get("name") == name), None)


def assert_date_chart(
    spec: dict[str, Any],
    trusted_count: int,
    reading_count: int,
    excursions: list[tuple[str, str]],
) -> None:
    """Check that every time value of a temperature chart reaches plotly.js as a date.

    Args:
        spec: The serialised figure.
        trusted_count: Trusted readings (points on the readings line).
        reading_count: All readings (line + untrusted crosses).
        excursions: Expected ``(x0, x1)`` of each excursion window, in order.
    """
    assert spec["layout"]["xaxis"]["type"] == "date"

    line = trace(spec, "Readings (trusted)")
    assert line is not None
    assert len(line["x"]) == len(line["y"]) == trusted_count
    times = [as_date(x) for x in line["x"]]
    assert times == sorted(times)

    points = [t for t in spec["data"] if t.get("x") != [None]]
    assert sum(len(t["x"]) for t in points) == reading_count
    for t in points:
        for x in t["x"]:
            as_date(x)

    if reading_count:
        assert first_non_empty_x_trace(spec)["x"][0] is not None, "a legend-only trace is first"

    shapes = spec["layout"].get("shapes", [])
    red = [s for s in shapes if s.get("fillcolor") == charts.EXCURSION_COLOR]
    assert [(s["x0"], s["x1"]) for s in red] == excursions
    for shape in red:
        assert shape["xref"] == "x"
        assert as_date(shape["x0"]) < as_date(shape["x1"])
    for shape in shapes:
        if shape.get("xref") == "x":
            as_date(shape["x0"])
            as_date(shape["x1"])
    for note in spec["layout"].get("annotations", []):
        if note.get("xref", "x") == "x":
            as_date(note["x"])


def window_labels(spec: dict[str, Any], prefix: str) -> list[dict[str, Any]]:
    """Return the annotations whose text starts with ``prefix`` (e.g. ``EXCR-`` or ``gap``)."""
    return [a for a in spec["layout"].get("annotations", []) if a["text"].startswith(prefix)]


def assert_labels_do_not_overlap(spec: dict[str, Any], prefix: str) -> None:
    """Labels at the same height must be at least one label width apart in time.

    The width is ``charts.LABEL_WIDTH_FRACTION`` of the logger window (the two dotted lines).
    """
    edges = sorted(
        as_date(s["x0"])
        for s in spec["layout"].get("shapes", [])
        if s["type"] == "line" and s.get("line", {}).get("dash") == "dot"
    )
    width = (edges[-1] - edges[0]) * charts.LABEL_WIDTH_FRACTION
    by_height: dict[float, list[datetime]] = {}
    for label in window_labels(spec, prefix):
        by_height.setdefault(round(label["y"], 6), []).append(as_date(label["x"]))
    for height, starts in by_height.items():
        starts.sort()
        for left, right in itertools.pairwise(starts):
            assert right - left >= width, f"labels at y={height} overlap: {left} and {right}"
