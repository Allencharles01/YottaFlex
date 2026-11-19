"""The five injected edge cases (PROJECT_PLAN.md §7.2, ``docs/anomaly_catalogue.md``).

Every injector works on the clean baseline tables of its *target* shipments only and returns an
:class:`Injection`: the raw tables it changed, the excursion segments it designed, the evidence
it built in (readings removed or re-attributed, a foreign device, a broken custody chain, the
carrier's claim) and one ground-truth record per affected shipment. The expected outcome of each
target is then derived from those facts with the documented rules
(:func:`coldchain.generate.expected.derive_expected_outcomes`), never by running the engine.

Injected data stays well formed (BACKLOG #2): every ID keeps its format and its parent, every
foreign key still resolves, surviving IDs are never renumbered, and nothing the engine treats as
a broken invariant (unknown device, missing lots, no effective policy) is produced. Each anomaly
breaks a *business* rule that the engine must flag.

==========================================  ================================================
Issue type                                  What is changed (target shipments only)
==========================================  ================================================
``logger_not_assigned``                     every reading's ``device_id`` → a device that has
                                            no assignment overlapping the logger window
``product_threshold_mismatch``              ``declared_policy_id`` → another temperature class,
                                            and the whole trip is held inside that class's
                                            safe band but outside the product's range
``missing_data_silently_passed``            one block of readings longer than the gap limit
                                            is deleted; the carrier's PASS stays
``custody_gap_not_flagged``                 one middle HANDOFF custody event is deleted
``excursion_across_unrelated_shipments``    two back-to-back shipments of one device: A ends
                                            and B starts above ``max_c``, each within its
                                            allowance, together beyond it
==========================================  ================================================
"""

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.config import AppConfig
from coldchain.enums import (
    CustodyEventType,
    ExcursionDirection,
    IssueType,
    PolicyCode,
    TableName,
)
from coldchain.errors import InjectionError
from coldchain.generate.expected import EVIDENCE_COLUMNS
from coldchain.generate.readings import reshape_segments
from coldchain.generate.scenarios import SEGMENT_COLUMNS
from coldchain.schemas import ISO_TIMESTAMP_FORMAT, LIST_SEPARATOR, effective_policy_ids

logger = logging.getLogger(__name__)

STAGES: dict[str, str] = {
    "targets": "inject.targets",
    **{issue.value: f"inject.{issue.value}" for issue in IssueType},
}
"""RNG stream name of target selection and of every injector (one stream per step)."""

SHIPMENTS_PER_ANOMALY: dict[IssueType, int] = {
    issue: 2 if issue is IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS else 1
    for issue in IssueType
}
"""How many shipments one anomaly of each type touches."""

SELECTION_ORDER: tuple[IssueType, ...] = (
    IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS,
    IssueType.CUSTODY_GAP_NOT_FLAGGED,
    IssueType.PRODUCT_THRESHOLD_MISMATCH,
    IssueType.MISSING_DATA_SILENTLY_PASSED,
    IssueType.LOGGER_NOT_ASSIGNED,
)
"""Order in which targets are drawn: the most constrained issue type first."""

TARGET_COLUMNS = ("issue_type", "anomaly", "lead_shipment_id", "shipment_id")
"""Columns of :func:`select_targets` (``anomaly`` counts from 1 within each issue type)."""

RECORD_COLUMNS = (
    "issue_type",
    "lead_shipment_id",
    "shipment_id",
    "description",
    "affected_record_ids",
    "removed_record_ids",
    "idle_min",
)
"""Columns of the ground-truth records returned by every injector (``idle_min``: pairs only)."""


@dataclass(frozen=True)
class Injection:
    """What one injector produced.

    Attributes:
        tables: The raw tables it changed (complete tables, not just the changed rows).
        segments: Excursion segments it designed (:data:`SEGMENT_COLUMNS`), possibly empty.
        evidence: Facts per target, indexed by ``shipment_id`` (:data:`EVIDENCE_COLUMNS`).
        records: One ground-truth record per target (:data:`RECORD_COLUMNS`).
    """

    tables: dict[TableName, pd.DataFrame]
    segments: pd.DataFrame
    evidence: pd.DataFrame
    records: pd.DataFrame


Injector = Callable[
    [AppConfig, dict[TableName, pd.DataFrame], pd.DataFrame, pd.DataFrame, np.random.Generator],
    Injection,
]
"""Signature of an injector: ``(config, tables, targets, applied, rng) -> Injection``."""


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def _ids(*columns: pd.Series | np.ndarray | list) -> list[str]:
    """Join aligned ID columns row by row into ``|``-lists (``None`` for an empty row)."""
    rows = zip(*columns, strict=True)
    joined = [LIST_SEPARATOR.join(dict.fromkeys(i for i in row if i)) for row in rows]
    return [value or None for value in joined]


def _ts(value: pd.Timestamp) -> str:
    """Format a timestamp the way the project writes it."""
    return value.strftime(ISO_TIMESTAMP_FORMAT)


def _windows(config: AppConfig, loggers: pd.DataFrame, ships: pd.Series) -> pd.DataFrame:
    """Logger window of each shipment in ``ships`` (same order), with its reading count."""
    frame = loggers.set_index("shipment_id").loc[ships.to_numpy()].reset_index()
    minutes = (frame["unassigned_at"] - frame["assigned_at"]).dt.total_seconds() // 60
    frame["readings"] = minutes.astype("int64") // config.reading_interval_min + 1
    return frame


def _reading_index(config: AppConfig, readings: pd.DataFrame, loggers: pd.DataFrame) -> pd.Series:
    """Position of every reading on its logger's grid (0 at ``assigned_at``)."""
    start = readings["logger_id"].map(loggers.set_index("logger_id")["assigned_at"])
    minutes = (readings["recorded_at"] - start).dt.total_seconds() // 60
    return (minutes // config.reading_interval_min).astype("int64")


def _evidence(
    tables: dict[TableName, pd.DataFrame],
    ships: pd.Series,
    *,
    untrusted: np.ndarray | int = 0,
    foreign_device: bool = False,
    custody_broken: bool = False,
) -> pd.DataFrame:
    """Evidence rows (:data:`EVIDENCE_COLUMNS`) for target shipments; the claim is kept as is."""
    claims = tables[TableName.SHIPMENTS].set_index("shipment_id")["carrier_reported_status"]
    frame = pd.DataFrame(
        {
            "untrusted_readings": np.broadcast_to(np.asarray(untrusted, dtype="int64"), len(ships)),
            "foreign_device": foreign_device,
            "custody_broken": custody_broken,
            "carrier_reported_status": claims.loc[ships.to_numpy()].to_numpy(),
        },
        index=pd.Index(ships.to_numpy(), name="shipment_id"),
    )
    return frame[list(EVIDENCE_COLUMNS)]


def empty_segments() -> pd.DataFrame:
    """An empty designed-segments frame with the right dtypes."""
    stamp = pd.Series(dtype="datetime64[us, UTC]")
    columns = {name: pd.Series(dtype=object) for name in SEGMENT_COLUMNS}
    columns.update(
        {
            "segment": pd.Series(dtype="int64"),
            "is_excursion": pd.Series(dtype=bool),
            "first_index": pd.Series(dtype="int64"),
            "reading_count": pd.Series(dtype="int64"),
            "absolute_index": pd.Series(dtype="int64"),
            "ramp_before": pd.Series(dtype="int64"),
            "ramp_after": pd.Series(dtype="int64"),
            "started_at": stamp,
            "ended_at": stamp,
        }
    )
    return pd.DataFrame(columns)[list(SEGMENT_COLUMNS)]


def _records(
    issue: IssueType,
    targets: pd.DataFrame,
    descriptions: list[str],
    affected: list[str | None],
    removed: list[str | None],
    idle_min: np.ndarray | None = None,
) -> pd.DataFrame:
    """Ground-truth records of one injector, aligned with ``targets``."""
    idle = pd.array([None] * len(targets) if idle_min is None else idle_min, dtype="Int64")
    return pd.DataFrame(
        {
            "issue_type": issue.value,
            "lead_shipment_id": targets["lead_shipment_id"].to_numpy(),
            "shipment_id": targets["shipment_id"].to_numpy(),
            "description": descriptions,
            "affected_record_ids": affected,
            "removed_record_ids": removed,
            "idle_min": idle,
        }
    )[list(RECORD_COLUMNS)]


# --------------------------------------------------------------------------------------------
# Target selection
# --------------------------------------------------------------------------------------------


def target_counts(config: AppConfig, shipments: int) -> dict[IssueType, int]:
    """Number of anomalies of each issue type for a run of ``shipments`` shipments.

    Each rate is a share of *shipments* (PROJECT_PLAN.md §7.2: "~1 % of shipments each"), so a
    two-shipment anomaly counts half as often: ``round(rate x shipments / shipments per
    anomaly)``, rounding halves up, and at least one anomaly of every type with a positive rate.

    Args:
        config: Validated configuration (``injection_rates``).
        shipments: Number of shipments in the run.

    Returns:
        Anomalies per issue type, in :class:`IssueType` order.
    """
    counts = {}
    for issue in IssueType:
        rate = config.injection_rates[issue]
        exact = rate * shipments / SHIPMENTS_PER_ANOMALY[issue]
        counts[issue] = 0 if rate == 0 else max(1, math.floor(exact + 0.5))
    return counts


def back_to_back_pairs(loggers: pd.DataFrame) -> pd.DataFrame:
    """Every pair of consecutive assignments of the same device.

    Args:
        loggers: The ``loggers`` table.

    Returns:
        ``device_id``, ``first_shipment_id``, ``second_shipment_id`` (the next assignment of
        that device) and ``idle_min`` (minutes from the first ``unassigned_at`` to the second
        ``assigned_at``), ordered by device and time.
    """
    ordered = loggers.sort_values(["device_id", "assigned_at"], kind="stable")
    by_device = ordered.groupby("device_id")
    following = by_device["shipment_id"].shift(-1)
    next_start = by_device["assigned_at"].shift(-1)
    keep = following.notna()
    idle = (next_start[keep] - ordered.loc[keep, "unassigned_at"]).dt.total_seconds() // 60
    return pd.DataFrame(
        {
            "device_id": ordered.loc[keep, "device_id"].to_numpy(),
            "first_shipment_id": ordered.loc[keep, "shipment_id"].to_numpy(),
            "second_shipment_id": following[keep].to_numpy(),
            "idle_min": idle.astype("int64").to_numpy(),
        }
    )


def mismatch_options(
    config: AppConfig, tables: dict[TableName, pd.DataFrame], applied: pd.DataFrame
) -> pd.DataFrame:
    """Every wrong temperature class a shipment's trip can be held in without the trap showing.

    For a shipment whose product policy is *P* (the applied version) and another code *d*
    (the version the planner would declare, effective at ``planned_departure_at``), the trip
    can be held in a band that is (with ``u`` = the device's uncertainty, ``m`` =
    ``limit_margin_c``):

    * inside *d*'s safe band ``[min_d + u + m, max_d - u - m]`` (fully compliant under *d*, not
      even near a limit), and
    * inside one of *P*'s excursion bands: HIGH ``[max_P + u + m, abs_max_P - m]`` or LOW
      ``[abs_min_P + m, min_P - u - m]`` (out of range under *P*, never near the limit nor
      beyond the absolute limit).

    Args:
        config: Validated configuration.
        tables: Baseline tables (``shipments``, ``loggers``, ``devices``,
            ``threshold_policies``).
        applied: Applied policy per shipment.

    Returns:
        One row per feasible option: ``shipment_id``, ``declared_policy_id``, ``direction``,
        ``low_c`` and ``high_c`` (the band, 2 decimals), ordered by shipment, code, direction.
    """
    policies = tables[TableName.THRESHOLD_POLICIES]
    margin = config.generation.readings.limit_margin_c
    base = applied[["shipment_id", "policy_code", "min_c", "max_c", "abs_min_c", "abs_max_c"]]
    base = base.merge(
        tables[TableName.SHIPMENTS][["shipment_id", "planned_departure_at"]], on="shipment_id"
    )
    base = base.merge(tables[TableName.LOGGERS][["shipment_id", "device_id"]], on="shipment_id")
    base = base.merge(tables[TableName.DEVICES][["device_id", "uncertainty_c"]], on="device_id")
    codes = pd.DataFrame({"declared_code": [code.value for code in PolicyCode]})
    options = base.merge(codes, how="cross")
    options = options[options["declared_code"] != options["policy_code"]].reset_index(drop=True)
    options["declared_policy_id"] = effective_policy_ids(
        policies, options["declared_code"], options["planned_departure_at"]
    ).to_numpy()
    options = options.dropna(subset=["declared_policy_id"])
    declared = policies[["policy_id", "min_c", "max_c"]].rename(
        columns={"policy_id": "declared_policy_id", "min_c": "d_min", "max_c": "d_max"}
    )
    options = options.merge(declared, on="declared_policy_id")
    u = options["uncertainty_c"]
    safe_low, safe_high = options["d_min"] + u + margin, options["d_max"] - u - margin
    bands = {
        ExcursionDirection.HIGH: (options["max_c"] + u + margin, options["abs_max_c"] - margin),
        ExcursionDirection.LOW: (options["abs_min_c"] + margin, options["min_c"] - u - margin),
    }
    frames = []
    for direction, (low, high) in bands.items():
        band_low = np.round(np.maximum(low, safe_low), 2)
        band_high = np.round(np.minimum(high, safe_high), 2)
        frames.append(
            options.assign(direction=direction.value, low_c=band_low, high_c=band_high)[
                band_low <= band_high
            ]
        )
    result = pd.concat(frames, ignore_index=True)
    result = result.sort_values(["shipment_id", "declared_code", "direction"], kind="stable")
    columns = ["shipment_id", "declared_policy_id", "direction", "low_c", "high_c"]
    return result[columns].reset_index(drop=True)


def _missing_block_bounds(config: AppConfig, applied: pd.DataFrame) -> pd.DataFrame:
    """Shortest and longest block that :func:`inject_missing_data` may delete, per shipment."""
    extra = config.injection.missing_extra_readings
    shortest = applied["max_reading_gap_min"] // config.reading_interval_min + extra.min
    return pd.DataFrame(
        {
            "shipment_id": applied["shipment_id"].to_numpy(),
            "shortest": shortest.to_numpy(),
            "longest": (shortest + extra.max - extra.min).to_numpy(),
        }
    )


def _free_device_counts(loggers: pd.DataFrame, devices: pd.DataFrame) -> pd.Series:
    """Devices without any assignment overlapping each logger window, per shipment.

    A device has non-overlapping assignments, so the windows overlapping one window (itself
    included) belong to distinct devices: ``free = devices - overlapping``.
    """
    starts = np.sort(loggers["assigned_at"].to_numpy())
    ends = np.sort(loggers["unassigned_at"].to_numpy())
    overlapping = np.searchsorted(starts, loggers["unassigned_at"].to_numpy(), side="right")
    overlapping -= np.searchsorted(ends, loggers["assigned_at"].to_numpy(), side="left")
    return pd.Series(len(devices) - overlapping, index=loggers["shipment_id"].to_numpy())


def select_targets(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    outcomes: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Choose the disjoint target shipments of every issue type.

    Only shipments whose natural scenario is NORMAL **and** whose expected status is COMPLIANT
    are eligible (DECISIONS.md #56). Every shipment gets a random rank; each issue type, in
    :data:`SELECTION_ORDER`, takes the lowest-ranked eligible shipments that are still free and
    that its injector can use:

    * excursion pair: two consecutive assignments of one device, both long enough for the
      excursion and its ramp; pairs are taken in order of the **shortest idle time** between
      the two assignments (the device goes straight from one trip to the next), ties broken by
      the first shipment's rank (DECISIONS.md #83);
    * custody gap: at least one HANDOFF event (an intermediate stop);
    * product mismatch: at least one option of :func:`mismatch_options`;
    * missing data: a window long enough to delete the longest block and keep one reading on
      either side;
    * logger not assigned: at least one device free during the whole window.

    Args:
        config: Validated configuration.
        tables: Baseline raw tables.
        outcomes: Baseline ``expected_outcomes``.
        applied: Applied policy per shipment.
        rng: The ``inject.targets`` stream.

    Returns:
        :data:`TARGET_COLUMNS`, one row per (anomaly, shipment).

    Raises:
        InjectionError: If an issue type cannot get the number of targets it needs.
    """
    shipments = tables[TableName.SHIPMENTS]["shipment_id"].sort_values(kind="stable")
    rank = pd.Series(rng.permutation(len(shipments)), index=shipments.to_numpy())
    natural = outcomes.set_index("shipment_id")
    eligible = (natural["scenario"] == "NORMAL") & (natural["expected_status"] == "COMPLIANT")
    eligible = eligible.reindex(rank.index, fill_value=False)

    windows = _windows(config, tables[TableName.LOGGERS], pd.Series(rank.index))
    readings = windows.set_index("shipment_id")["readings"]
    limits = applied.set_index("shipment_id")
    n_max = limits["max_single_excursion_min"] // config.reading_interval_min
    ramp_max = config.generation.scenarios.ramp_readings.max
    custody = tables[TableName.CUSTODY_EVENTS]
    bounds = _missing_block_bounds(config, applied).set_index("shipment_id")
    usable = {
        IssueType.CUSTODY_GAP_NOT_FLAGGED: rank.index.isin(
            custody.loc[custody["event_type"] == CustodyEventType.HANDOFF, "shipment_id"]
        ),
        IssueType.PRODUCT_THRESHOLD_MISMATCH: rank.index.isin(
            mismatch_options(config, tables, applied)["shipment_id"]
        ),
        IssueType.MISSING_DATA_SILENTLY_PASSED: (
            readings >= bounds["longest"].reindex(readings.index) + 2
        )
        .reindex(rank.index)
        .to_numpy(),
        IssueType.LOGGER_NOT_ASSIGNED: (
            _free_device_counts(tables[TableName.LOGGERS], tables[TableName.DEVICES]) > 0
        )
        .reindex(rank.index)
        .to_numpy(),
    }
    counts = target_counts(config, len(shipments))
    taken: set[str] = set()
    chosen: list[dict[str, object]] = []
    for issue in SELECTION_ORDER:
        need = counts[issue]
        if need == 0:
            continue
        if issue is IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS:
            pairs = back_to_back_pairs(tables[TableName.LOGGERS])
            first, second = pairs["first_shipment_id"], pairs["second_shipment_id"]
            fits = readings.loc[first].to_numpy() >= n_max.loc[first].to_numpy() + ramp_max + 1
            fits &= readings.loc[second].to_numpy() >= n_max.loc[second].to_numpy() + ramp_max + 1
            ok = eligible.loc[first].to_numpy() & eligible.loc[second].to_numpy() & fits
            pairs = pairs[ok].assign(rank=rank.loc[first[ok]].to_numpy())
            pairs = pairs.sort_values(["idle_min", "rank"], kind="stable")  # shortest idle first
            found = 0
            for a, b in zip(pairs["first_shipment_id"], pairs["second_shipment_id"], strict=True):
                if found == need:
                    break
                if a in taken or b in taken:
                    continue
                found += 1
                taken.update((a, b))
                chosen += [
                    {
                        "issue_type": issue.value,
                        "anomaly": found,
                        "lead_shipment_id": a,
                        "shipment_id": s,
                    }
                    for s in (a, b)
                ]
        else:
            pool = eligible.to_numpy() & usable[issue] & ~rank.index.isin(list(taken))
            picked = rank[pool].sort_values(kind="stable").index[:need]
            found = len(picked)
            taken.update(picked)
            chosen += [
                {
                    "issue_type": issue.value,
                    "anomaly": k + 1,
                    "lead_shipment_id": s,
                    "shipment_id": s,
                }
                for k, s in enumerate(picked)
            ]
        if found < need:
            raise InjectionError(
                f"{issue.value}: needs {need} anomalies but only {found} eligible targets "
                "(NORMAL and COMPLIANT, disjoint from the other issue types) were found"
            )
    frame = pd.DataFrame(chosen, columns=list(TARGET_COLUMNS))
    return frame.astype({"anomaly": "int64"})


# --------------------------------------------------------------------------------------------
# The five injectors
# --------------------------------------------------------------------------------------------


def inject_logger_not_assigned(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    targets: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> Injection:
    """Re-attribute every reading of the target loggers to a device not assigned to them.

    The replacement device is drawn among the devices that have no assignment overlapping the
    target's logger window, so the readings plausibly come from an idle spare device. The
    ``logger_id`` (and so every reading ID) is unchanged. Under §1.2 every reading is then
    untrusted: LOGGER_NOT_ASSIGNED, and zero trusted readings (DATA_GAP, LOW_COVERAGE).

    Args:
        config: Validated configuration.
        tables: Current raw tables.
        targets: This type's rows of :func:`select_targets`.
        applied: Applied policy per shipment (unused; part of the common signature).
        rng: The ``inject.logger_not_assigned`` stream.

    Returns:
        The changed ``temperature_readings`` with evidence and records.

    Raises:
        InjectionError: If no device is free for a target window.
    """
    loggers, devices = tables[TableName.LOGGERS], tables[TableName.DEVICES]
    readings = tables[TableName.TEMPERATURE_READINGS].copy()
    windows = _windows(config, loggers, targets["shipment_id"])
    device_ids = devices["device_id"].sort_values(kind="stable").to_numpy()
    replacement = []
    for start, end in zip(windows["assigned_at"], windows["unassigned_at"], strict=True):
        busy = loggers.loc[
            (loggers["assigned_at"] <= end) & (loggers["unassigned_at"] >= start), "device_id"
        ]
        free = device_ids[~np.isin(device_ids, busy.to_numpy())]
        if len(free) == 0:
            raise InjectionError(f"No device is free between {_ts(start)} and {_ts(end)}")
        replacement.append(free[rng.integers(len(free))])
    by_logger = pd.Series(replacement, index=windows["logger_id"].to_numpy())
    mask = readings["logger_id"].isin(by_logger.index)
    readings.loc[mask, "device_id"] = readings.loc[mask, "logger_id"].map(by_logger)
    changed = readings[mask].groupby("logger_id").size().reindex(windows["logger_id"]).fillna(0)

    descriptions = [
        f"All {n} readings of {log} carry device {new} instead of the assigned {old}; {new} has "
        "no assignment overlapping the logger window, so no reading can be attributed to the "
        "assigned, calibrated device."
        for log, new, old, n in zip(
            windows["logger_id"],
            replacement,
            windows["device_id"],
            changed.astype(int),
            strict=True,
        )
    ]
    return Injection(
        tables={TableName.TEMPERATURE_READINGS: readings},
        segments=empty_segments(),
        evidence=_evidence(
            tables,
            targets["shipment_id"],
            untrusted=changed.to_numpy(dtype="int64"),
            foreign_device=True,
        ),
        records=_records(
            IssueType.LOGGER_NOT_ASSIGNED,
            targets,
            descriptions,
            _ids(windows["logger_id"], replacement),
            [None] * len(targets),
        ),
    )


def inject_product_threshold_mismatch(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    targets: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> Injection:
    """Declare a wrong temperature class and hold the whole trip inside it.

    One option of :func:`mismatch_options` is drawn per target. ``declared_policy_id`` becomes
    the wrong class's version effective at ``planned_departure_at``, and every reading is moved
    into the option's band: ``clip(t - setpoint + centre, low, high)``, keeping the stream's
    natural shape. A checker that trusted the declaration sees a complete, compliant stream
    with no reading near a limit; under the product's policy the whole window is one excursion
    (SINGLE_DURATION and CUMULATIVE breaches, never an absolute breach or a near-limit reading).

    Args:
        config: Validated configuration.
        tables: Current raw tables.
        targets: This type's rows of :func:`select_targets`.
        applied: Applied policy per shipment (the product's policy).
        rng: The ``inject.product_threshold_mismatch`` stream.

    Returns:
        The changed ``shipments`` and ``temperature_readings``, one whole-window excursion
        segment per target, evidence and records.
    """
    options = mismatch_options(config, tables, applied)
    options = options[options["shipment_id"].isin(targets["shipment_id"])].reset_index(drop=True)
    pick = options.assign(u=rng.random(len(options))).groupby("shipment_id")["u"].idxmax()
    choice = options.loc[pick.loc[targets["shipment_id"]].to_numpy()].reset_index(drop=True)
    windows = _windows(config, tables[TableName.LOGGERS], targets["shipment_id"])
    choice["logger_id"] = windows["logger_id"].to_numpy()

    shipments = tables[TableName.SHIPMENTS].copy()
    declared = choice.set_index("shipment_id")["declared_policy_id"]
    hit = shipments["shipment_id"].isin(declared.index)
    shipments.loc[hit, "declared_policy_id"] = shipments.loc[hit, "shipment_id"].map(declared)

    setpoints = shipments.merge(
        tables[TableName.PACKAGING][["packaging_id", "setpoint_c"]], on="packaging_id"
    ).set_index("shipment_id")["setpoint_c"]
    per_logger = choice.assign(
        setpoint_c=setpoints.loc[choice["shipment_id"]].to_numpy(),
        centre=(choice["low_c"] + choice["high_c"]) / 2.0,
    ).set_index("logger_id")
    readings = tables[TableName.TEMPERATURE_READINGS].copy()
    mask = readings["logger_id"].isin(per_logger.index)
    band = per_logger.loc[readings.loc[mask, "logger_id"]]
    shifted = readings.loc[mask, "temperature_c"].to_numpy() - band["setpoint_c"].to_numpy()
    shifted = np.round(shifted + band["centre"].to_numpy(), 2)
    readings.loc[mask, "temperature_c"] = np.round(
        np.clip(shifted, band["low_c"].to_numpy(), band["high_c"].to_numpy()), 2
    )

    segments = pd.DataFrame(
        {
            "shipment_id": windows["shipment_id"].to_numpy(),
            "logger_id": windows["logger_id"].to_numpy(),
            "segment": 1,
            "is_excursion": True,
            "direction": choice["direction"].to_numpy(),
            "first_index": 0,
            "reading_count": windows["readings"].to_numpy(),
            "absolute_index": -1,
            "ramp_before": 0,
            "ramp_after": 0,
            "started_at": windows["assigned_at"].to_numpy(),
            "ended_at": windows["unassigned_at"].to_numpy(),
        }
    )[list(SEGMENT_COLUMNS)]
    limits = applied.set_index("shipment_id").loc[choice["shipment_id"]]
    high = (choice["direction"] == ExcursionDirection.HIGH.value).to_numpy()
    product_limit = np.where(high, limits["max_c"], limits["min_c"])
    descriptions = [
        f"Planner declared {decl}, but the product class is {code} (applied {appl}). The trip was "
        f"held at {low:.2f} to {high_c:.2f} °C: inside the declared band, {side} the product's "
        f"{limit:g} °C limit. Trusting the declaration would pass it."
        for decl, code, appl, low, high_c, side, limit in zip(
            choice["declared_policy_id"],
            limits["policy_code"],
            limits["applied_policy_id"],
            choice["low_c"],
            choice["high_c"],
            np.where(high, "above", "below"),
            product_limit,
            strict=True,
        )
    ]
    return Injection(
        tables={TableName.SHIPMENTS: shipments, TableName.TEMPERATURE_READINGS: readings},
        segments=segments,
        evidence=_evidence(tables, targets["shipment_id"]),
        records=_records(
            IssueType.PRODUCT_THRESHOLD_MISMATCH,
            targets,
            descriptions,
            _ids(targets["shipment_id"], choice["declared_policy_id"], windows["logger_id"]),
            [None] * len(targets),
        ),
    )


def inject_missing_data(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    targets: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> Injection:
    """Delete one block of consecutive readings longer than the gap limit, keeping PASS.

    The block has ``floor(max_reading_gap_min / i)`` readings (the shortest block whose gap
    ``(m + 1) x i`` exceeds the limit) plus ``missing_extra_readings`` more, and lies strictly
    inside the window (at least one reading survives on either side). ``carrier_reported_status``
    is not touched, so the carrier's PASS now contradicts the engine (an intended exception to
    DECISIONS.md #20/#27). Surviving reading IDs are unchanged.

    Args:
        config: Validated configuration.
        tables: Current raw tables.
        targets: This type's rows of :func:`select_targets`.
        applied: Applied policy per shipment (``max_reading_gap_min``).
        rng: The ``inject.missing_data_silently_passed`` stream.

    Returns:
        The changed ``temperature_readings``, evidence and records.
    """
    loggers = tables[TableName.LOGGERS]
    windows = _windows(config, loggers, targets["shipment_id"])
    bounds = _missing_block_bounds(config, applied).set_index("shipment_id")
    bounds = bounds.loc[targets["shipment_id"]]
    length = rng.integers(bounds["shortest"].to_numpy(), bounds["longest"].to_numpy() + 1)
    start = rng.integers(1, windows["readings"].to_numpy() - length)  # start + length <= n - 1
    block = pd.DataFrame(
        {"logger_id": windows["logger_id"].to_numpy(), "start": start, "length": length}
    ).set_index("logger_id")

    readings = tables[TableName.TEMPERATURE_READINGS]
    mine = readings["logger_id"].isin(block.index)
    index = pd.Series(-1, index=readings.index, dtype="int64")
    index[mine] = _reading_index(config, readings[mine], loggers)
    first = readings["logger_id"].map(block["start"])
    last = first + readings["logger_id"].map(block["length"]) - 1
    removed = mine & (index >= first) & (index <= last)
    edges = mine & ((index == first - 1) | (index == last + 1))

    gone = readings[removed].groupby("logger_id")["reading_id"]
    removed_ids = gone.agg(LIST_SEPARATOR.join).reindex(windows["logger_id"])
    kept = readings[edges].groupby("logger_id")["reading_id"].agg(LIST_SEPARATOR.join)
    span = readings[removed].groupby("logger_id")["recorded_at"].agg(["min", "max"])
    span = span.reindex(windows["logger_id"])
    limit = applied.set_index("shipment_id").loc[targets["shipment_id"], "max_reading_gap_min"]
    descriptions = [
        f"{m} consecutive readings of {log} deleted ({_ts(t0)} to {_ts(t1)}): a "
        f"{(m + 1) * config.reading_interval_min}-min gap against the {lim}-min limit. The "
        "carrier still reported PASS."
        for log, m, t0, t1, lim in zip(
            windows["logger_id"], length, span["min"], span["max"], limit, strict=True
        )
    ]
    return Injection(
        tables={TableName.TEMPERATURE_READINGS: readings[~removed].reset_index(drop=True)},
        segments=empty_segments(),
        evidence=_evidence(tables, targets["shipment_id"], untrusted=length),
        records=_records(
            IssueType.MISSING_DATA_SILENTLY_PASSED,
            targets,
            descriptions,
            kept.reindex(windows["logger_id"]).tolist(),
            removed_ids.tolist(),
        ),
    )


def inject_custody_gap(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    targets: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> Injection:
    """Delete one middle HANDOFF custody event of every target.

    A HANDOFF is never the first (PICKUP) or last (DELIVERY) event, so both neighbours survive.
    The surviving events keep their IDs and sequence numbers, so the chain skips one number,
    and the previous event's ``to_party_id`` no longer equals the next event's
    ``from_party_id`` (a hub hand-over always switches between the carrier and the hub party).

    Args:
        config: Validated configuration (unused; part of the common signature).
        tables: Current raw tables.
        targets: This type's rows of :func:`select_targets`.
        applied: Applied policy per shipment (unused; part of the common signature).
        rng: The ``inject.custody_gap_not_flagged`` stream.

    Returns:
        The changed ``custody_events``, evidence and records.
    """
    events = tables[TableName.CUSTODY_EVENTS]
    events = events.sort_values(["shipment_id", "sequence"], kind="stable").reset_index(drop=True)
    handoffs = events[
        events["shipment_id"].isin(targets["shipment_id"])
        & (events["event_type"] == CustodyEventType.HANDOFF.value)
    ]
    pick = handoffs.assign(u=rng.random(len(handoffs))).groupby("shipment_id")["u"].idxmax()
    deleted = events.loc[pick.loc[targets["shipment_id"]].to_numpy()]
    before = events.loc[deleted.index - 1]
    after = events.loc[deleted.index + 1]
    descriptions = [
        f"Custody event {cid} (HANDOFF {src} to {dst}) deleted: sequence {seq - 1} is followed "
        f"by {seq + 1}, and {prev_to} never hands over to {next_from}. The carrier still "
        "reported PASS."
        for cid, src, dst, seq, prev_to, next_from in zip(
            deleted["custody_event_id"],
            deleted["from_party_id"],
            deleted["to_party_id"],
            deleted["sequence"],
            before["to_party_id"],
            after["from_party_id"],
            strict=True,
        )
    ]
    return Injection(
        tables={TableName.CUSTODY_EVENTS: events.drop(index=deleted.index).reset_index(drop=True)},
        segments=empty_segments(),
        evidence=_evidence(tables, targets["shipment_id"], custody_broken=True),
        records=_records(
            IssueType.CUSTODY_GAP_NOT_FLAGGED,
            targets,
            descriptions,
            _ids(before["custody_event_id"], after["custody_event_id"]),
            deleted["custody_event_id"].tolist(),
        ),
    )


def inject_excursion_across_shipments(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    targets: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> Injection:
    """Make shipment A end warm and shipment B start warm on the same, reused device.

    A and B are consecutive assignments of one device (:func:`back_to_back_pairs`). With
    ``n = floor(max_single_excursion_min / i)`` under each one's applied policy and ``n_max``
    the larger of the two, A's last ``k_A`` and B's first ``k_B`` readings are designed HIGH
    excursions with ``1 <= k <= n`` (each within its own allowance) and ``k_A + k_B > n_max``
    (one run beyond the allowance if the two were merged by device). A gets a ramp before its
    block and B a ramp after it; both are shaped by
    :func:`coldchain.generate.readings.reshape_segments`.

    Args:
        config: Validated configuration.
        tables: Current raw tables.
        targets: This type's rows of :func:`select_targets` (two rows per anomaly: A, then B).
        applied: Applied policy per shipment.
        rng: The ``inject.excursion_across_unrelated_shipments`` stream.

    Returns:
        The changed ``temperature_readings``, two segments per anomaly, evidence and records.
    """
    interval = config.reading_interval_min
    windows = _windows(config, tables[TableName.LOGGERS], targets["shipment_id"])
    first = windows.iloc[0::2].reset_index(drop=True)
    second = windows.iloc[1::2].reset_index(drop=True)
    limits = applied.set_index("shipment_id")
    n_a = (limits.loc[first["shipment_id"], "max_single_excursion_min"] // interval).to_numpy()
    n_b = (limits.loc[second["shipment_id"], "max_single_excursion_min"] // interval).to_numpy()
    longest = np.maximum(n_a, n_b)
    k_a = rng.integers(np.maximum(1, longest - n_b + 1), n_a + 1)
    k_b = rng.integers(longest - k_a + 1, n_b + 1)
    ramp = config.generation.scenarios.ramp_readings
    ramp_a = rng.integers(ramp.min, ramp.max + 1, size=len(first))
    ramp_b = rng.integers(ramp.min, ramp.max + 1, size=len(second))

    def segment(window: pd.DataFrame, count: np.ndarray, start: np.ndarray, before, after):
        started = window["assigned_at"] + pd.to_timedelta(start * interval, unit="min")
        return pd.DataFrame(
            {
                "shipment_id": window["shipment_id"].to_numpy(),
                "logger_id": window["logger_id"].to_numpy(),
                "segment": 1,
                "is_excursion": True,
                "direction": ExcursionDirection.HIGH.value,
                "first_index": start,
                "reading_count": count,
                "absolute_index": -1,
                "ramp_before": before,
                "ramp_after": after,
                "started_at": started.to_numpy(),
                "ended_at": (
                    started + pd.to_timedelta((count - 1) * interval, unit="min")
                ).to_numpy(),
            }
        )[list(SEGMENT_COLUMNS)]

    zeros = np.zeros(len(first), dtype="int64")
    segments = pd.concat(
        [
            segment(first, k_a, first["readings"].to_numpy() - k_a, ramp_a, zeros),
            segment(second, k_b, zeros, zeros, ramp_b),
        ],
        ignore_index=True,
    )
    readings = tables[TableName.TEMPERATURE_READINGS].copy()
    shaped = reshape_segments(
        config,
        windows,
        readings,
        applied,
        tables[TableName.DEVICES],
        segments,
        rng,
    )
    position = pd.Series(readings.index, index=readings["reading_id"].to_numpy())
    readings.loc[position.loc[shaped.index].to_numpy(), "temperature_c"] = shaped.to_numpy()

    by_logger = (
        readings.loc[position.loc[shaped.index].to_numpy()]
        .sort_values("recorded_at", kind="stable")
        .groupby("logger_id")["reading_id"]
        .agg(LIST_SEPARATOR.join)
    )
    duration_a, duration_b = k_a * interval, k_b * interval
    idle = (second["assigned_at"] - first["unassigned_at"]).dt.total_seconds() // 60
    idle = idle.astype("int64").to_numpy()
    pair_text = [
        f"{dev} went from {a} to {b} after {gap} min idle: {a} ends with {ka} and {b} starts "
        f"with {kb} readings above max_c. Per assignment {da} and {db} min, each within its "
        f"allowance; merged by device, one {ka + kb}-reading run beyond it."
        for dev, a, b, gap, ka, kb, da, db in zip(
            first["device_id"],
            first["shipment_id"],
            second["shipment_id"],
            idle,
            k_a,
            k_b,
            duration_a,
            duration_b,
            strict=True,
        )
    ]
    descriptions = list(np.repeat(pair_text, 2))
    return Injection(
        tables={TableName.TEMPERATURE_READINGS: readings},
        segments=segments,
        evidence=_evidence(tables, targets["shipment_id"]),
        records=_records(
            IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS,
            targets,
            descriptions,
            _ids(windows["device_id"], by_logger.reindex(windows["logger_id"]).to_numpy()),
            [None] * len(targets),
            np.repeat(idle, 2),
        ),
    )


INJECTORS: dict[IssueType, Injector] = {
    IssueType.LOGGER_NOT_ASSIGNED: inject_logger_not_assigned,
    IssueType.PRODUCT_THRESHOLD_MISMATCH: inject_product_threshold_mismatch,
    IssueType.MISSING_DATA_SILENTLY_PASSED: inject_missing_data,
    IssueType.CUSTODY_GAP_NOT_FLAGGED: inject_custody_gap,
    IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS: inject_excursion_across_shipments,
}
"""The injector of every issue type."""


# --------------------------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------------------------


def anomaly_table(records: pd.DataFrame, outcomes: pd.DataFrame) -> pd.DataFrame:
    """Build the ``anomalies`` table from the injectors' records and the expected outcomes.

    Anomaly IDs are numbered ``ANOM-000001, ...`` in :class:`IssueType` order, then by the
    anomaly's lead shipment (A for a pair); both rows of a pair share one ID (DECISIONS.md #8).
    Expected status, reason codes and excursion durations are copied from the injected
    shipments' ``expected_outcomes`` rows, so the two tables always agree.

    Args:
        records: The injectors' records (:data:`RECORD_COLUMNS`).
        outcomes: The updated ``expected_outcomes``.

    Returns:
        The ``anomalies`` table (schema columns).
    """
    rank = {issue.value: k for k, issue in enumerate(IssueType)}
    frame = records.assign(issue_rank=records["issue_type"].map(rank))
    frame = frame.sort_values(["issue_rank", "lead_shipment_id", "shipment_id"], kind="stable")
    number = frame.groupby(["issue_rank", "lead_shipment_id"], sort=False).ngroup() + 1
    frame["anomaly_id"] = [ids.anomaly_id(int(n)) for n in number]
    expected = outcomes.set_index("shipment_id").loc[
        frame["shipment_id"],
        ["expected_status", "expected_reason_codes", "expected_excursion_durations_min"],
    ]
    for column in expected.columns:
        frame[column] = expected[column].to_numpy()
    return frame[
        [
            "anomaly_id",
            "shipment_id",
            "issue_type",
            "description",
            "affected_record_ids",
            "removed_record_ids",
            "expected_status",
            "expected_reason_codes",
            "expected_excursion_durations_min",
            "idle_min",
        ]
    ].reset_index(drop=True)
