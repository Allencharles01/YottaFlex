"""Pure table transforms behind the viewer pages (DataFrame in, DataFrame or dict out).

Nothing here reads files or imports streamlit. The functions only reshape what the pipeline
already decided. Two of them recompute something for display and are tested against the
engine's own numbers: :func:`gap_spans` (its largest gap equals ``max_gap_min`` of every
decision) and :func:`custody_chain` (a shipment has a broken link exactly when the engine
flagged CUSTODY_GAP).
"""

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd

from coldchain.enums import ComplianceStatus, CustodyEventType, ReasonCode
from coldchain.schemas import LIST_SEPARATOR

RELEASED_STATUSES = (ComplianceStatus.COMPLIANT, ComplianceStatus.COMPLIANT_WITH_WARNINGS)
"""Statuses counted as compliant on the Overview page (both lead to RELEASE)."""

GAP_REASON_CODES = (
    ReasonCode.CUSTODY_GAP,
    ReasonCode.DATA_GAP,
    ReasonCode.LOW_COVERAGE,
    ReasonCode.LOGGER_NOT_ASSIGNED,
)
"""Engine reason codes listed on the Custody & Data Gaps page."""

ANSWER_KEY_PREFIX = "answer_key_"
"""Column prefix for every value that comes from ``ground_truth/``."""


class ReadingClass(StrEnum):
    """How a reading relates to its shipment's logger assignment (compliance_rules.md §1.2)."""

    TRUSTED = "TRUSTED"
    WRONG_DEVICE = "WRONG_DEVICE"
    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def split_codes(value: Any) -> list[str]:
    """Split a ``|``-separated list cell into its items (an empty or null cell gives ``[]``)."""
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA:
        return []
    return [item for item in str(value).split(LIST_SEPARATOR) if item]


def one_row(frame: pd.DataFrame, column: str, value: str) -> pd.Series | None:
    """Return the first row where ``column == value``, or ``None`` if there is none."""
    rows = frame[frame[column] == value]
    return None if rows.empty else rows.iloc[0]


def with_party_names(
    frame: pd.DataFrame, parties: pd.DataFrame, columns: Iterable[str]
) -> pd.DataFrame:
    """Add a ``<column>_name`` column with the party name next to each party-ID column."""
    names = parties.set_index("party_id")["name"]
    result = frame.copy()
    for column in columns:
        position = result.columns.get_loc(column) + 1
        result.insert(position, column.removesuffix("_id") + "_name", result[column].map(names))
    return result


def field_table(
    record: pd.Series | dict[str, Any] | None, value_label: str = "value"
) -> pd.DataFrame:
    """Show one record as a two-column ``field`` / value table, every value as text.

    Args:
        record: A row (Series or dict); ``None`` gives an empty table.
        value_label: Header of the value column.

    Returns:
        One row per field; nulls become an empty string.
    """
    items = {} if record is None else dict(record)
    values = ["" if pd.isna(v) else str(v) for v in items.values()]
    return pd.DataFrame({"field": list(items), value_label: values}, dtype="str")


# --------------------------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------------------------


def kpis(decisions: pd.DataFrame, reports: pd.DataFrame) -> dict[str, float | int]:
    """Compute the Overview tiles.

    Args:
        decisions: ``compliance_decisions``.
        reports: ``excursion_reports``.

    Returns:
        ``shipments``, ``compliant`` (COMPLIANT + COMPLIANT_WITH_WARNINGS), ``compliant_pct``
        (0.0 for no shipments), ``non_compliant``, ``inconclusive`` and ``excursions``.
    """
    status = decisions["status"]
    total = len(decisions)
    compliant = int(status.isin([s.value for s in RELEASED_STATUSES]).sum())
    return {
        "shipments": total,
        "compliant": compliant,
        "compliant_pct": round(100.0 * compliant / total, 1) if total else 0.0,
        "non_compliant": int((status == ComplianceStatus.NON_COMPLIANT.value).sum()),
        "inconclusive": int((status == ComplianceStatus.INCONCLUSIVE.value).sum()),
        "excursions": len(reports),
    }


def status_counts(decisions: pd.DataFrame) -> pd.DataFrame:
    """Count shipments per status, in severity order, including statuses with no shipment.

    Returns:
        Columns ``status``, ``shipments`` and ``share_pct`` (one decimal).
    """
    order = [s.value for s in ComplianceStatus]
    counts = decisions["status"].value_counts().reindex(order, fill_value=0).astype("int64")
    total = int(counts.sum())
    share = (100.0 * counts / total).round(1) if total else counts.astype("float64") * 0.0
    return pd.DataFrame(
        {"status": order, "shipments": counts.to_numpy(), "share_pct": share.to_numpy()}
    )


def reason_code_counts(decisions: pd.DataFrame) -> pd.DataFrame:
    """Count shipments per reason code (a shipment counts once per code), most frequent first.

    Returns:
        Columns ``reason_code`` and ``shipments``; codes never raised are left out.
    """
    codes = decisions["reason_codes"].map(split_codes).explode().dropna()
    counts = codes.value_counts()
    frame = pd.DataFrame({"reason_code": counts.index.astype(str), "shipments": counts.to_numpy()})
    return frame.sort_values(["shipments", "reason_code"], ascending=[False, True]).reset_index(
        drop=True
    )


# --------------------------------------------------------------------------------------------
# Shipment details
# --------------------------------------------------------------------------------------------


def shipment_lots_detail(
    shipment_id: str, shipment_lots: pd.DataFrame, lots: pd.DataFrame, products: pd.DataFrame
) -> pd.DataFrame:
    """Return the lots of a shipment with their product (name, category, policy code)."""
    rows = shipment_lots[shipment_lots["shipment_id"] == shipment_id]
    rows = rows.rename(columns={"quantity_units": "shipped_units"}).drop(columns="shipment_id")
    rows = rows.merge(lots, on="lot_id", how="left").merge(
        products[["product_id", "name", "category", "policy_code"]], on="product_id", how="left"
    )
    return (
        rows.rename(columns={"name": "product_name"}).sort_values("lot_id").reset_index(drop=True)
    )


def status_history(
    raw_events: pd.DataFrame, engine_events: pd.DataFrame, shipment_id: str
) -> pd.DataFrame:
    """Return a shipment's whole status history, the raw lifecycle then the engine's events.

    Returns:
        The status-event columns plus ``source`` (``raw`` or ``compliance engine``), sorted by
        ``sequence``.
    """
    raw = raw_events[raw_events["shipment_id"] == shipment_id].assign(source="raw")
    engine = engine_events[engine_events["shipment_id"] == shipment_id].assign(
        source="compliance engine"
    )
    history = pd.concat([raw, engine], ignore_index=True)
    return history.sort_values("sequence", kind="stable").reset_index(drop=True)


def assignment_window(
    shipment_id: str,
    shipments: pd.DataFrame,
    loggers: pd.DataFrame,
    devices: pd.DataFrame,
    default_interval_min: int,
) -> dict[str, Any]:
    """Describe a shipment's logger window as the engine defines it (compliance_rules.md §1.2).

    The window is the logger assignment ``[assigned_at, unassigned_at]`` with the device's
    reading interval. Without an assignment it falls back to the actual departure and arrival,
    the config interval and no device (DECISIONS.md #10, #59).

    Returns:
        ``logger_id``, ``device_id`` (``None`` without an assignment), ``has_logger``,
        ``window_start``, ``window_end``, ``interval_min``, ``uncertainty_c``, ``model``,
        ``calibrated_on`` and ``calibration_due_on``.

    Raises:
        KeyError: If the shipment does not exist.
    """
    shipment = one_row(shipments, "shipment_id", shipment_id)
    if shipment is None:
        raise KeyError(shipment_id)
    logger = one_row(loggers, "shipment_id", shipment_id)
    device = None if logger is None else one_row(devices, "device_id", logger["device_id"])
    window: dict[str, Any] = {
        "logger_id": None if logger is None else logger["logger_id"],
        "device_id": None if logger is None else logger["device_id"],
        "has_logger": logger is not None,
        "window_start": shipment["actual_departure_at"]
        if logger is None
        else logger["assigned_at"],
        "window_end": shipment["actual_arrival_at"] if logger is None else logger["unassigned_at"],
        "interval_min": default_interval_min,
        "uncertainty_c": None,
        "model": None,
        "calibrated_on": None,
        "calibration_due_on": None,
    }
    if device is not None:
        window.update(
            interval_min=int(device["reading_interval_min"]),
            uncertainty_c=float(device["uncertainty_c"]),
            model=device["model"],
            calibrated_on=device["calibrated_on"],
            calibration_due_on=device["calibration_due_on"],
        )
    return window


def classify_readings(readings: pd.DataFrame, window: dict[str, Any]) -> pd.DataFrame:
    """Label each reading TRUSTED, WRONG_DEVICE or OUTSIDE_WINDOW (compliance_rules.md §1.2).

    Args:
        readings: One shipment's readings.
        window: The shipment's :func:`assignment_window`.

    Returns:
        The readings with a ``reading_class`` column (:class:`ReadingClass` values).
    """
    inside = readings["recorded_at"].between(window["window_start"], window["window_end"])
    right_device = readings["device_id"] == window["device_id"]
    classes = np.where(
        ~inside,
        ReadingClass.OUTSIDE_WINDOW.value,
        np.where(right_device, ReadingClass.TRUSTED.value, ReadingClass.WRONG_DEVICE.value),
    )
    return readings.assign(reading_class=pd.Series(classes, index=readings.index, dtype="str"))


def gap_spans(
    trusted_times: pd.Series,
    window_start: pd.Timestamp,
    window_end: pd.Timestamp,
    interval_min: int,
    max_gap_min: int,
) -> pd.DataFrame:
    """Find the gaps between consecutive trusted readings (compliance_rules.md §1.4).

    As in the engine, the reading times are padded with virtual readings at
    ``window_start - interval`` and ``window_end + interval``, so a stream that starts late,
    ends early or is empty shows a gap too. A gap is any step longer than the interval.

    Args:
        trusted_times: ``recorded_at`` of the trusted readings.
        window_start: Start of the logger window.
        window_end: End of the logger window.
        interval_min: Reading interval in minutes.
        max_gap_min: The applied policy's ``max_reading_gap_min``.

    Returns:
        Columns ``gap_start`` (last reading before), ``gap_end`` (first reading after),
        ``gap_min`` and ``breach`` (``gap_min > max_gap_min``), sorted by ``gap_start``.
    """
    step = pd.Timedelta(minutes=interval_min)
    padding = pd.Series([window_start - step, window_end + step], dtype=trusted_times.dtype)
    times = pd.concat([trusted_times, padding], ignore_index=True).sort_values(ignore_index=True)
    deltas = times.diff()
    minutes = (deltas.dt.total_seconds() // 60).fillna(0).astype("int64")
    gaps = minutes > interval_min
    spans = pd.DataFrame(
        {
            "gap_start": times.shift()[gaps].to_numpy(),
            "gap_end": times[gaps].to_numpy(),
            "gap_min": minutes[gaps].to_numpy(),
        }
    )
    spans["breach"] = spans["gap_min"] > max_gap_min
    return spans.reset_index(drop=True)


def excursion_spans(reports: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    """Return the time span each excursion report covers: ``started_at`` to ``ended_at + i``.

    The span length equals ``duration_min`` (the duration rule in compliance_rules.md §1.5).
    """
    spans = reports.sort_values("started_at", kind="stable").reset_index(drop=True)
    return spans.assign(span_end=spans["ended_at"] + pd.Timedelta(minutes=interval_min))


def custody_chain(custody_events: pd.DataFrame, shipment: pd.Series) -> pd.DataFrame:
    """Return a shipment's custody events with each break in the chain explained.

    The rules are the engine's (compliance_rules.md §1.8): events numbered ``1..n``, each one
    starting from the previous event's receiving party, a PICKUP from the supplier first and a
    DELIVERY to the receiver last.

    Args:
        custody_events: The ``custody_events`` table.
        shipment: The shipment row (``shipment_id``, ``supplier_party_id``,
            ``receiver_party_id``).

    Returns:
        The events sorted by ``sequence`` with ``link_ok`` (bool) and ``problem`` (text, empty
        when the link is fine).
    """
    events = custody_events[custody_events["shipment_id"] == shipment["shipment_id"]]
    events = events.sort_values("sequence", kind="stable").reset_index(drop=True)
    position = pd.Series(np.arange(1, len(events) + 1), index=events.index)
    previous_to = events["to_party_id"].shift()
    is_first = position == 1
    is_last = position == len(events)
    problems = [
        (events["sequence"] != position, "sequence skips a number (an event is missing)"),
        (
            ~is_first & (events["from_party_id"] != previous_to),
            "from-party is not the previous event's to-party",
        ),
        (
            is_first
            & (
                (events["event_type"] != CustodyEventType.PICKUP.value)
                | (events["from_party_id"] != shipment["supplier_party_id"])
            ),
            "first event is not a PICKUP from the supplier",
        ),
        (
            is_last
            & (
                (events["event_type"] != CustodyEventType.DELIVERY.value)
                | (events["to_party_id"] != shipment["receiver_party_id"])
            ),
            "last event is not a DELIVERY to the receiver",
        ),
    ]
    text = pd.Series("", index=events.index, dtype="str")
    for mask, message in problems:
        text = text.where(~mask, text.where(text == "", text + "; ") + message)
    return events.assign(link_ok=text == "", problem=text)


# --------------------------------------------------------------------------------------------
# Excursion reports and gaps
# --------------------------------------------------------------------------------------------


def excursion_table(reports: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    """Return the excursion reports with their shipment's status and action."""
    return reports.merge(
        decisions[["shipment_id", "status", "decided_action"]], on="shipment_id", how="left"
    ).rename(columns={"status": "shipment_status"})


def report_readings(report_id: str, links: pd.DataFrame, readings: pd.DataFrame) -> pd.DataFrame:
    """Return the readings linked to one excursion report, in time order."""
    ids = links.loc[links["report_id"] == report_id, "reading_id"]
    linked = readings[readings["reading_id"].isin(ids)]
    return linked.sort_values("recorded_at", kind="stable").reset_index(drop=True)


def flagged_shipments(
    decisions: pd.DataFrame, codes: Iterable[ReasonCode] = GAP_REASON_CODES
) -> pd.DataFrame:
    """Return the shipments whose decision carries any of ``codes``, with one flag per code.

    Returns:
        ``shipment_id``, ``status``, one boolean column per code, ``coverage_pct``,
        ``max_gap_min`` and ``reason_codes``, sorted by shipment.
    """
    code_sets = decisions["reason_codes"].map(lambda v: set(split_codes(v)))
    flags = pd.DataFrame(
        {code.value: code_sets.map(lambda s, c=code.value: c in s) for code in codes},
        index=decisions.index,
    )
    keep = flags.any(axis=1)
    frame = pd.concat([decisions[["shipment_id", "status"]], flags], axis=1)[keep]
    frame = frame.join(decisions.loc[keep, ["coverage_pct", "max_gap_min", "reason_codes"]])
    return frame.sort_values("shipment_id").reset_index(drop=True)


# --------------------------------------------------------------------------------------------
# Data quality
# --------------------------------------------------------------------------------------------


def answer_key_comparison(expected: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    """Compare the answer key (ground truth) with the engine's decision for every shipment.

    Every column that comes from ``ground_truth/`` is prefixed ``answer_key_``. Reason codes
    are compared as sets (their order carries no meaning).

    Returns:
        ``shipment_id``, ``answer_key_scenario``, ``answer_key_issue_type``,
        ``answer_key_status``, ``engine_status``, ``answer_key_reason_codes``,
        ``engine_reason_codes``, ``status_match`` and ``reason_codes_match``.
    """
    key = expected[
        ["shipment_id", "scenario", "issue_type", "expected_status", "expected_reason_codes"]
    ].rename(
        columns={
            "scenario": f"{ANSWER_KEY_PREFIX}scenario",
            "issue_type": f"{ANSWER_KEY_PREFIX}issue_type",
            "expected_status": f"{ANSWER_KEY_PREFIX}status",
            "expected_reason_codes": f"{ANSWER_KEY_PREFIX}reason_codes",
        }
    )
    engine = decisions[["shipment_id", "status", "reason_codes"]].rename(
        columns={"status": "engine_status", "reason_codes": "engine_reason_codes"}
    )
    frame = key.merge(engine, on="shipment_id", how="outer")
    frame["status_match"] = frame[f"{ANSWER_KEY_PREFIX}status"] == frame["engine_status"]
    frame["reason_codes_match"] = frame[f"{ANSWER_KEY_PREFIX}reason_codes"].map(
        lambda v: set(split_codes(v))
    ) == frame["engine_reason_codes"].map(lambda v: set(split_codes(v)))
    columns = [
        "shipment_id",
        f"{ANSWER_KEY_PREFIX}scenario",
        f"{ANSWER_KEY_PREFIX}issue_type",
        f"{ANSWER_KEY_PREFIX}status",
        "engine_status",
        f"{ANSWER_KEY_PREFIX}reason_codes",
        "engine_reason_codes",
        "status_match",
        "reason_codes_match",
    ]
    return frame[columns].sort_values("shipment_id").reset_index(drop=True)


def confusion_frame(status_block: dict[str, Any]) -> pd.DataFrame:
    """Turn the evaluation report's ``status`` block into an expected-by-engine count table.

    Args:
        status_block: ``evaluation_report.json["status"]`` with ``labels`` and ``confusion``
            (``{expected: {engine: count}}``).

    Returns:
        One row per expected status (index), one column per engine status (incl.
        ``NO_DECISION`` when present).
    """
    confusion = status_block.get("confusion", {})
    frame = pd.DataFrame.from_dict(confusion, orient="index").fillna(0).astype("int64")
    frame.index.name = "answer key (rows) / engine (columns)"
    return frame


def check_table(summary: dict[str, Any], only_problems: bool) -> pd.DataFrame:
    """Return the validation checks of a summary as a table.

    Args:
        summary: ``validation_summary.json``.
        only_problems: Keep only the checks whose status is not PASS.

    Returns:
        Columns ``check_id``, ``tier``, ``category``, ``table``, ``status``, ``checked``,
        ``failed`` and ``description``.
    """
    columns = [
        "check_id",
        "tier",
        "category",
        "table",
        "status",
        "checked",
        "failed",
        "description",
    ]
    frame = pd.DataFrame(summary.get("checks", []), columns=columns)
    if only_problems:
        frame = frame[frame["status"] != "PASS"]
    return frame.reset_index(drop=True)


def reproducibility_overview(payload: dict[str, Any]) -> dict[str, Any]:
    """Summarise ``reproducibility_check.json`` for the Data Quality page.

    Returns:
        ``verdict``, ``seed``, ``control_seed``, ``files``, ``matching``, ``mismatching``,
        ``control_changed``, ``control_passed``, ``dataset_sha256_run1`` and
        ``dataset_sha256_run2`` (missing values are ``None``).
    """
    totals = payload.get("totals", {})
    control = payload.get("control", {})
    runs = payload.get("runs", {})
    return {
        "verdict": payload.get("verdict"),
        "seed": payload.get("seed"),
        "control_seed": payload.get("control_seed"),
        "files": totals.get("files"),
        "matching": totals.get("matching"),
        "mismatching": totals.get("mismatching"),
        "control_changed": control.get("changed"),
        "control_passed": control.get("passed"),
        "dataset_sha256_run1": runs.get("run1", {}).get("dataset_sha256"),
        "dataset_sha256_run2": runs.get("run2", {}).get("dataset_sha256"),
    }
