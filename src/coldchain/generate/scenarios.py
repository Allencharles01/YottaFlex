"""Natural scenarios: which shipment gets which scenario, and how each scenario is shaped.

Scenarios are assigned *before* the shipment tables are built (DECISIONS.md #38). The stage
works in this order:

1. **policy transitions**: for every policy version that takes effect inside the date window,
   one shipment is planned before the change and departs after it, so the planner's declared
   version differs from the applied one (DECISIONS.md #26, #39);
2. **assignment**: exact scenario counts from ``scenario_weights`` (at least one of every
   scenario with a positive weight), a variant per shipment and an excursion direction;
3. **flags**: the per-shipment switches passed into the shipment generators
   (:class:`coldchain.generate.shipments.ShipmentFlags`);
4. **design**: once the logger windows exist, the *designed segments* of every shaped
   shipment: which consecutive readings are an excursion (or sit exactly at a limit). The
   readings generator turns them into temperatures, and :mod:`coldchain.generate.expected`
   derives the expected outcome from them with the documented rules.

Every scenario except NORMAL has one shape per *variant*; variants cycle in shipment-ID order so
small runs still cover them.
"""

import logging
from collections.abc import Mapping

import numpy as np
import pandas as pd

from coldchain.config import AppConfig
from coldchain.enums import ExcursionDirection, Scenario, ScenarioVariant
from coldchain.errors import GenerationError
from coldchain.generate.shipments import ShipmentFlags

logger = logging.getLogger(__name__)

STAGES: dict[str, str] = {
    "transitions": "scenarios.transitions",
    "assign": "scenarios.assign",
    "design": "scenarios.design",
}
"""RNG stream name of every step of the scenario stage."""

SCENARIO_VARIANTS: dict[Scenario, tuple[ScenarioVariant, ...]] = {
    Scenario.NORMAL: (ScenarioVariant.NONE,),
    Scenario.MINOR_SPIKE_WITHIN_ALLOWANCE: (ScenarioVariant.SPIKE,),
    Scenario.BOUNDARY: (ScenarioVariant.AT_LIMIT, ScenarioVariant.EXACT_ALLOWANCE),
    Scenario.EXCURSION_BREACH: (
        ScenarioVariant.SINGLE_DURATION,
        ScenarioVariant.ABSOLUTE_LIMIT,
        ScenarioVariant.CUMULATIVE_DURATION,
    ),
    Scenario.LONG_DWELL: (ScenarioVariant.NONE,),
    Scenario.CALIBRATION_EXPIRED: (ScenarioVariant.NONE,),
}
"""The shapes of every scenario, in the order they are cycled through."""

SEGMENT_COLUMNS = (
    "shipment_id",
    "logger_id",
    "segment",
    "is_excursion",
    "direction",
    "first_index",
    "reading_count",
    "absolute_index",
    "ramp_before",
    "ramp_after",
    "started_at",
    "ended_at",
)
"""Columns of the designed-segments frame (see :func:`design_segments`)."""


# --------------------------------------------------------------------------------------------
# 1. Policy transitions
# --------------------------------------------------------------------------------------------


def policy_transitions(policies: pd.DataFrame, config: AppConfig) -> pd.DataFrame:
    """List the instants at which a later policy version takes effect inside the date window.

    Args:
        policies: The ``threshold_policies`` table.
        config: Validated configuration (date window).

    Returns:
        One row per transition: ``policy_code``, ``policy_id`` (the new version) and
        ``boundary`` (its ``effective_from`` at UTC midnight), ordered by boundary and code.
    """
    first_version = policies.groupby("policy_code")["version"].transform("min")
    later = policies[policies["version"] > first_version]
    boundary = pd.to_datetime(later["effective_from"]).dt.tz_localize("UTC")
    start = pd.Timestamp(config.date_window.start, tz="UTC")
    end = pd.Timestamp(config.date_window.end, tz="UTC") + pd.Timedelta(days=1)
    inside = (boundary > start) & (boundary < end)
    return (
        pd.DataFrame(
            {
                "policy_code": later["policy_code"].to_numpy(),
                "policy_id": later["policy_id"].to_numpy(),
                "boundary": boundary.to_numpy(),
            }
        )[inside.to_numpy()]
        .sort_values(["boundary", "policy_code"], kind="stable")
        .reset_index(drop=True)
    )


def force_policy_transitions(
    config: AppConfig,
    plan: pd.DataFrame,
    policies: pd.DataFrame,
    products: pd.DataFrame,
    rng: np.random.Generator,
) -> tuple[pd.DataFrame, np.ndarray]:
    """Guarantee one shipment per policy transition that is planned before it and departs after.

    For every transition (see :func:`policy_transitions`), the latest shipment of that code
    planned within ``departure_delay_min.max`` minutes before the boundary is chosen. If there
    is none, the latest shipment planned before the boundary is used instead: its primary
    product is swapped for a random product of the code (if needed) and its planned departure
    is moved to a random minute in that last stretch before the boundary. It stays the latest
    shipment before the boundary, so shipment-ID order is kept. Its departure delay is then
    forced high enough that it actually departs on or after the boundary.

    Args:
        config: Validated configuration.
        plan: Output of :func:`coldchain.generate.shipments.plan_shipments`.
        policies: The ``threshold_policies`` table.
        products: The ``products`` table.
        rng: The ``scenarios.transitions`` stream.

    Returns:
        ``(plan, min_delay)``: the (possibly adjusted) plan and the minimum departure delay in
        minutes per plan row.

    Raises:
        GenerationError: If a transition cannot be straddled (no shipment before it, or the
            maximum departure delay is below one minute).
    """
    delays = config.generation.departure_delay_min
    plan = plan.copy()
    min_delay = np.full(len(plan), delays.min, dtype="int64")
    used = np.zeros(len(plan), dtype=bool)
    for transition in policy_transitions(policies, config).itertuples(index=False):
        if delays.max < 1:
            raise GenerationError(
                "departure_delay_min.max must be >= 1 to straddle a policy transition"
            )
        departure = plan["planned_departure_at"]
        before = (departure < transition.boundary).to_numpy() & ~used
        reach = (departure >= transition.boundary - pd.Timedelta(minutes=delays.max)).to_numpy()
        same_code = (plan["policy_code"] == transition.policy_code).to_numpy()
        candidates = np.flatnonzero(before & reach & same_code)
        if len(candidates):
            row = candidates[-1]
        else:
            eligible = np.flatnonzero(before)
            if len(eligible) == 0:
                raise GenerationError(
                    f"No shipment is planned before the {transition.policy_id} transition"
                )
            row = eligible[-1]
            if not same_code[row]:
                options = products[products["policy_code"] == transition.policy_code]
                product = options.iloc[rng.integers(len(options))]
                plan.loc[row, "primary_product_id"] = product["product_id"]
                plan.loc[row, "policy_code"] = product["policy_code"]
                plan.loc[row, "supplier_party_id"] = product["supplier_party_id"]
            if not reach[row]:
                lead = int(rng.integers(1, delays.max + 1))
                plan.loc[row, "planned_departure_at"] = transition.boundary - pd.Timedelta(
                    minutes=lead
                )
        needed = (transition.boundary - plan["planned_departure_at"].iloc[row]).total_seconds()
        min_delay[row] = max(min_delay[row], int(needed // 60))
        used[row] = True
        logger.debug(
            "Policy transition %s straddled by %s", transition.policy_id, plan["shipment_id"][row]
        )
    return plan, min_delay


# --------------------------------------------------------------------------------------------
# 2. Assignment
# --------------------------------------------------------------------------------------------


def scenario_counts(weights: Mapping[Scenario, float], count: int) -> dict[Scenario, int]:
    """Exact number of shipments per scenario.

    Counts follow the weights by largest-remainder rounding (ties broken in :class:`Scenario`
    order). Every scenario with a positive weight is then topped up to its *minimum*, taking
    shipments from the scenario with the most: the minimum is its number of variants
    (:data:`SCENARIO_VARIANTS`) when ``count`` allows all of them, else one shipment
    (DECISIONS.md #40, #52).

    Args:
        weights: Weight per scenario (sums to 1).
        count: Number of shipments.

    Returns:
        Shipments per scenario, summing to ``count``.
    """
    scenarios = list(Scenario)
    quota = np.array([weights[s] for s in scenarios], dtype=float) * count
    counts = np.floor(quota).astype(int)
    remainder = quota - counts
    order = sorted(range(len(scenarios)), key=lambda k: (-remainder[k], k))
    for k in order[: count - counts.sum()]:
        counts[k] += 1
    positive = [k for k, s in enumerate(scenarios) if weights[s] > 0]
    every_variant = {k: len(SCENARIO_VARIANTS[scenarios[k]]) for k in positive}
    if count >= sum(every_variant.values()):
        minimum = every_variant
    elif count >= len(positive):
        minimum = dict.fromkeys(positive, 1)
        logger.warning("Only %d shipments: cannot guarantee every scenario variant", count)
    else:
        minimum = {}
        logger.warning(
            "Only %d shipments: cannot guarantee one of each of %d scenarios", count, len(positive)
        )
    floor = np.array([minimum.get(k, 0) for k in range(len(scenarios))])
    for k, needed in minimum.items():
        while counts[k] < needed:
            counts[int(np.argmax(counts - floor))] -= 1  # the largest surplus gives one
            counts[k] += 1
    return {s: int(c) for s, c in zip(scenarios, counts, strict=True)}


def calibration_expired_eligible(
    config: AppConfig, plan: pd.DataFrame, devices: pd.DataFrame, needed: int
) -> np.ndarray:
    """Which shipments can take a device that is already past its calibration due date.

    A shipment is eligible once ``needed`` devices have expired and finished their last
    baseline assignment: its planned departure is at least one day plus the worst-case
    turnaround, logger lead and early departure after the ``needed``-th earliest due date.

    Args:
        config: Validated configuration.
        plan: The shipment plan.
        devices: The ``devices`` table.
        needed: Number of CALIBRATION_EXPIRED shipments to place.

    Returns:
        Boolean per plan row.
    """
    if needed == 0:
        return np.zeros(len(plan), dtype=bool)
    if needed > len(devices):
        return np.zeros(len(plan), dtype=bool)
    due = np.sort(pd.to_datetime(devices["calibration_due_on"]).to_numpy())[needed - 1]
    settings = config.generation
    margin = pd.Timedelta(
        days=1,
        minutes=settings.devices.turnaround_min
        + settings.logger_lead_min.max
        + config.reading_interval_min
        + max(0, -settings.departure_delay_min.min),
    )
    earliest = pd.Timestamp(due).tz_localize("UTC") + margin
    return (plan["planned_departure_at"] >= earliest).to_numpy()


def assign_scenarios(
    config: AppConfig, plan: pd.DataFrame, devices: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Assign a scenario, a variant and an excursion direction to every shipment.

    Each shipment draws a random key. CALIBRATION_EXPIRED goes first to the eligible
    shipments (see :func:`calibration_expired_eligible`) with the lowest keys; the other
    scenarios then fill the remaining shipments in key order, in :class:`Scenario` order.
    Variants cycle through :data:`SCENARIO_VARIANTS` in shipment-ID order within a scenario.

    Args:
        config: Validated configuration.
        plan: The shipment plan (shipment IDs in departure order).
        devices: The ``devices`` table.
        rng: The ``scenarios.assign`` stream.

    Returns:
        One row per shipment: ``shipment_id``, ``scenario``, ``variant`` and ``direction``.

    Raises:
        GenerationError: If too few shipments can host CALIBRATION_EXPIRED.
    """
    count = len(plan)
    counts = scenario_counts(config.scenario_weights, count)
    order = np.argsort(rng.random(count), kind="stable")
    direction = np.where(
        rng.random(count) < 0.5, ExcursionDirection.HIGH.value, ExcursionDirection.LOW.value
    )
    scenario = np.full(count, Scenario.NORMAL.value, dtype=object)
    expired_count = counts[Scenario.CALIBRATION_EXPIRED]
    eligible = calibration_expired_eligible(config, plan, devices, expired_count)
    expired_rows = order[eligible[order]][:expired_count]
    if len(expired_rows) < expired_count:
        raise GenerationError(
            f"Only {len(expired_rows)} shipments can host CALIBRATION_EXPIRED "
            f"({expired_count} needed); too few devices expire inside the date window"
        )
    scenario[expired_rows] = Scenario.CALIBRATION_EXPIRED.value
    rest = order[scenario[order] == Scenario.NORMAL.value]
    position = 0
    for name in Scenario:
        if name is Scenario.CALIBRATION_EXPIRED:
            continue
        scenario[rest[position : position + counts[name]]] = name.value
        position += counts[name]

    frame = pd.DataFrame(
        {"shipment_id": plan["shipment_id"].to_numpy(), "scenario": scenario.astype(str)}
    )
    rank = frame.groupby("scenario").cumcount().to_numpy()
    variants = frame["scenario"].map(lambda s: SCENARIO_VARIANTS[Scenario(s)])
    frame["variant"] = [v[r % len(v)].value for v, r in zip(variants, rank, strict=True)]
    frame["direction"] = direction
    return frame


def cumulative_min_trip_min(
    config: AppConfig, policy_codes: pd.Series, policies: pd.DataFrame
) -> np.ndarray:
    """Shortest planned trip that always fits a CUMULATIVE_DURATION design, per shipment.

    The design (:func:`design_segments`) needs ``k x n_max`` excursion readings,
    ``(k - 1) x separation_readings`` between them and ``ramp_readings.max`` free readings at
    both ends, under the *worst* version of the shipment's policy code (the applied version is
    only known after departure). The result is divided by ``drive_time_factor.min`` and padded
    for intermediate stops that dwell less than planned, so the actual transit is long enough.

    Args:
        config: Validated configuration.
        policy_codes: Policy code per shipment.
        policies: The ``threshold_policies`` table.

    Returns:
        Minutes per shipment.
    """
    interval = config.reading_interval_min
    settings, routes = config.generation.scenarios, config.generation.routes
    n_max = policies["max_single_excursion_min"] // interval
    k = policies["max_cumulative_excursion_min"] // (n_max * interval) + 1
    readings = k * n_max + (k - 1) * settings.separation_readings + 2 * settings.ramp_readings.max
    worst = (readings * interval).groupby(policies["policy_code"]).max()
    needed = worst.reindex(policy_codes.to_numpy()).to_numpy(dtype="float64")
    shorter_dwell = routes.intermediate_stops.max * max(
        0, routes.planned_dwell_min - routes.dwell_min.min
    )
    return np.ceil(needed / routes.drive_time_factor.min + shorter_dwell).astype("int64")


def shipment_flags(
    config: AppConfig,
    assignment: pd.DataFrame,
    plan: pd.DataFrame,
    policies: pd.DataFrame,
    min_delay: np.ndarray,
) -> ShipmentFlags:
    """Translate the scenario assignment into flags for the shipment generators.

    Args:
        config: Validated configuration.
        assignment: Output of :func:`assign_scenarios`, aligned with the plan.
        plan: The shipment plan (``policy_code``).
        policies: The ``threshold_policies`` table.
        min_delay: Minimum departure delay per plan row (:func:`force_policy_transitions`).

    Returns:
        The :class:`ShipmentFlags`.
    """
    scenario = assignment["scenario"].to_numpy()
    cumulative = (assignment["variant"] == ScenarioVariant.CUMULATIVE_DURATION.value).to_numpy()
    lowest = int(np.ceil(config.trip_duration_hours.min * 60))
    min_trip = np.where(
        cumulative, cumulative_min_trip_min(config, plan["policy_code"], policies), lowest
    )
    return ShipmentFlags(
        long_dwell=scenario == Scenario.LONG_DWELL.value,
        expired_device=scenario == Scenario.CALIBRATION_EXPIRED.value,
        min_departure_delay_min=np.asarray(min_delay, dtype="int64"),
        min_trip_min=np.maximum(min_trip, lowest).astype("int64"),
    )


# --------------------------------------------------------------------------------------------
# 4. Design
# --------------------------------------------------------------------------------------------


def _uniform_count(u: np.ndarray, low: int | np.ndarray, high: int | np.ndarray) -> np.ndarray:
    """Map uniforms in [0, 1) to integers in the inclusive range [low, high]."""
    return low + np.floor(u * (np.asarray(high) - low + 1)).astype("int64")


def design_segments(
    config: AppConfig,
    assignment: pd.DataFrame,
    shipments: pd.DataFrame,
    loggers: pd.DataFrame,
    applied: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Design the shaped readings of every scenario that needs them.

    With ``n_max = floor(max_single_excursion_min / i)`` under the *applied* policy:

    ============================  ====================================================
    Variant                       Segments (consecutive readings, one direction)
    ============================  ====================================================
    SPIKE (MINOR_SPIKE)           1 excursion of 1 .. ``n_max - 1`` readings
    AT_LIMIT (BOUNDARY)           1 run of ``at_limit_readings`` readings exactly at a limit
    EXACT_ALLOWANCE (BOUNDARY)    1 excursion of exactly ``n_max`` readings
    SINGLE_DURATION (BREACH)      1 excursion of ``n_max + breach_extra_readings`` readings
    ABSOLUTE_LIMIT (BREACH)       1 excursion of 1 .. ``n_max`` readings, one beyond abs limit
    CUMULATIVE_DURATION (BREACH)  ``k`` excursions of ``n_max`` readings with
                                  ``k * n_max * i > max_cumulative_excursion_min``
    ============================  ====================================================

    Segments of one shipment are ``separation_readings`` apart. Every segment also gets a
    ramp of ``ramp_readings`` in-range readings before it (toward the limit) and after it
    (back to normal); ramps of neighbouring segments never overlap because
    ``separation_readings >= 2 x ramp_readings.max`` (config-validated). The block is placed at
    a random position in transit (actual departure → arrival), or anywhere inside the logger
    window if transit is too short, always at least ``ramp_readings.max`` readings from both
    ends of the window, so every ramp fits and every excursion is bounded by in-range readings.

    Args:
        config: Validated configuration.
        assignment: Output of :func:`assign_scenarios`.
        shipments: The shipments frame (actual departure and arrival).
        loggers: The ``loggers`` table.
        applied: Applied policy per shipment (:func:`coldchain.generate.expected.applied_policies`).
        rng: The ``scenarios.design`` stream.

    Returns:
        One row per segment with :data:`SEGMENT_COLUMNS`: ``first_index`` counts readings
        from ``assigned_at``; ``absolute_index`` is the position of the reading beyond the
        absolute limit within the segment (-1 if none); ``ramp_before``/``ramp_after`` are the
        number of in-range ramp readings just before/after it; ``started_at``/``ended_at`` are
        the times of its first and last reading.

    Raises:
        GenerationError: If the allowance is shorter than one reading, or a design does not
            fit its logger window.
    """
    interval = config.reading_interval_min
    settings = config.generation.scenarios
    frame = (
        assignment.merge(
            loggers[["shipment_id", "logger_id", "assigned_at", "unassigned_at"]], on="shipment_id"
        )
        .merge(
            shipments[["shipment_id", "actual_departure_at", "actual_arrival_at"]],
            on="shipment_id",
        )
        .merge(
            applied[["shipment_id", "max_single_excursion_min", "max_cumulative_excursion_min"]],
            on="shipment_id",
        )
    )
    shaped = frame[frame["variant"] != ScenarioVariant.NONE.value].sort_values(
        "shipment_id", kind="stable"
    )
    shaped = shaped.reset_index(drop=True)
    size = len(shaped)
    u_length, u_absolute, u_place = rng.random(size), rng.random(size), rng.random(size)
    variant = shaped["variant"].to_numpy()
    n_max = (shaped["max_single_excursion_min"] // interval).to_numpy()
    if (n_max < 1).any():
        raise GenerationError("max_single_excursion_min is shorter than one reading interval")

    def is_(v: ScenarioVariant) -> np.ndarray:
        return variant == v.value

    extra, at_limit = settings.breach_extra_readings, settings.at_limit_readings
    readings = np.select(
        [
            is_(ScenarioVariant.SPIKE),
            is_(ScenarioVariant.AT_LIMIT),
            is_(ScenarioVariant.SINGLE_DURATION),
            is_(ScenarioVariant.ABSOLUTE_LIMIT),
        ],
        [
            _uniform_count(u_length, 1, np.maximum(n_max - 1, 1)),
            _uniform_count(u_length, at_limit.min, at_limit.max),
            n_max + _uniform_count(u_length, extra.min, extra.max),
            _uniform_count(u_length, 1, n_max),
        ],
        default=n_max,  # EXACT_ALLOWANCE and CUMULATIVE_DURATION
    )
    cumulative = shaped["max_cumulative_excursion_min"].to_numpy()
    segments = np.where(
        is_(ScenarioVariant.CUMULATIVE_DURATION), cumulative // (n_max * interval) + 1, 1
    )
    length = segments * readings + (segments - 1) * settings.separation_readings

    def index_of(times: pd.Series) -> np.ndarray:
        return ((times - shaped["assigned_at"]).dt.total_seconds() / 60 / interval).to_numpy()

    edge = settings.ramp_readings.max  # readings kept free at both ends of the window
    last = index_of(shaped["unassigned_at"]).astype("int64")
    low = np.maximum(edge, np.ceil(index_of(shaped["actual_departure_at"]))).astype("int64")
    high = np.minimum(last - edge, np.floor(index_of(shaped["actual_arrival_at"])))
    high = high.astype("int64") - length + 1
    too_short = high < low
    low = np.where(too_short, edge, low)
    high = np.where(too_short, last - edge - length + 1, high)
    if (high < low).any():
        failing = shaped.loc[high < low, "shipment_id"].tolist()
        raise GenerationError(f"Designed excursions do not fit the logger window of {failing}")
    start = _uniform_count(u_place, low, high)
    absolute = np.where(
        is_(ScenarioVariant.ABSOLUTE_LIMIT), np.floor(u_absolute * readings).astype("int64"), -1
    )

    row = np.repeat(np.arange(size), segments)
    segment = pd.Series(row).groupby(row).cumcount().to_numpy()
    ramp = settings.ramp_readings
    ramp_before = _uniform_count(rng.random(len(row)), ramp.min, ramp.max)
    ramp_after = _uniform_count(rng.random(len(row)), ramp.min, ramp.max)
    first_index = start[row] + segment * (readings[row] + settings.separation_readings)
    assigned = shaped["assigned_at"].iloc[row].reset_index(drop=True)
    started = assigned + pd.to_timedelta(first_index * interval, unit="min")
    ended = started + pd.to_timedelta((readings[row] - 1) * interval, unit="min")
    return pd.DataFrame(
        {
            "shipment_id": shaped["shipment_id"].to_numpy()[row],
            "logger_id": shaped["logger_id"].to_numpy()[row],
            "segment": segment + 1,
            "is_excursion": variant[row] != ScenarioVariant.AT_LIMIT.value,
            "direction": shaped["direction"].to_numpy()[row],
            "first_index": first_index,
            "reading_count": readings[row],
            "absolute_index": absolute[row],
            "ramp_before": ramp_before,
            "ramp_after": ramp_after,
            "started_at": started,
            "ended_at": ended,
        }
    )
