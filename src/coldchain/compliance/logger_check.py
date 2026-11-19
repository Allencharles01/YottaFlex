"""Logger assignment, trusted readings and calibration (compliance_rules.md §1.2 and §1.7).

* The **window** *W* of a shipment is its logger assignment ``[assigned_at, unassigned_at]``.
  Without an assignment it falls back to ``[actual_departure_at, actual_arrival_at]`` with the
  config ``reading_interval_min`` and no device (DECISIONS.md #10, #59).
* A reading is **trusted** when it is on the shipment's logger, inside *W* (both ends
  inclusive) and from the assigned device. A wrong-device reading inside *W* is untrusted and
  adds LOGGER_NOT_ASSIGNED; readings outside *W* are ignored for every rule (DECISIONS.md #60).
* CALIBRATION_EXPIRED when the device's calibration does not cover the whole window.
"""

import pandas as pd

from coldchain.enums import ReasonCode
from coldchain.errors import ComplianceError

WINDOW_COLUMNS = (
    "shipment_id",
    "logger_id",
    "device_id",
    "has_logger",
    "window_start",
    "window_end",
    "interval_min",
    "uncertainty_c",
    "calibrated_on",
    "calibration_due_on",
)
"""Columns of :func:`assignment_windows` (one row per shipment)."""

READING_COLUMNS = ("shipment_id", "logger_id", "reading_id", "recorded_at", "temperature_c")
"""Columns of the trusted readings passed to the gap and excursion rules."""


def assignment_windows(
    shipments: pd.DataFrame,
    loggers: pd.DataFrame,
    devices: pd.DataFrame,
    default_interval_min: int,
) -> pd.DataFrame:
    """Return the evaluation window, device and calibration of every shipment.

    Args:
        shipments: The ``shipments`` table.
        loggers: The ``loggers`` table (at most one per shipment, enforced by its schema).
        devices: The ``devices`` table.
        default_interval_min: Reading interval used when a shipment has no logger.

    Returns:
        One row per shipment (``shipments`` order) with :data:`WINDOW_COLUMNS`. Device columns
        are null and ``has_logger`` is ``False`` for a shipment without an assignment.

    Raises:
        ComplianceError: If a logger references a device that is not in ``devices``.
    """
    frame = shipments[["shipment_id", "actual_departure_at", "actual_arrival_at"]].merge(
        loggers[["shipment_id", "logger_id", "device_id", "assigned_at", "unassigned_at"]],
        on="shipment_id",
        how="left",
        validate="one_to_one",
    )
    frame = frame.merge(
        devices[
            [
                "device_id",
                "reading_interval_min",
                "uncertainty_c",
                "calibrated_on",
                "calibration_due_on",
            ]
        ],
        on="device_id",
        how="left",
        validate="many_to_one",
        indicator=True,
    )
    frame["has_logger"] = frame["logger_id"].notna()
    unknown = frame["has_logger"] & (frame["_merge"] != "both")
    if unknown.any():
        broken = frame.loc[unknown, ["logger_id", "device_id"]].to_dict("records")
        raise ComplianceError(f"Loggers reference unknown devices: {broken}")
    has = frame["has_logger"]
    frame["window_start"] = frame["assigned_at"].where(has, frame["actual_departure_at"])
    frame["window_end"] = frame["unassigned_at"].where(has, frame["actual_arrival_at"])
    frame["interval_min"] = (
        frame["reading_interval_min"].where(has, default_interval_min).astype("int64")
    )
    return frame[list(WINDOW_COLUMNS)].reset_index(drop=True)


def screen_readings(readings: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Attach every in-window reading to its shipment and mark whether it is trusted.

    Only readings on a shipment's assigned logger and inside its window are kept; everything
    else is ignored. ``trusted`` is ``True`` when the reading's device is the assigned one.

    Args:
        readings: The ``temperature_readings`` table.
        windows: Output of :func:`assignment_windows`.

    Returns:
        :data:`READING_COLUMNS` plus ``device_id`` and ``trusted``, sorted by
        (``logger_id``, ``recorded_at``).
    """
    columns = ["shipment_id", "logger_id", "device_id", "window_start", "window_end"]
    assigned = windows.loc[windows["has_logger"], columns].rename(
        columns={"device_id": "assigned_device_id"}
    )
    screened = readings[["reading_id", "logger_id", "device_id", "recorded_at", "temperature_c"]]
    screened = screened.merge(assigned, on="logger_id", how="inner", validate="many_to_one")
    inside = (screened["recorded_at"] >= screened["window_start"]) & (
        screened["recorded_at"] <= screened["window_end"]
    )
    screened = screened[inside].copy()
    screened["trusted"] = screened["device_id"] == screened["assigned_device_id"]
    screened = screened.sort_values(["logger_id", "recorded_at"], kind="stable")
    return screened[[*READING_COLUMNS, "device_id", "trusted"]].reset_index(drop=True)


def trusted_readings(screened: pd.DataFrame) -> pd.DataFrame:
    """Keep only the trusted readings of :func:`screen_readings` (:data:`READING_COLUMNS`)."""
    return screened.loc[screened["trusted"], list(READING_COLUMNS)].reset_index(drop=True)


def logger_flags(windows: pd.DataFrame, screened: pd.DataFrame) -> pd.DataFrame:
    """Evaluate LOGGER_NOT_ASSIGNED and CALIBRATION_EXPIRED for every shipment.

    Args:
        windows: Output of :func:`assignment_windows`.
        screened: Output of :func:`screen_readings`.

    Returns:
        Boolean frame indexed by ``shipment_id`` with one column per reason code.
    """
    index = pd.Index(windows["shipment_id"], name="shipment_id")
    foreign = (~screened["trusted"]).groupby(screened["shipment_id"]).any()
    foreign = foreign.reindex(index, fill_value=False)
    not_assigned = ~windows["has_logger"].to_numpy() | foreign.to_numpy(dtype=bool)

    # Calendar dates as datetime64 (NaT for a shipment without a device → comparison is False).
    calibrated = pd.to_datetime(windows["calibrated_on"])
    due = pd.to_datetime(windows["calibration_due_on"])
    first_day = windows["window_start"].dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
    last_day = windows["window_end"].dt.tz_convert("UTC").dt.tz_localize(None).dt.normalize()
    expired = windows["has_logger"] & ((due < last_day) | (calibrated > first_day))

    return pd.DataFrame(
        {
            ReasonCode.LOGGER_NOT_ASSIGNED.value: not_assigned,
            ReasonCode.CALIBRATION_EXPIRED.value: expired.to_numpy(dtype=bool),
        },
        index=index,
    )
