"""Shipment generators: shipments, shipment_lots, route_stops and loggers (device assignments).

The stage works in dependency order:

1. **plan**: planned departure, primary product (and so supplier and temperature class),
   carrier, receiver and planner;
2. **routes**: intermediate hub stops, ordered along the origin → destination direction;
3. **schedule**: planned trip length from road distance (clipped to ``trip_duration_hours``),
   then actual departure delay, drive times and dwell, giving every stop's timestamps;
4. **packaging**: a profile that supports the temperature class and holds for the trip;
5. **declared policy**: the version the planner sees, effective at ``planned_departure_at``;
6. **lots**: 1..n lots of products from the same supplier and class, with quantities that
   never exceed a lot's units;
7. **loggers**: one device per shipment, scheduled so no device has overlapping windows and
   every assignment lies inside the device's calibration validity.

Natural scenarios (Phase 3) are assigned *before* this stage and reach it as per-shipment
:class:`ShipmentFlags` (DECISIONS.md #38): a LONG_DWELL shipment gets at least one intermediate
stop and one dwell above ``long_dwell_threshold_min``; a CALIBRATION_EXPIRED shipment gets a
device already past its calibration due date; a policy-transition shipment gets a minimum
departure delay. Without flags, the output is the valid all-NORMAL baseline: dwell never exceeds
``long_dwell_threshold_min`` and every device's calibration covers its assignment. The carrier's
claim is set by the caller from the expected outcome (:mod:`coldchain.generate`).
"""

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.config import AppConfig, ReferenceData
from coldchain.enums import LotStatus, PartyType, StopType, TableName
from coldchain.errors import GenerationError
from coldchain.schemas import SCHEMAS, TIMESTAMP_DTYPE, effective_policy_ids

STAGES: dict[str, str] = {
    "plan": "shipments.plan",
    "routes": "shipments.routes",
    "packaging": "shipments.packaging",
    "lots": "shipments.lots",
    "loggers": "shipments.loggers",
}
"""RNG stream name of every step of the shipment stage."""

EARTH_RADIUS_KM = 6371.0
"""Mean Earth radius used for great-circle distances."""

PAIR_WEIGHT_RANGE = (0.5, 1.5)
"""Relative weight range of one shipment's share of a lot it draws from."""

MINUTES_PER_DAY = 24 * 60


@dataclass(frozen=True)
class ShipmentFlags:
    """Per-shipment scenario flags for the shipment generators, aligned with the plan rows.

    Attributes:
        long_dwell: Force one intermediate stop with a dwell in ``scenarios.long_dwell_min``.
        expired_device: Assign a device whose calibration was already due before the window.
        min_departure_delay_min: Lower bound of the actual departure delay, in minutes.
        min_trip_min: Lower bound of the planned trip, in minutes (``None``: the configured
            ``trip_duration_hours.min`` for every shipment).
    """

    long_dwell: np.ndarray
    expired_device: np.ndarray
    min_departure_delay_min: np.ndarray
    min_trip_min: np.ndarray | None = None

    @classmethod
    def baseline(cls, config: AppConfig, count: int) -> "ShipmentFlags":
        """Flags that change nothing: the all-NORMAL baseline.

        Args:
            config: Validated configuration.
            count: Number of shipments.

        Returns:
            Flags with every switch off and the configured minimum delay.
        """
        return cls(
            long_dwell=np.zeros(count, dtype=bool),
            expired_device=np.zeros(count, dtype=bool),
            min_departure_delay_min=np.full(
                count, config.generation.departure_delay_min.min, dtype="int64"
            ),
        )


def great_circle_km(
    lat1: np.ndarray, lon1: np.ndarray, lat2: np.ndarray, lon2: np.ndarray
) -> np.ndarray:
    """Vectorised haversine distance between coordinate pairs, in km.

    Args:
        lat1: Latitudes of the first points (degrees).
        lon1: Longitudes of the first points (degrees).
        lat2: Latitudes of the second points (degrees).
        lon2: Longitudes of the second points (degrees).

    Returns:
        Distances in km.
    """
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    d_phi, d_lambda = phi2 - phi1, np.radians(np.asarray(lon2) - np.asarray(lon1))
    a = np.sin(d_phi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(a, 0.0, 1.0)))


def _minutes(values: np.ndarray | pd.Series) -> pd.TimedeltaIndex:
    """Whole-minute durations as timedeltas."""
    return pd.to_timedelta(np.asarray(values, dtype="int64"), unit="min")


def _utc_midnight(dates: pd.Series) -> pd.Series:
    """``datetime.date`` values as UTC-midnight timestamps."""
    return pd.to_datetime(dates).dt.tz_localize("UTC")


def _party_pool(parties: pd.DataFrame, party_type: PartyType) -> np.ndarray:
    """IDs of every party of one type, failing fast when there are none."""
    pool = parties.loc[parties["party_type"] == party_type, "party_id"].to_numpy()
    if len(pool) == 0:
        raise GenerationError(f"No {party_type.value} parties to choose from")
    return pool


# --------------------------------------------------------------------------------------------
# 1. Plan
# --------------------------------------------------------------------------------------------


def plan_shipments(
    config: AppConfig,
    count: int,
    parties: pd.DataFrame,
    products: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Draw the planned departure, primary product and parties of every shipment.

    Departures are uniform whole minutes over the date window; shipment IDs are numbered in
    departure order.

    Args:
        config: Validated configuration.
        count: Number of shipments.
        parties: The ``parties`` table.
        products: The ``products`` table.
        rng: The ``shipments.plan`` stream.

    Returns:
        One row per shipment: ``shipment_id``, ``primary_product_id``, ``policy_code``, the four
        party IDs and ``planned_departure_at``.

    Raises:
        GenerationError: If ``count`` does not fit the shipment-ID width or a party type is
            missing.
    """
    if not 1 <= count < 10**6:
        raise GenerationError(f"Shipment count must be within 1..999999, got {count}")
    window = config.date_window
    window_minutes = ((window.end - window.start).days + 1) * MINUTES_PER_DAY
    offsets = np.sort(rng.integers(0, window_minutes, size=count))
    start = pd.Timestamp(window.start).tz_localize("UTC")
    product_rows = products.iloc[rng.integers(0, len(products), size=count)]
    picks = {
        role: rng.choice(_party_pool(parties, party_type), size=count)
        for role, party_type in (
            ("carrier_party_id", PartyType.CARRIER),
            ("receiver_party_id", PartyType.RECEIVER),
            ("planner_party_id", PartyType.PLANNER),
        )
    }
    return pd.DataFrame(
        {
            "shipment_id": pd.Series(
                [ids.shipment_id(n) for n in range(1, count + 1)], dtype="str"
            ),
            "primary_product_id": product_rows["product_id"].to_numpy(),
            "policy_code": product_rows["policy_code"].to_numpy(),
            "supplier_party_id": product_rows["supplier_party_id"].to_numpy(),
            **picks,
            "planned_departure_at": start + _minutes(offsets),
        }
    )


# --------------------------------------------------------------------------------------------
# 2. Routes
# --------------------------------------------------------------------------------------------


def plan_routes(
    config: AppConfig,
    plan: pd.DataFrame,
    parties: pd.DataFrame,
    reference: ReferenceData,
    rng: np.random.Generator,
    force_stop: np.ndarray | None = None,
) -> pd.DataFrame:
    """Build every shipment's ordered stops and the road distance of each leg.

    Each shipment gets ``intermediate_stops`` distinct warehouse parties (capped at the number
    of warehouses): those with the smallest detour (origin → hub → destination minus the direct
    distance), each detour multiplied by a random factor in ``1 ± hub_detour_jitter`` so routes
    vary. Stops are ordered by their projection on the origin → destination vector, so the
    route heads towards the destination.

    Args:
        config: Validated configuration.
        plan: Output of :func:`plan_shipments`.
        parties: The ``parties`` table.
        reference: Reference data (city coordinates).
        rng: The ``shipments.routes`` stream.
        force_stop: Optional boolean per shipment: give it at least one intermediate stop
            (LONG_DWELL needs a stop to dwell at).

    Returns:
        One row per stop: ``shipment_id``, ``sequence``, ``stop_type``, ``party_id`` and
        ``leg_km`` (road distance from the previous stop; 0 at the origin).
    """
    settings = config.generation.routes
    count = len(plan)
    warehouses = _party_pool(parties, PartyType.WAREHOUSE)
    n_stops = np.minimum(
        rng.integers(
            settings.intermediate_stops.min, settings.intermediate_stops.max + 1, size=count
        ),
        len(warehouses),
    )
    if force_stop is not None:
        n_stops = np.where(force_stop, np.maximum(n_stops, 1), n_stops)

    coords = pd.DataFrame(
        [(loc.city, loc.latitude, loc.longitude) for loc in reference.locations],
        columns=["city", "lat", "lon"],
    ).set_index("city")
    party_coords = coords.loc[parties["city"].to_numpy()].set_axis(parties["party_id"].to_numpy())

    def lat_lon(party_ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        located = party_coords.loc[party_ids]
        return located["lat"].to_numpy(), located["lon"].to_numpy()

    # Rank every warehouse per shipment by its (jittered) detour; the best n_stops are used.
    o_lat, o_lon = (a[:, None] for a in lat_lon(plan["supplier_party_id"].to_numpy()))
    d_lat, d_lon = (a[:, None] for a in lat_lon(plan["receiver_party_id"].to_numpy()))
    w_lat, w_lon = (a[None, :] for a in lat_lon(warehouses))
    detour = (
        great_circle_km(o_lat, o_lon, w_lat, w_lon)
        + great_circle_km(w_lat, w_lon, d_lat, d_lon)
        - great_circle_km(o_lat, o_lon, d_lat, d_lon)
    )
    jitter = rng.uniform(
        1 - settings.hub_detour_jitter, 1 + settings.hub_detour_jitter, size=detour.shape
    )
    choice_order = np.argsort(detour * jitter + 1e-9 * rng.random(detour.shape), axis=1)
    ship_idx, slot = np.nonzero(np.arange(len(warehouses)) < n_stops[:, None])
    hub_party = warehouses[choice_order[ship_idx, slot]]
    cross_dock = rng.random(len(ship_idx)) < settings.cross_dock_share

    o_lat, o_lon = lat_lon(plan["supplier_party_id"].to_numpy()[ship_idx])
    d_lat, d_lon = lat_lon(plan["receiver_party_id"].to_numpy()[ship_idx])
    h_lat, h_lon = lat_lon(hub_party)
    span = (d_lat - o_lat) ** 2 + (d_lon - o_lon) ** 2
    progress = np.where(
        span > 0,
        ((h_lat - o_lat) * (d_lat - o_lat) + (h_lon - o_lon) * (d_lon - o_lon))
        / np.where(span > 0, span, 1.0),
        0.0,
    )
    hubs = pd.DataFrame(
        {
            "ship": ship_idx,
            "rank": progress,
            "slot": slot,
            "stop_type": np.where(cross_dock, StopType.CROSS_DOCK.value, StopType.WAREHOUSE.value),
            "party_id": hub_party,
        }
    ).sort_values(["ship", "rank", "slot"], kind="stable")
    hubs["sequence"] = hubs.groupby("ship").cumcount() + 2

    ships = np.arange(count)
    ends = pd.DataFrame(
        {
            "ship": np.concatenate([ships, ships]),
            "stop_type": [StopType.ORIGIN.value] * count + [StopType.DESTINATION.value] * count,
            "party_id": np.concatenate(
                [plan["supplier_party_id"].to_numpy(), plan["receiver_party_id"].to_numpy()]
            ),
            "sequence": np.concatenate([np.ones(count, dtype=int), n_stops + 2]),
        }
    )
    stops = pd.concat([ends, hubs[ends.columns]], ignore_index=True).sort_values(
        ["ship", "sequence"], kind="stable"
    )
    lat, lon = lat_lon(stops["party_id"].to_numpy())
    stops = stops.assign(lat=lat, lon=lon)
    previous = stops.groupby("ship")[["lat", "lon"]].shift()
    leg = great_circle_km(previous["lat"], previous["lon"], stops["lat"], stops["lon"])
    stops["leg_km"] = np.nan_to_num(leg, nan=0.0) * settings.road_factor
    stops["shipment_id"] = plan["shipment_id"].to_numpy()[stops["ship"].to_numpy()]
    return stops[["shipment_id", "sequence", "stop_type", "party_id", "leg_km"]].reset_index(
        drop=True
    )


# --------------------------------------------------------------------------------------------
# 3. Schedule
# --------------------------------------------------------------------------------------------


def _pick_long_dwell_stops(
    plan: pd.DataFrame,
    stops: pd.DataFrame,
    is_hub: np.ndarray,
    long_dwell: np.ndarray,
    rng: np.random.Generator,
) -> np.ndarray:
    """Choose one random intermediate stop per ``long_dwell`` shipment.

    Args:
        plan: The shipment plan.
        stops: The route stops.
        is_hub: Boolean per stop: an intermediate (WAREHOUSE/CROSS_DOCK) stop.
        long_dwell: Boolean per plan row.
        rng: The ``shipments.routes`` stream.

    Returns:
        Boolean per stop: the stop that dwells long.

    Raises:
        GenerationError: If a flagged shipment has no intermediate stop.
    """
    flagged = pd.Series(long_dwell, index=plan["shipment_id"].to_numpy())
    candidate = is_hub & flagged.loc[stops["shipment_id"].to_numpy()].to_numpy()
    key = np.where(candidate, rng.random(len(stops)), -1.0)
    best = pd.Series(key).groupby(stops["shipment_id"].to_numpy()).transform("max").to_numpy()
    chosen = candidate & (key == best)
    missing = sorted(
        set(plan.loc[long_dwell, "shipment_id"]) - set(stops.loc[chosen, "shipment_id"])
    )
    if missing:
        raise GenerationError(f"LONG_DWELL shipments without an intermediate stop: {missing}")
    return chosen


def schedule_trips(
    config: AppConfig,
    plan: pd.DataFrame,
    stops: pd.DataFrame,
    rng: np.random.Generator,
    flags: ShipmentFlags | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Compute planned and actual trip times and every stop's arrival/departure.

    *Planned* trip = road drive time (distance / average speed) + ``planned_dwell_min`` per
    intermediate stop, rounded to whole minutes and clipped to ``trip_duration_hours``. The
    *actual* trip departs ``departure_delay_min`` after the plan; each leg's drive time is the
    planned leg time x ``drive_time_factor``, and each intermediate stop dwells ``dwell_min``.
    With ``flags``, one random intermediate stop of every ``long_dwell`` shipment dwells
    ``scenarios.long_dwell_min`` instead, the departure delay is at least
    ``min_departure_delay_min`` and the planned trip at least ``min_trip_min``.

    Args:
        config: Validated configuration.
        plan: Output of :func:`plan_shipments`.
        stops: Output of :func:`plan_routes`.
        rng: The ``shipments.routes`` stream (continues after :func:`plan_routes`).
        flags: Optional scenario flags (default: the baseline).

    Returns:
        ``(plan, stops)``: the plan with ``planned_arrival_at``, ``actual_departure_at`` and
        ``actual_arrival_at`` added; the stops with ``arrived_at``, ``departed_at`` and
        ``dwell_min``.

    Raises:
        GenerationError: If the planned dwell leaves no time to drive within the maximum trip,
            a ``min_trip_min`` exceeds the maximum trip, or a ``long_dwell`` shipment has no
            intermediate stop.
    """
    flags = flags if flags is not None else ShipmentFlags.baseline(config, len(plan))
    settings = config.generation.routes
    is_origin = (stops["stop_type"] == StopType.ORIGIN).to_numpy()
    is_hub = stops["stop_type"].isin([StopType.WAREHOUSE, StopType.CROSS_DOCK]).to_numpy()
    shipment = stops["shipment_id"]

    raw_drive = stops["leg_km"].to_numpy() / settings.average_speed_kmh * 60.0
    hubs_per_ship = pd.Series(is_hub).groupby(shipment.to_numpy()).sum()
    legs_per_ship = pd.Series(~is_origin).groupby(shipment.to_numpy()).sum()
    drive_per_ship = pd.Series(raw_drive).groupby(shipment.to_numpy()).sum()
    ship_ids = plan["shipment_id"].to_numpy()
    planned_dwell = hubs_per_ship.loc[ship_ids].to_numpy() * settings.planned_dwell_min
    lowest = math.ceil(config.trip_duration_hours.min * 60)
    highest = math.floor(config.trip_duration_hours.max * 60)
    planned_total = np.clip(
        np.round(drive_per_ship.loc[ship_ids].to_numpy() + planned_dwell), lowest, highest
    ).astype("int64")
    if (planned_total - planned_dwell <= 0).any():
        raise GenerationError(
            "Planned dwell leaves no drive time within trip_duration_hours.max; "
            "lower generation.routes.planned_dwell_min or intermediate_stops.max"
        )
    if flags.min_trip_min is not None:
        if (flags.min_trip_min > highest).any():
            raise GenerationError("A scenario needs a trip longer than trip_duration_hours.max")
        planned_total = np.maximum(planned_total, flags.min_trip_min).astype("int64")
    planned_drive = planned_total - planned_dwell

    # Distribute each shipment's planned drive time over its legs, pro rata to distance.
    per_stop = pd.DataFrame({"shipment_id": shipment.to_numpy(), "raw": raw_drive})
    total_raw = drive_per_ship.loc[shipment].to_numpy()
    n_legs = legs_per_ship.loc[shipment].to_numpy()
    share = np.where(total_raw > 0, raw_drive / np.where(total_raw > 0, total_raw, 1.0), 1 / n_legs)
    share = np.where(is_origin, 0.0, share)
    planned_leg = share * pd.Series(planned_drive, index=ship_ids).loc[shipment].to_numpy()
    factor = rng.uniform(
        settings.drive_time_factor.min, settings.drive_time_factor.max, size=len(stops)
    )
    actual_leg = np.where(is_origin, 0, np.maximum(1, np.round(planned_leg * factor))).astype(
        "int64"
    )
    dwell = np.where(
        is_hub,
        rng.integers(settings.dwell_min.min, settings.dwell_min.max + 1, size=len(stops)),
        0,
    ).astype("int64")
    long_stop = _pick_long_dwell_stops(plan, stops, is_hub, flags.long_dwell, rng)
    long_range = config.generation.scenarios.long_dwell_min
    long_dwell = rng.integers(long_range.min, long_range.max + 1, size=len(stops))
    dwell = np.where(long_stop, long_dwell, dwell).astype("int64")
    delay_range = config.generation.departure_delay_min
    delay = rng.integers(delay_range.min, delay_range.max + 1, size=len(plan))
    delay = np.maximum(delay, flags.min_departure_delay_min)

    plan = plan.copy()
    plan["planned_arrival_at"] = plan["planned_departure_at"] + _minutes(planned_total)
    plan["actual_departure_at"] = plan["planned_departure_at"] + _minutes(delay)

    per_stop["step"] = actual_leg + dwell
    elapsed = per_stop.groupby("shipment_id")["step"].cumsum().to_numpy()
    departure = plan.set_index("shipment_id")["actual_departure_at"].loc[shipment]
    departure = departure.reset_index(drop=True)
    arrived = departure + _minutes(elapsed - dwell)
    departed = departure + _minutes(elapsed)
    is_destination = (stops["stop_type"] == StopType.DESTINATION).to_numpy()

    stops = stops.copy()
    stops["arrived_at"] = arrived.where(~is_origin).astype(TIMESTAMP_DTYPE).to_numpy()
    stops["departed_at"] = departed.where(~is_destination).astype(TIMESTAMP_DTYPE).to_numpy()
    stops["dwell_min"] = pd.array(np.where(is_hub, dwell, 0), dtype="Int64")
    stops.loc[~is_hub, "dwell_min"] = pd.NA
    arrival = stops.loc[is_destination].set_index("shipment_id")["arrived_at"]
    plan["actual_arrival_at"] = arrival.loc[ship_ids].to_numpy()
    return plan, stops


# --------------------------------------------------------------------------------------------
# 4. Packaging and 5. declared policy
# --------------------------------------------------------------------------------------------


def choose_packaging(
    plan: pd.DataFrame, packaging: pd.DataFrame, rng: np.random.Generator
) -> pd.Series:
    """Pick a packaging profile per shipment that supports its class and holds for the trip.

    Args:
        plan: Plan with ``policy_code``, ``planned_departure_at`` and ``planned_arrival_at``.
        packaging: The ``packaging`` table.
        rng: The ``shipments.packaging`` stream.

    Returns:
        Packaging ID per shipment, aligned with ``plan``.

    Raises:
        GenerationError: If a shipment has no compatible packaging.
    """
    trip_hours = (plan["planned_arrival_at"] - plan["planned_departure_at"]).dt.total_seconds()
    rows = pd.DataFrame(
        {
            "row": np.arange(len(plan)),
            "policy_code": plan["policy_code"].to_numpy(),
            "trip_hours": trip_hours.to_numpy() / 3600.0,
        }
    )
    candidates = rows.merge(
        packaging[["packaging_id", "supported_policy_code", "max_hold_hours"]],
        left_on="policy_code",
        right_on="supported_policy_code",
    )
    candidates = candidates[candidates["max_hold_hours"] >= candidates["trip_hours"]]
    candidates = candidates.sort_values(["row", "packaging_id"], kind="stable")
    candidates["key"] = rng.random(len(candidates))
    chosen = candidates.sort_values(["row", "key"], kind="stable").drop_duplicates("row")
    if len(chosen) != len(plan):
        missing = sorted(set(range(len(plan))) - set(chosen["row"]))
        raise GenerationError(
            f"No compatible packaging for shipments {plan['shipment_id'].iloc[missing].tolist()}"
        )
    return pd.Series(chosen.set_index("row")["packaging_id"].loc[np.arange(len(plan))].to_numpy())


def declared_policies(plan: pd.DataFrame, policies: pd.DataFrame) -> pd.Series:
    """The planner's declared policy: the version effective at ``planned_departure_at``.

    A shipment planned just before a new version takes effect but departing after it therefore
    declares the old version (DECISIONS.md #26).

    Args:
        plan: Plan with ``policy_code`` and ``planned_departure_at``.
        policies: The ``threshold_policies`` table.

    Returns:
        Declared policy ID per shipment, aligned with ``plan``.

    Raises:
        GenerationError: If no version of a shipment's code is effective at its planned departure.
    """
    declared = effective_policy_ids(policies, plan["policy_code"], plan["planned_departure_at"])
    if declared.isna().any():
        missing = plan.loc[declared.isna(), "shipment_id"].tolist()
        raise GenerationError(f"No effective policy at planned departure for {missing}")
    return declared.astype("str")


# --------------------------------------------------------------------------------------------
# 6. Lots
# --------------------------------------------------------------------------------------------


def allocate_lots(
    config: AppConfig,
    plan: pd.DataFrame,
    products: pd.DataFrame,
    lots: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Choose the lots of every shipment and the quantity shipped from each.

    A shipment has ``lots_per_shipment`` slots. Slot 0 uses its primary product; later slots
    use a random product of the same supplier and temperature class (one container = one
    class). Each slot takes a random RELEASED lot of that product made before the departure
    date and expiring after the arrival date (planned or actual, whichever is stricter). Each
    lot's shipments share ``utilisation`` x its units, so Σ shipped ≤ lot quantity.

    Args:
        config: Validated configuration.
        plan: Scheduled plan (:func:`schedule_trips`).
        products: The ``products`` table.
        lots: The ``lots`` table.
        rng: The ``shipments.lots`` stream.

    Returns:
        The ``shipment_lots`` table.

    Raises:
        GenerationError: If a shipment ends up with no eligible lot, or a lot is too small for
            its shipments.
    """
    per_shipment = config.generation.lots_per_shipment
    n_slots = rng.integers(per_shipment.min, per_shipment.max + 1, size=len(plan))
    slots = plan.loc[
        np.repeat(np.arange(len(plan)), n_slots),
        [
            "shipment_id",
            "primary_product_id",
            "planned_departure_at",
            "actual_departure_at",
            "planned_arrival_at",
            "actual_arrival_at",
        ],
    ].reset_index(drop=True)
    slots["slot"] = slots.groupby("shipment_id").cumcount()

    # Products grouped by (supplier, class): pick a random member of the primary's group.
    grouped = products.assign(
        group=products["supplier_party_id"] + "|" + products["policy_code"]
    ).sort_values(["group", "product_id"], kind="stable")
    grouped["group_start"] = grouped.groupby("group").cumcount().rsub(np.arange(len(grouped)))
    grouped["group_size"] = grouped.groupby("group")["product_id"].transform("size")
    by_product = grouped.set_index("product_id")
    start = by_product.loc[slots["primary_product_id"], "group_start"].to_numpy()
    size = by_product.loc[slots["primary_product_id"], "group_size"].to_numpy()
    member = start + np.floor(rng.random(len(slots)) * size).astype(int)
    slots["product_id"] = np.where(
        slots["slot"] == 0,
        slots["primary_product_id"],
        grouped["product_id"].to_numpy()[member],
    )

    ship_from = slots[["planned_departure_at", "actual_departure_at"]].min(axis=1).dt.floor("D")
    ship_to = slots[["planned_arrival_at", "actual_arrival_at"]].max(axis=1).dt.floor("D")
    slots = slots.assign(ship_from=ship_from, ship_to=ship_to, slot_id=np.arange(len(slots)))
    stock = lots.loc[
        lots["lot_status"] == LotStatus.RELEASED, ["lot_id", "product_id", "quantity_units"]
    ].assign(made=_utc_midnight(lots["manufactured_on"]), expires=_utc_midnight(lots["expires_on"]))
    options = slots.merge(stock, on="product_id")
    options = options[
        (options["made"] < options["ship_from"]) & (options["expires"] > options["ship_to"])
    ].sort_values(["slot_id", "lot_id"], kind="stable")
    options["key"] = rng.random(len(options))
    picked = options.sort_values(["slot_id", "key"], kind="stable").drop_duplicates("slot_id")
    pairs = picked.drop_duplicates(["shipment_id", "lot_id"])[
        ["shipment_id", "lot_id", "quantity_units"]
    ].sort_values(["shipment_id", "lot_id"], kind="stable")
    without = sorted(set(plan["shipment_id"]) - set(pairs["shipment_id"]))
    if without:
        raise GenerationError(f"No eligible released lot for shipments {without}")

    utilisation = config.generation.lots.utilisation
    lot_share = pd.Series(
        rng.uniform(utilisation.min, utilisation.max, size=len(lots)), index=lots["lot_id"]
    )
    weight = rng.uniform(*PAIR_WEIGHT_RANGE, size=len(pairs))
    weight_sum = pd.Series(weight).groupby(pairs["lot_id"].to_numpy()).transform("sum").to_numpy()
    quantity = np.floor(
        pairs["quantity_units"].to_numpy()
        * lot_share.loc[pairs["lot_id"]].to_numpy()
        * weight
        / weight_sum
    ).astype("int64")
    if (quantity < 1).any():
        raise GenerationError("A lot is too small to split over its shipments")
    return pd.DataFrame(
        {
            "shipment_id": pairs["shipment_id"].to_numpy(),
            "lot_id": pairs["lot_id"].to_numpy(),
            "quantity_units": quantity,
        }
    )


# --------------------------------------------------------------------------------------------
# 7. Loggers
# --------------------------------------------------------------------------------------------


def assign_loggers(
    config: AppConfig,
    plan: pd.DataFrame,
    devices: pd.DataFrame,
    rng: np.random.Generator,
    expired_device: np.ndarray | None = None,
) -> pd.DataFrame:
    """Assign one device to every shipment without overlapping any device's windows.

    The window starts ``logger_lead_min`` before the actual departure (floored to the reading
    grid) and ends ``logger_tail_min`` after the actual arrival (ceiled to the grid). Shipments
    are scheduled in window order; each takes a random device that is free (its previous
    window ended at least ``turnaround_min`` earlier) and whose calibration is valid on every
    day of the window. A shipment flagged ``expired_device`` (CALIBRATION_EXPIRED) instead takes
    a free device whose ``calibration_due_on`` is before the window's first day, i.e. a device
    already past its due date. Baseline shipments never use such a device, so expired devices
    are idle. The loop runs over shipments, never over readings.

    Args:
        config: Validated configuration.
        plan: Scheduled plan (:func:`schedule_trips`).
        devices: The ``devices`` table.
        rng: The ``shipments.loggers`` stream.
        expired_device: Optional boolean per plan row (default: none flagged).

    Returns:
        The ``loggers`` table.

    Raises:
        GenerationError: If no suitable free device exists for some shipment.
    """
    expired = (
        np.zeros(len(plan), dtype=bool) if expired_device is None else np.asarray(expired_device)
    )
    settings = config.generation
    grid = f"{config.reading_interval_min}min"
    lead = rng.integers(settings.logger_lead_min.min, settings.logger_lead_min.max + 1, len(plan))
    tail = rng.integers(settings.logger_tail_min.min, settings.logger_tail_min.max + 1, len(plan))
    assigned = (plan["actual_departure_at"] - _minutes(lead)).dt.floor(grid)
    unassigned = (plan["actual_arrival_at"] + _minutes(tail)).dt.ceil(grid)

    def naive(values: pd.Series) -> np.ndarray:
        return values.dt.tz_convert(None).to_numpy().astype("datetime64[us]")

    start, end = naive(assigned), naive(unassigned)
    start_day, end_day = naive(assigned.dt.floor("D")), naive(unassigned.dt.floor("D"))
    valid_from = naive(_utc_midnight(devices["calibrated_on"]))
    valid_to = naive(_utc_midnight(devices["calibration_due_on"]))
    free_at = np.full(len(devices), np.datetime64("1970-01-01T00:00", "us"))
    turnaround = np.timedelta64(settings.devices.turnaround_min, "m")
    device_index = np.empty(len(plan), dtype=np.int64)

    for row in np.lexsort((plan["shipment_id"].to_numpy(), start)):
        if expired[row]:
            suitable = valid_to < start_day[row]
        else:
            suitable = (valid_from <= start_day[row]) & (valid_to >= end_day[row])
        eligible = np.flatnonzero((free_at <= start[row]) & suitable)
        if len(eligible) == 0:
            kind = "expired" if expired[row] else "calibrated"
            raise GenerationError(
                f"No free, {kind} device for {plan['shipment_id'].iloc[row]}; "
                "increase the scale's devices or calibration validity"
            )
        chosen = eligible[rng.integers(len(eligible))]
        device_index[row] = chosen
        free_at[chosen] = end[row] + turnaround

    return pd.DataFrame(
        {
            "logger_id": ids.logger_ids(plan["shipment_id"]),
            "shipment_id": plan["shipment_id"],
            "device_id": devices["device_id"].to_numpy()[device_index],
            "assigned_at": assigned,
            "unassigned_at": unassigned,
        }
    )


# --------------------------------------------------------------------------------------------
# Stage entry point
# --------------------------------------------------------------------------------------------


def generate_shipment_tables(
    config: AppConfig,
    reference: ReferenceData,
    master: dict[TableName, pd.DataFrame],
    plan: pd.DataFrame,
    flags: ShipmentFlags,
    rngs: dict[str, np.random.Generator],
) -> dict[TableName, pd.DataFrame]:
    """Generate shipments, shipment_lots, route_stops and loggers from a shipment plan.

    Args:
        config: Validated configuration.
        reference: Reference data.
        master: Master tables (parties, threshold_policies, products, packaging, lots, devices).
        plan: Output of :func:`plan_shipments` (possibly adjusted by the scenario stage).
        flags: Scenario flags aligned with ``plan``.
        rngs: One generator per key of :data:`STAGES` except ``plan``.

    Returns:
        The four tables, keyed by :class:`TableName`, with their schema columns in schema order
        (dtypes are normalised by the caller, see :mod:`coldchain.generate`). The ``shipments``
        frame has every column except ``carrier_reported_status``, which the caller derives
        from the expected outcome before the table is validated.
    """
    parties = master[TableName.PARTIES]
    stops = plan_routes(config, plan, parties, reference, rngs["routes"], flags.long_dwell)
    plan, stops = schedule_trips(config, plan, stops, rngs["routes"], flags)
    plan["packaging_id"] = choose_packaging(
        plan, master[TableName.PACKAGING], rngs["packaging"]
    ).to_numpy()
    plan["declared_policy_id"] = declared_policies(
        plan, master[TableName.THRESHOLD_POLICIES]
    ).to_numpy()

    route_stops = stops.assign(stop_id=ids.route_stop_ids(stops["shipment_id"], stops["sequence"]))
    tables = {
        TableName.SHIPMENTS: plan,
        TableName.SHIPMENT_LOTS: allocate_lots(
            config, plan, master[TableName.PRODUCTS], master[TableName.LOTS], rngs["lots"]
        ),
        TableName.ROUTE_STOPS: route_stops,
        TableName.LOGGERS: assign_loggers(
            config, plan, master[TableName.DEVICES], rngs["loggers"], flags.expired_device
        ),
    }
    columns = {name: list(SCHEMAS[name].columns) for name in tables}
    columns[TableName.SHIPMENTS].remove("carrier_reported_status")
    return {name: frame[columns[name]] for name, frame in tables.items()}
