"""Temperature rules on trusted readings (compliance_rules.md §1.5 and §1.6).

* **Out of range:** ``t > max_c`` (HIGH) or ``t < min_c`` (LOW); a reading at a limit is in range.
* **Excursion:** a maximal run of consecutive trusted readings of one logger assignment that are
  out of range in the same direction. Runs are computed per ``logger_id`` (never per device),
  so back-to-back shipments on one device are never merged. An in-range reading or a change of
  direction ends a run; a gap does not (DECISIONS.md #9).
* ``duration_min = (ended_at - started_at) + i``; breach type: ABSOLUTE_LIMIT_BREACH if any
  reading is beyond an absolute limit, else SINGLE_DURATION_BREACH if the duration exceeds
  ``max_single_excursion_min``, else WITHIN_ALLOWANCE.
* **Report IDs** ``EXCR-2026-000001, …`` are numbered over the whole run in order of
  (``shipment_id``, ``started_at``, ``direction``).
* **NEAR_LIMIT_UNCERTAIN:** any trusted reading with ``round(|t - limit|, 2) ≤ uncertainty_c``.

Everything is vectorised: run boundaries come from shifted comparisons and a cumulative sum.
"""

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.enums import BreachType, ExcursionDirection, ReasonCode, TableName
from coldchain.schemas import LIST_SEPARATOR, PRIMARY_KEYS, SCHEMAS

REPORT_COLUMNS = tuple(SCHEMAS[TableName.EXCURSION_REPORTS].columns)
"""Columns of the ``excursion_reports`` table, in schema order."""

BRIDGE_COLUMNS = PRIMARY_KEYS[TableName.EXCURSION_REPORT_READINGS]
"""Columns of the ``excursion_report_readings`` bridge table."""

_ORDER = ["shipment_id", "started_at", "direction"]


def _directions(readings: pd.DataFrame) -> np.ndarray:
    """HIGH / LOW per reading, or an empty string when the reading is in range."""
    t = readings["temperature_c"]
    return np.select(
        [t > readings["max_c"], t < readings["min_c"]],
        [ExcursionDirection.HIGH.value, ExcursionDirection.LOW.value],
        default="",
    )


def detect_excursions(
    trusted: pd.DataFrame, windows: pd.DataFrame, policies: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Find every excursion and build its report and reading links.

    Args:
        trusted: Trusted readings (``shipment_id``, ``logger_id``, ``reading_id``,
            ``recorded_at``, ``temperature_c``).
        windows: Per-shipment windows with ``interval_min``.
        policies: Applied policy per shipment
            (:func:`coldchain.compliance.policy.resolve_policies`).

    Returns:
        ``(excursion_reports, excursion_report_readings)`` with their schema columns, sorted by
        primary key.
    """
    limits = policies[
        [
            "shipment_id",
            "applied_policy_id",
            "min_c",
            "max_c",
            "abs_min_c",
            "abs_max_c",
            "max_single_excursion_min",
        ]
    ].merge(windows[["shipment_id", "interval_min"]], on="shipment_id", validate="one_to_one")
    readings = trusted.merge(limits, on="shipment_id", how="left", validate="many_to_one")
    readings = readings.sort_values(["logger_id", "recorded_at"], kind="stable", ignore_index=True)

    direction = pd.Series(_directions(readings), index=readings.index)
    same_logger = readings["logger_id"].eq(readings["logger_id"].shift())
    continues = same_logger & direction.eq(direction.shift())
    starts = direction.ne("") & ~continues
    readings["run"] = starts.cumsum()
    readings["direction"] = direction
    readings["beyond_absolute"] = (readings["temperature_c"] > readings["abs_max_c"]) | (
        readings["temperature_c"] < readings["abs_min_c"]
    )
    out = readings[direction.ne("")]
    if out.empty:
        return (
            pd.DataFrame(columns=list(REPORT_COLUMNS)),
            pd.DataFrame(columns=list(BRIDGE_COLUMNS)),
        )

    runs = out.groupby("run", sort=True).agg(
        shipment_id=("shipment_id", "first"),
        logger_id=("logger_id", "first"),
        policy_id=("applied_policy_id", "first"),
        direction=("direction", "first"),
        started_at=("recorded_at", "first"),
        ended_at=("recorded_at", "last"),
        high_peak=("temperature_c", "max"),
        low_peak=("temperature_c", "min"),
        reading_count=("reading_id", "size"),
        first_reading_id=("reading_id", "first"),
        last_reading_id=("reading_id", "last"),
        beyond_absolute=("beyond_absolute", "any"),
        interval_min=("interval_min", "first"),
        max_single_excursion_min=("max_single_excursion_min", "first"),
    )
    window = (runs["ended_at"] - runs["started_at"]).dt.total_seconds() // 60
    runs["duration_min"] = window.astype("int64") + runs["interval_min"].astype("int64")
    runs["peak_c"] = np.where(
        runs["direction"] == ExcursionDirection.HIGH.value, runs["high_peak"], runs["low_peak"]
    ).round(2)
    runs["breach_type"] = np.select(
        [runs["beyond_absolute"], runs["duration_min"] > runs["max_single_excursion_min"]],
        [BreachType.ABSOLUTE_LIMIT_BREACH.value, BreachType.SINGLE_DURATION_BREACH.value],
        default=BreachType.WITHIN_ALLOWANCE.value,
    )

    runs = runs.sort_values(_ORDER, kind="stable")
    numbers = pd.Series(np.arange(1, len(runs) + 1, dtype="int64"), index=runs.index)
    runs["report_id"] = ids.excursion_report_ids(numbers)
    reports = runs.reset_index(drop=True)[list(REPORT_COLUMNS)]

    bridge = pd.DataFrame(
        {
            "report_id": runs["report_id"].reindex(out["run"]).to_numpy(),
            "reading_id": out["reading_id"].to_numpy(),
        }
    )
    bridge = bridge.sort_values(list(BRIDGE_COLUMNS), kind="stable", ignore_index=True)
    return reports, bridge


def excursion_summary(reports: pd.DataFrame, policies: pd.DataFrame) -> pd.DataFrame:
    """Aggregate the excursion reports of every shipment into its totals and reason flags.

    Args:
        reports: The ``excursion_reports`` table.
        policies: Applied policy per shipment with ``max_cumulative_excursion_min``.

    Returns:
        Frame indexed by ``shipment_id`` (``policies`` order) with ``total_excursion_min``,
        ``excursion_report_ids`` (``|``-joined in report-ID order, null when none) and boolean
        ABSOLUTE_LIMIT_BREACH, SINGLE_DURATION_BREACH, CUMULATIVE_DURATION_BREACH and
        EXCURSION_WITHIN_ALLOWANCE columns.
    """
    index = pd.Index(policies["shipment_id"], name="shipment_id")
    ordered = reports.sort_values("report_id", kind="stable")
    by_ship = ordered.groupby("shipment_id")
    total = by_ship["duration_min"].sum().reindex(index, fill_value=0).astype("int64")
    report_ids = by_ship["report_id"].agg(LIST_SEPARATOR.join).reindex(index)

    def any_of(breach: BreachType) -> np.ndarray:
        hit = (ordered["breach_type"] == breach.value).groupby(ordered["shipment_id"]).any()
        return hit.reindex(index, fill_value=False).to_numpy(dtype=bool)

    cumulative_limit = policies.set_index("shipment_id")["max_cumulative_excursion_min"]
    return pd.DataFrame(
        {
            "total_excursion_min": total,
            "excursion_report_ids": report_ids.astype(object).where(report_ids.notna(), None),
            ReasonCode.ABSOLUTE_LIMIT_BREACH.value: any_of(BreachType.ABSOLUTE_LIMIT_BREACH),
            ReasonCode.SINGLE_DURATION_BREACH.value: any_of(BreachType.SINGLE_DURATION_BREACH),
            ReasonCode.CUMULATIVE_DURATION_BREACH.value: (
                total > cumulative_limit.loc[index]
            ).to_numpy(dtype=bool),
            ReasonCode.EXCURSION_WITHIN_ALLOWANCE.value: any_of(BreachType.WITHIN_ALLOWANCE),
        },
        index=index,
    )


def near_limit_flags(
    trusted: pd.DataFrame, windows: pd.DataFrame, policies: pd.DataFrame
) -> pd.Series:
    """NEAR_LIMIT_UNCERTAIN per shipment: a trusted reading within ``uncertainty_c`` of a limit.

    The distance to each limit is rounded to 2 decimals before the inclusive comparison
    (DECISIONS.md #15).

    Args:
        trusted: Trusted readings (``shipment_id``, ``temperature_c``).
        windows: Per-shipment windows with the device's ``uncertainty_c``.
        policies: Applied policy per shipment with ``min_c`` and ``max_c``.

    Returns:
        Boolean Series indexed by ``shipment_id`` (``windows`` order).
    """
    index = pd.Index(windows["shipment_id"], name="shipment_id")
    frame = (
        trusted[["shipment_id", "temperature_c"]]
        .merge(policies[["shipment_id", "min_c", "max_c"]], on="shipment_id")
        .merge(windows[["shipment_id", "uncertainty_c"]], on="shipment_id")
    )
    t = frame["temperature_c"].to_numpy()
    u = frame["uncertainty_c"].to_numpy(dtype=float)
    near = (np.round(np.abs(t - frame["min_c"].to_numpy()), 2) <= u) | (
        np.round(np.abs(t - frame["max_c"].to_numpy()), 2) <= u
    )
    flags = pd.Series(near, index=frame["shipment_id"].to_numpy()).groupby(level=0).any()
    return (
        flags.reindex(index, fill_value=False)
        .astype(bool)
        .rename(ReasonCode.NEAR_LIMIT_UNCERTAIN.value)
    )
