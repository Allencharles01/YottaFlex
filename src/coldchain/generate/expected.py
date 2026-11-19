"""Expected outcomes: the answer key for the natural scenarios (``ground_truth/expected_outcomes``).

The expected result of every shipment is derived by applying the documented rules
(``docs/compliance_rules.md``) to what the generator *built*: the applied policy, the logger's
device and calibration, the route stops' dwell and the designed excursion segments. It is
never assigned at random, and it is never obtained by scanning the generated readings, so it
stays independent from the Phase 4 engine. Natural readings are kept clear of every limit by
construction (:mod:`coldchain.generate.readings`), so the designed segments are the only
readings that can trigger a temperature rule. The tests re-scan the readings to prove it.
"""

import numpy as np
import pandas as pd

from coldchain.config import AppConfig
from coldchain.enums import (
    REASON_STATUS,
    STATUS_ACTION,
    STATUS_SEVERITY,
    BreachType,
    ExcursionDirection,
    ReasonCode,
)
from coldchain.errors import GenerationError
from coldchain.schemas import ISO_TIMESTAMP_FORMAT, LIST_SEPARATOR, effective_policy_ids

POLICY_LIMIT_COLUMNS = (
    "min_c",
    "max_c",
    "abs_min_c",
    "abs_max_c",
    "max_single_excursion_min",
    "max_cumulative_excursion_min",
)
"""Policy columns carried by :func:`applied_policies`."""


def applied_policies(
    shipments: pd.DataFrame,
    shipment_lots: pd.DataFrame,
    lots: pd.DataFrame,
    products: pd.DataFrame,
    policies: pd.DataFrame,
) -> pd.DataFrame:
    """Resolve the applied policy of every shipment (compliance_rules.md §1.1).

    The policy code comes from the products in the shipment (``shipment_lots → lots →
    products``); the version is the one effective at ``actual_departure_at``.

    Args:
        shipments: Shipments with ``shipment_id``, ``declared_policy_id``, ``actual_departure_at``.
        shipment_lots: The ``shipment_lots`` table.
        lots: The ``lots`` table.
        products: The ``products`` table.
        policies: The ``threshold_policies`` table.

    Returns:
        One row per shipment: ``shipment_id``, ``declared_policy_id``, ``policy_code``,
        ``applied_policy_id`` and the limits in :data:`POLICY_LIMIT_COLUMNS`.

    Raises:
        GenerationError: If a shipment has no lots, mixes policy codes (DECISIONS.md #21), or
            no version of its code is effective at departure.
    """
    codes = shipment_lots[["shipment_id", "lot_id"]].merge(
        lots[["lot_id", "product_id"]], on="lot_id"
    )
    codes = codes.merge(products[["product_id", "policy_code"]], on="product_id")
    per_shipment = codes.groupby("shipment_id")["policy_code"].agg(["nunique", "first"])
    mixed = per_shipment.index[per_shipment["nunique"] != 1].tolist()
    if mixed:
        raise GenerationError(f"Shipments mix policy codes: {mixed}")
    frame = shipments[["shipment_id", "declared_policy_id", "actual_departure_at"]].copy()
    frame["policy_code"] = per_shipment["first"].reindex(frame["shipment_id"]).to_numpy()
    if frame["policy_code"].isna().any():
        raise GenerationError(f"Shipments without lots: {frame.loc[frame['policy_code'].isna()]}")
    frame["applied_policy_id"] = effective_policy_ids(
        policies, frame["policy_code"], frame["actual_departure_at"]
    ).to_numpy()
    if frame["applied_policy_id"].isna().any():
        missing = frame.loc[frame["applied_policy_id"].isna(), "shipment_id"].tolist()
        raise GenerationError(f"No policy effective at actual departure for {missing}")
    limits = policies[["policy_id", *POLICY_LIMIT_COLUMNS]].rename(
        columns={"policy_id": "applied_policy_id"}
    )
    frame = frame.merge(limits, on="applied_policy_id", how="left")
    return frame.drop(columns="actual_departure_at")


def _excursion_facts(
    config: AppConfig, segments: pd.DataFrame, applied: pd.DataFrame
) -> pd.DataFrame:
    """Duration and breach type of every designed excursion (compliance_rules.md §1.5)."""
    excursions = segments[segments["is_excursion"].astype(bool)].merge(
        applied[["shipment_id", "max_single_excursion_min"]], on="shipment_id"
    )
    window = (excursions["ended_at"] - excursions["started_at"]).dt.total_seconds() // 60
    excursions["duration_min"] = window.astype("int64") + config.reading_interval_min
    excursions["breach_type"] = np.select(
        [
            excursions["absolute_index"] >= 0,
            excursions["duration_min"] > excursions["max_single_excursion_min"],
        ],
        [BreachType.ABSOLUTE_LIMIT_BREACH.value, BreachType.SINGLE_DURATION_BREACH.value],
        default=BreachType.WITHIN_ALLOWANCE.value,
    )
    return excursions.sort_values(["shipment_id", "started_at", "direction"], kind="stable")


def _near_limit(segments: pd.DataFrame, applied: pd.DataFrame, uncertainty: pd.Series) -> pd.Series:
    """Shipments with a designed reading within ``uncertainty_c`` of a limit (§1.6).

    AT_LIMIT segments hold readings exactly at ``max_c`` (HIGH) or ``min_c`` (LOW); every other
    reading is kept more than ``uncertainty_c`` away from both limits by construction.
    """
    at_limit = segments[~segments["is_excursion"].astype(bool)].merge(
        applied[["shipment_id", "min_c", "max_c"]], on="shipment_id"
    )
    value = np.where(
        at_limit["direction"] == ExcursionDirection.HIGH.value,
        at_limit["max_c"],
        at_limit["min_c"],
    )
    gap = np.minimum(
        np.abs(value - at_limit["min_c"].to_numpy()), np.abs(value - at_limit["max_c"].to_numpy())
    )
    near = np.round(gap, 2) <= uncertainty.loc[at_limit["shipment_id"]].to_numpy()
    return pd.Series(near, index=at_limit["shipment_id"].to_numpy()).groupby(level=0).any()


def derive_expected_outcomes(
    config: AppConfig,
    assignment: pd.DataFrame,
    applied: pd.DataFrame,
    loggers: pd.DataFrame,
    devices: pd.DataFrame,
    route_stops: pd.DataFrame,
    segments: pd.DataFrame,
) -> pd.DataFrame:
    """Derive the expected engine result of every shipment from the documented rules.

    Reason codes, per compliance_rules.md:

    * POLICY_MISMATCH_DECLARED: declared ≠ applied ``policy_id`` (§1.1);
    * CALIBRATION_EXPIRED: calibration does not cover the logger window (§1.7);
    * excursion codes from the designed excursions: duration ``(end - start) + i``, breach type
      (absolute first, then single), CUMULATIVE when the total exceeds the allowance, and
      EXCURSION_WITHIN_ALLOWANCE when any excursion is within its allowance (§1.5, §2);
    * NEAR_LIMIT_UNCERTAIN from the designed at-limit readings (§1.6);
    * LONG_DWELL: any stop dwells longer than ``long_dwell_threshold_min`` (§1.9).

    Natural data is complete, so no gap, coverage, logger or custody code is expected, and the
    carrier's claim is set from the expected action, so no contradiction is expected either.

    Args:
        config: Validated configuration.
        assignment: Scenario per shipment (:func:`coldchain.generate.scenarios.assign_scenarios`).
        applied: Applied policy per shipment (:func:`applied_policies`).
        loggers: The ``loggers`` table.
        devices: The ``devices`` table.
        route_stops: The ``route_stops`` table.
        segments: Designed segments (:func:`coldchain.generate.scenarios.design_segments`).

    Returns:
        The ``expected_outcomes`` table (schema columns, ``issue_type`` null).
    """
    frame = assignment[["shipment_id", "scenario"]].merge(applied, on="shipment_id")
    frame = frame.merge(
        loggers[["shipment_id", "device_id", "assigned_at", "unassigned_at"]], on="shipment_id"
    )
    frame = frame.merge(
        devices[["device_id", "calibrated_on", "calibration_due_on", "uncertainty_c"]],
        on="device_id",
    ).set_index("shipment_id")
    ships = frame.index

    excursions = _excursion_facts(config, segments, applied)
    by_ship = excursions.groupby("shipment_id")
    breach = (
        excursions.assign(
            absolute=excursions["breach_type"] == BreachType.ABSOLUTE_LIMIT_BREACH.value,
            single=excursions["breach_type"] == BreachType.SINGLE_DURATION_BREACH.value,
            within=excursions["breach_type"] == BreachType.WITHIN_ALLOWANCE.value,
        )
        .groupby("shipment_id")[["absolute", "single", "within"]]
        .any()
    )
    breach = breach.reindex(ships, fill_value=False)
    total = by_ship["duration_min"].sum().reindex(ships, fill_value=0).astype("int64")
    max_dwell = route_stops.groupby("shipment_id")["dwell_min"].max().reindex(ships)

    flags = {
        ReasonCode.ABSOLUTE_LIMIT_BREACH: breach["absolute"],
        ReasonCode.SINGLE_DURATION_BREACH: breach["single"],
        ReasonCode.CUMULATIVE_DURATION_BREACH: total > frame["max_cumulative_excursion_min"],
        ReasonCode.CALIBRATION_EXPIRED: (
            (frame["calibration_due_on"] < frame["unassigned_at"].dt.date)
            | (frame["calibrated_on"] > frame["assigned_at"].dt.date)
        ),
        ReasonCode.EXCURSION_WITHIN_ALLOWANCE: breach["within"],
        ReasonCode.NEAR_LIMIT_UNCERTAIN: _near_limit(segments, applied, frame["uncertainty_c"])
        .reindex(ships, fill_value=False)
        .astype(bool),
        ReasonCode.POLICY_MISMATCH_DECLARED: frame["declared_policy_id"]
        != frame["applied_policy_id"],
        ReasonCode.LONG_DWELL: (max_dwell > config.long_dwell_threshold_min).fillna(False),
    }
    matrix = pd.DataFrame(
        {code.value: flags.get(code, False) for code in ReasonCode}, index=ships
    ).astype(bool)
    reasons = matrix.apply(lambda row: LIST_SEPARATOR.join(matrix.columns[row.to_numpy()]), axis=1)
    severity = pd.DataFrame(
        {
            code.value: np.where(matrix[code.value], STATUS_SEVERITY[status], 1)
            for code, status in REASON_STATUS.items()
            if status is not None
        },
        index=ships,
    ).max(axis=1)
    by_severity = {level: status.value for status, level in STATUS_SEVERITY.items()}
    status = severity.map(by_severity)
    action = status.map({s.value: a.value for s, a in STATUS_ACTION.items()})

    windows = (
        excursions["started_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
        + "/"
        + excursions["ended_at"].dt.strftime(ISO_TIMESTAMP_FORMAT)
    )
    joined = excursions.assign(window=windows, duration=excursions["duration_min"].astype(str))
    lists = joined.groupby("shipment_id")[["window", "duration"]].agg(LIST_SEPARATOR.join)
    lists = lists.reindex(ships)

    result = pd.DataFrame(
        {
            "shipment_id": ships.to_numpy(),
            "scenario": frame["scenario"].to_numpy(),
            "issue_type": None,
            "expected_status": status.to_numpy(),
            "expected_action": action.to_numpy(),
            "expected_reason_codes": reasons.where(reasons != "", None).to_numpy(),
            "expected_excursion_count": by_ship.size().reindex(ships, fill_value=0).to_numpy(),
            "expected_total_excursion_min": total.to_numpy(),
            "expected_excursion_windows": lists["window"].to_numpy(),
            "expected_excursion_durations_min": lists["duration"].to_numpy(),
        }
    )
    return result
