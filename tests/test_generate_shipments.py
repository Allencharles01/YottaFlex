from datetime import date

import numpy as np
import pandas as pd
import pytest

from coldchain.config import parse_config
from coldchain.enums import LotStatus, Scenario, StopType, TableName
from coldchain.errors import GenerationError
from coldchain.generate import generate_tables, shipments
from coldchain.io import raw_tables
from coldchain.rng import stage_rng
from coldchain.schemas import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMAS, effective_policy_ids

MASTER_AND_SHIPMENTS = (
    TableName.PARTIES,
    TableName.THRESHOLD_POLICIES,
    TableName.PRODUCTS,
    TableName.PACKAGING,
    TableName.LOTS,
    TableName.DEVICES,
    TableName.SHIPMENTS,
    TableName.SHIPMENT_LOTS,
    TableName.ROUTE_STOPS,
    TableName.LOGGERS,
)
"""The Phase 2 tables, which this module tests (events and scenarios: test_generate_events.py)."""

GENERATED = MASTER_AND_SHIPMENTS


def _scenario_shipments(tables, scenario: Scenario) -> set[str]:
    outcomes = tables[TableName.EXPECTED_OUTCOMES]
    return set(outcomes.loc[outcomes["scenario"] == scenario, "shipment_id"])


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


# --------------------------------------------------------------------------------------------
# Whole-stage properties
# --------------------------------------------------------------------------------------------


def test_generator_produces_every_raw_table_then_the_ground_truth(starter_tables) -> None:
    assert tuple(starter_tables) == (
        *raw_tables(),
        TableName.EXPECTED_OUTCOMES,
        TableName.DEVICE_TRUTH,
    )


@pytest.mark.parametrize("table", GENERATED)
def test_every_table_passes_its_schema(starter_tables, table: TableName) -> None:
    SCHEMAS[table].validate(starter_tables[table])


@pytest.mark.parametrize("table", GENERATED)
def test_primary_keys_unique_and_sorted(starter_tables, table: TableName) -> None:
    frame = starter_tables[table]
    keys = list(PRIMARY_KEYS[table])
    assert not frame.duplicated(keys).any()
    pd.testing.assert_frame_equal(
        frame, frame.sort_values(keys, kind="stable").reset_index(drop=True)
    )


@pytest.mark.parametrize(
    "fk", [fk for fk in FOREIGN_KEYS if fk[0] in GENERATED and fk[2] in GENERATED], ids=str
)
def test_foreign_keys_resolve(starter_tables, fk) -> None:
    child, column, parent, parent_column = fk
    values = starter_tables[child][column].dropna()
    assert values.isin(starter_tables[parent][parent_column]).all()


def test_starter_volumes(starter_tables, app_config) -> None:
    volumes = app_config.scale("starter")
    assert len(starter_tables[TableName.SHIPMENTS]) == volumes.shipments == 50
    assert len(starter_tables[TableName.DEVICES]) == volumes.devices
    assert len(starter_tables[TableName.LOGGERS]) == volumes.shipments


def test_same_seed_gives_identical_tables(app_config, reference, starter_tables) -> None:
    again = generate_tables(app_config, reference, "starter")
    assert tuple(again) == tuple(starter_tables)
    for table in starter_tables:
        pd.testing.assert_frame_equal(again[table], starter_tables[table])


def test_different_seed_gives_different_shipments(app_config, reference, starter_tables) -> None:
    other = generate_tables(app_config.model_copy(update={"seed": 1}), reference, "starter")
    assert not other[TableName.SHIPMENTS].equals(starter_tables[TableName.SHIPMENTS])


def test_streams_are_independent(raw_config, reference, starter_tables) -> None:
    """Changing the device count only changes the tables that use devices.

    Devices also decide which shipments can host CALIBRATION_EXPIRED; with the default seed the
    extra devices do not change that choice, so every shipment-plan table stays identical.
    """
    raw_config["scales"]["starter"]["devices"] = 30
    other = generate_tables(parse_config(raw_config), reference, "starter")
    for table in GENERATED:
        if table in (TableName.DEVICES, TableName.LOGGERS):
            continue
        pd.testing.assert_frame_equal(other[table], starter_tables[table])
    assert not other[TableName.LOGGERS].equals(starter_tables[TableName.LOGGERS])


# --------------------------------------------------------------------------------------------
# Shipments
# --------------------------------------------------------------------------------------------


def test_shipment_ids_follow_planned_departure(starter_tables) -> None:
    assert starter_tables[TableName.SHIPMENTS]["planned_departure_at"].is_monotonic_increasing


def test_planned_times_inside_config_bounds(starter_tables, app_config) -> None:
    ships = starter_tables[TableName.SHIPMENTS]
    window = app_config.date_window
    departure_day = ships["planned_departure_at"].dt.date
    assert ((departure_day >= window.start) & (departure_day <= window.end)).all()
    hours = (ships["planned_arrival_at"] - ships["planned_departure_at"]).dt.total_seconds() / 3600
    bounds = app_config.trip_duration_hours
    assert hours.between(bounds.min, bounds.max).all()


def test_actual_times_ordered_and_delay_in_range(starter_tables, app_config) -> None:
    ships = starter_tables[TableName.SHIPMENTS]
    assert (ships["actual_arrival_at"] > ships["actual_departure_at"]).all()
    delay = (ships["actual_departure_at"] - ships["planned_departure_at"]).dt.total_seconds() / 60
    bounds = app_config.generation.departure_delay_min
    assert delay.between(bounds.min, bounds.max).all()


def test_shipment_party_roles(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS]
    party_type = starter_tables[TableName.PARTIES].set_index("party_id")["party_type"]
    for column, expected in (
        ("supplier_party_id", "SUPPLIER"),
        ("carrier_party_id", "CARRIER"),
        ("receiver_party_id", "RECEIVER"),
        ("planner_party_id", "PLANNER"),
    ):
        assert (party_type.loc[ships[column]] == expected).all(), column


def test_parties_active_before_every_shipment(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS]
    active = starter_tables[TableName.PARTIES].set_index("party_id")["active_from"]
    earliest = ships["planned_departure_at"].dt.date
    for column in ("supplier_party_id", "carrier_party_id", "receiver_party_id"):
        assert (active.loc[ships[column]].to_numpy() <= earliest.to_numpy()).all()


def test_carrier_claim_follows_expected_action(starter_tables) -> None:
    """DECISIONS.md #38: PASS exactly when the expected action is RELEASE."""
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    passed = ships["carrier_reported_status"] == "PASS"
    assert (passed == (outcomes.loc[ships.index, "expected_action"] == "RELEASE")).all()
    assert (~passed).any()


def _shipment_classes(tables) -> pd.DataFrame:
    """Policy code and supplier of every (shipment, lot) pair."""
    lots = tables[TableName.LOTS].set_index("lot_id")
    products = tables[TableName.PRODUCTS].set_index("product_id")
    pairs = tables[TableName.SHIPMENT_LOTS].copy()
    product = lots.loc[pairs["lot_id"], "product_id"].to_numpy()
    pairs["policy_code"] = products.loc[product, "policy_code"].to_numpy()
    pairs["supplier"] = products.loc[product, "supplier_party_id"].to_numpy()
    return pairs


def test_one_temperature_class_and_supplier_per_shipment(starter_tables) -> None:
    pairs = _shipment_classes(starter_tables)
    assert pairs.groupby("shipment_id")["policy_code"].nunique().eq(1).all()
    supplier = pairs.groupby("shipment_id")["supplier"].first()
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    assert (supplier == ships.loc[supplier.index, "supplier_party_id"]).all()


def test_packaging_supports_class_and_holds_for_trip(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    code = _shipment_classes(starter_tables).groupby("shipment_id")["policy_code"].first()
    packaging = starter_tables[TableName.PACKAGING].set_index("packaging_id")
    used = packaging.loc[ships.loc[code.index, "packaging_id"]]
    assert (used["supported_policy_code"].to_numpy() == code.to_numpy()).all()
    trip_hours = (ships["planned_arrival_at"] - ships["planned_departure_at"]).dt.total_seconds()
    assert (used["max_hold_hours"].to_numpy() * 3600 >= trip_hours.loc[code.index].to_numpy()).all()


def test_declared_policy_is_effective_at_planned_departure(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    code = _shipment_classes(starter_tables).groupby("shipment_id")["policy_code"].first()
    expected = effective_policy_ids(
        starter_tables[TableName.THRESHOLD_POLICIES],
        code,
        ships.loc[code.index, "planned_departure_at"],
    )
    assert (ships.loc[code.index, "declared_policy_id"] == expected).all()


# --------------------------------------------------------------------------------------------
# Lots
# --------------------------------------------------------------------------------------------


def test_every_shipment_has_lots_within_configured_count(starter_tables, app_config) -> None:
    per_shipment = starter_tables[TableName.SHIPMENT_LOTS].groupby("shipment_id").size()
    assert len(per_shipment) == len(starter_tables[TableName.SHIPMENTS])
    bounds = app_config.generation.lots_per_shipment
    assert per_shipment.between(bounds.min, bounds.max).all()


def test_lot_quantities_reconcile(starter_tables) -> None:
    shipped = starter_tables[TableName.SHIPMENT_LOTS].groupby("lot_id")["quantity_units"].sum()
    available = starter_tables[TableName.LOTS].set_index("lot_id")["quantity_units"]
    assert (shipped <= available.loc[shipped.index]).all()


def test_only_released_lots_ship_inside_their_dates(starter_tables) -> None:
    pairs = starter_tables[TableName.SHIPMENT_LOTS]
    lots = starter_tables[TableName.LOTS].set_index("lot_id").loc[pairs["lot_id"]]
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id").loc[pairs["shipment_id"]]
    assert (lots["lot_status"] == LotStatus.RELEASED).all()
    for column in ("planned_departure_at", "actual_departure_at"):
        assert (lots["manufactured_on"].to_numpy() < ships[column].dt.date.to_numpy()).all()
    for column in ("planned_arrival_at", "actual_arrival_at"):
        assert (lots["expires_on"].to_numpy() > ships[column].dt.date.to_numpy()).all()


# --------------------------------------------------------------------------------------------
# Route stops
# --------------------------------------------------------------------------------------------


def test_route_sequences_contiguous_with_origin_first_and_destination_last(
    starter_tables, app_config
) -> None:
    stops = starter_tables[TableName.ROUTE_STOPS]
    grouped = stops.groupby("shipment_id")
    assert (grouped["sequence"].min() == 1).all()
    assert (grouped["sequence"].max() == grouped.size()).all()
    first = stops[stops["sequence"] == 1]
    assert (first["stop_type"] == StopType.ORIGIN).all()
    last = stops.loc[grouped["sequence"].idxmax()]
    assert (last["stop_type"] == StopType.DESTINATION).all()
    middle = grouped.size() - 2
    bounds = app_config.generation.routes.intermediate_stops
    assert middle.between(bounds.min, bounds.max).all()


def test_route_stop_parties(starter_tables) -> None:
    stops = starter_tables[TableName.ROUTE_STOPS]
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    party_type = starter_tables[TableName.PARTIES].set_index("party_id")["party_type"]
    origin = stops[stops["stop_type"] == StopType.ORIGIN]
    destination = stops[stops["stop_type"] == StopType.DESTINATION]
    hubs = stops[stops["stop_type"].isin([StopType.WAREHOUSE, StopType.CROSS_DOCK])]
    assert (
        origin["party_id"].to_numpy() == ships.loc[origin["shipment_id"], "supplier_party_id"]
    ).all()
    assert (
        destination["party_id"].to_numpy()
        == ships.loc[destination["shipment_id"], "receiver_party_id"]
    ).all()
    assert (party_type.loc[hubs["party_id"]] == "WAREHOUSE").all()
    assert not hubs.duplicated(["shipment_id", "party_id"]).any()


def test_route_times_chronological_and_match_shipment(starter_tables, app_config) -> None:
    stops = starter_tables[TableName.ROUTE_STOPS]
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    origin = stops[stops["stop_type"] == StopType.ORIGIN].set_index("shipment_id")
    destination = stops[stops["stop_type"] == StopType.DESTINATION].set_index("shipment_id")
    assert (origin["departed_at"] == ships.loc[origin.index, "actual_departure_at"]).all()
    assert (destination["arrived_at"] == ships.loc[destination.index, "actual_arrival_at"]).all()
    # Every event time of the route, in sequence order, is non-decreasing (arrive < depart < next).
    events = stops.melt(
        id_vars=["shipment_id", "sequence"], value_vars=["arrived_at", "departed_at"]
    ).dropna()
    events["order"] = events["sequence"] * 2 + (events["variable"] == "departed_at")
    events = events.sort_values(["shipment_id", "order"])
    assert (events.groupby("shipment_id")["value"].diff().dropna() > pd.Timedelta(0)).all()
    long_dwell = _scenario_shipments(starter_tables, Scenario.LONG_DWELL)
    baseline = stops[~stops["shipment_id"].isin(long_dwell)]["dwell_min"].dropna()
    assert baseline.between(
        app_config.generation.routes.dwell_min.min, app_config.long_dwell_threshold_min
    ).all()
    # Every LONG_DWELL shipment has exactly one stop above the threshold, inside the range.
    scenario_stops = stops[stops["shipment_id"].isin(long_dwell)]
    above = scenario_stops[scenario_stops["dwell_min"] > app_config.long_dwell_threshold_min]
    assert sorted(above["shipment_id"]) == sorted(long_dwell)
    bounds = app_config.generation.scenarios.long_dwell_min
    assert above["dwell_min"].between(bounds.min, bounds.max).all()


# --------------------------------------------------------------------------------------------
# Loggers
# --------------------------------------------------------------------------------------------


def test_logger_window_covers_trip_on_reading_grid(starter_tables, app_config) -> None:
    loggers = starter_tables[TableName.LOGGERS].set_index("shipment_id")
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id").loc[loggers.index]
    assert (loggers["assigned_at"] <= ships["actual_departure_at"]).all()
    assert (loggers["unassigned_at"] >= ships["actual_arrival_at"]).all()
    interval = app_config.reading_interval_min
    for column in ("assigned_at", "unassigned_at"):
        assert (loggers[column].dt.minute % interval == 0).all()


def test_no_device_has_overlapping_assignments(starter_tables, app_config) -> None:
    loggers = starter_tables[TableName.LOGGERS].sort_values(["device_id", "assigned_at"])
    previous_end = loggers.groupby("device_id")["unassigned_at"].shift()
    gap = loggers["assigned_at"] - previous_end
    turnaround = pd.Timedelta(minutes=app_config.generation.devices.turnaround_min)
    assert (gap.dropna() >= turnaround).all()
    assert loggers["device_id"].duplicated().any()  # devices really are reused


def test_device_calibration_covers_every_assignment_except_expired_scenario(
    starter_tables,
) -> None:
    expired = _scenario_shipments(starter_tables, Scenario.CALIBRATION_EXPIRED)
    loggers = starter_tables[TableName.LOGGERS]
    devices = starter_tables[TableName.DEVICES].set_index("device_id").loc[loggers["device_id"]]
    covered = (devices["calibrated_on"].to_numpy() <= loggers["assigned_at"].dt.date.to_numpy()) & (
        devices["calibration_due_on"].to_numpy() >= loggers["unassigned_at"].dt.date.to_numpy()
    )
    assert expired
    assert set(loggers.loc[~covered, "shipment_id"]) == expired
    # CALIBRATION_EXPIRED devices were already past due when assigned (DECISIONS.md).
    is_expired = loggers["shipment_id"].isin(expired).to_numpy()
    due = devices["calibration_due_on"].to_numpy()[is_expired]
    assert (due < loggers["assigned_at"].dt.date.to_numpy()[is_expired]).all()


# --------------------------------------------------------------------------------------------
# Unit tests of the building blocks
# --------------------------------------------------------------------------------------------


def test_great_circle_distance() -> None:
    mumbai, delhi = (19.08, 72.88), (28.61, 77.21)
    km = shipments.great_circle_km(
        np.array([mumbai[0]]), np.array([mumbai[1]]), np.array([delhi[0]]), np.array([delhi[1]])
    )
    assert km[0] == pytest.approx(1150, abs=20)
    assert (
        shipments.great_circle_km(
            np.array([1.0]), np.array([2.0]), np.array([1.0]), np.array([2.0])
        )[0]
        == 0
    )


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-01-01T00:00:00Z", "THR-REFRIG_2_8-V1"),
        ("2026-06-30T23:59:00Z", "THR-REFRIG_2_8-V1"),
        ("2026-07-01T00:00:00Z", "THR-REFRIG_2_8-V2"),
        ("2030-01-01T00:00:00Z", "THR-REFRIG_2_8-V2"),
        ("2025-12-31T23:59:00Z", None),
    ],
)
def test_effective_policy_resolution_boundaries(starter_tables, instant: str, expected) -> None:
    result = effective_policy_ids(
        starter_tables[TableName.THRESHOLD_POLICIES],
        pd.Series(["REFRIG_2_8"], index=[7]),
        pd.Series([_utc(instant)], index=[7]),
    )
    assert result.index.tolist() == [7]
    value = result.iloc[0]
    assert (pd.isna(value) and expected is None) or value == expected


def test_effective_policy_unknown_code_is_missing(starter_tables) -> None:
    result = effective_policy_ids(
        starter_tables[TableName.THRESHOLD_POLICIES],
        pd.Series(["NOPE"]),
        pd.Series([_utc("2026-03-01")]),
    )
    assert result.isna().all()


def _plan(rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    """Minimal scheduled plan: (shipment_id, actual departure, actual arrival)."""
    return pd.DataFrame(
        {
            "shipment_id": [r[0] for r in rows],
            "actual_departure_at": [_utc(r[1]) for r in rows],
            "actual_arrival_at": [_utc(r[2]) for r in rows],
        }
    )


def _devices(*due: date) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "device_id": [f"DEV-{i:06d}" for i in range(1, len(due) + 1)],
            "calibrated_on": [date(2025, 12, 1)] * len(due),
            "calibration_due_on": list(due),
        }
    )


def test_assign_loggers_fails_without_a_free_device(app_config) -> None:
    plan = _plan(
        [
            ("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00"),
            ("SHP-2026-000002", "2026-03-01T12:00", "2026-03-02T12:00"),
        ]
    )
    with pytest.raises(GenerationError, match="No free, calibrated device for SHP-2026-000002"):
        shipments.assign_loggers(app_config, plan, _devices(date(2027, 1, 1)), stage_rng(1, "x"))


def test_assign_loggers_reuses_a_device_after_turnaround(app_config) -> None:
    plan = _plan(
        [
            ("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00"),
            ("SHP-2026-000002", "2026-03-03T08:00", "2026-03-04T08:00"),
        ]
    )
    loggers = shipments.assign_loggers(
        app_config, plan, _devices(date(2027, 1, 1)), stage_rng(1, "x")
    )
    assert loggers["device_id"].tolist() == ["DEV-000001", "DEV-000001"]


def test_assign_loggers_skips_devices_whose_calibration_expires(app_config) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")])
    devices = _devices(date(2026, 3, 1), date(2027, 1, 1))  # first expires before the trip ends
    for seed in range(5):
        loggers = shipments.assign_loggers(app_config, plan, devices, stage_rng(seed, "x"))
        assert loggers["device_id"].tolist() == ["DEV-000002"]
    with pytest.raises(GenerationError):
        shipments.assign_loggers(app_config, plan, _devices(date(2026, 3, 1)), stage_rng(1, "x"))


def test_assign_loggers_gives_expired_flag_an_already_expired_device(app_config) -> None:
    plan = _plan(
        [
            ("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00"),
            ("SHP-2026-000002", "2026-03-01T09:00", "2026-03-02T09:00"),
        ]
    )
    devices = _devices(date(2026, 2, 1), date(2027, 1, 1), date(2026, 3, 1))
    for seed in range(5):
        loggers = shipments.assign_loggers(
            app_config, plan, devices, stage_rng(seed, "x"), np.array([True, False])
        )
        # DEV-000003 expires during the trip: neither valid nor already past due.
        assert loggers["device_id"].tolist() == ["DEV-000001", "DEV-000002"]
    with pytest.raises(GenerationError, match="No free, expired device for SHP-2026-000001"):
        shipments.assign_loggers(
            app_config, plan, _devices(date(2027, 1, 1)), stage_rng(1, "x"), np.array([True, False])
        )


def test_long_dwell_flag_needs_an_intermediate_stop(app_config) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")])
    stops = pd.DataFrame(
        {
            "shipment_id": ["SHP-2026-000001"] * 2,
            "sequence": [1, 2],
            "stop_type": ["ORIGIN", "DESTINATION"],
            "party_id": ["PTY-0001", "PTY-0004"],
            "leg_km": [0.0, 100.0],
        }
    )
    plan = plan.assign(planned_departure_at=plan["actual_departure_at"])
    flags = shipments.ShipmentFlags.baseline(app_config, 1)
    flags = shipments.ShipmentFlags(
        long_dwell=np.array([True]),
        expired_device=flags.expired_device,
        min_departure_delay_min=flags.min_departure_delay_min,
    )
    with pytest.raises(GenerationError, match="LONG_DWELL shipments without an intermediate"):
        shipments.schedule_trips(app_config, plan, stops, stage_rng(1, "x"), flags)


def test_minimum_departure_delay_is_respected(app_config) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")])
    plan = plan.assign(planned_departure_at=plan["actual_departure_at"])
    stops = pd.DataFrame(
        {
            "shipment_id": ["SHP-2026-000001"] * 2,
            "sequence": [1, 2],
            "stop_type": ["ORIGIN", "DESTINATION"],
            "party_id": ["PTY-0001", "PTY-0004"],
            "leg_km": [0.0, 100.0],
        }
    )
    base = shipments.ShipmentFlags.baseline(app_config, 1)
    flags = shipments.ShipmentFlags(base.long_dwell, base.expired_device, np.array([115]))
    for seed in range(5):
        scheduled, _ = shipments.schedule_trips(
            app_config, plan, stops, stage_rng(seed, "x"), flags
        )
        delay = scheduled["actual_departure_at"] - scheduled["planned_departure_at"]
        assert (delay >= pd.Timedelta(minutes=115)).all()


def test_minimum_trip_stretches_the_plan_and_is_bounded(app_config) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")])
    plan = plan.assign(planned_departure_at=plan["actual_departure_at"])
    stops = pd.DataFrame(
        {
            "shipment_id": ["SHP-2026-000001"] * 2,
            "sequence": [1, 2],
            "stop_type": ["ORIGIN", "DESTINATION"],
            "party_id": ["PTY-0001", "PTY-0004"],
            "leg_km": [0.0, 100.0],  # 120 min of driving: clipped up to the 240-min minimum
        }
    )
    base = shipments.ShipmentFlags.baseline(app_config, 1)
    stretched = shipments.ShipmentFlags(
        base.long_dwell, base.expired_device, base.min_departure_delay_min, np.array([600])
    )
    scheduled, _ = shipments.schedule_trips(app_config, plan, stops, stage_rng(1, "x"), stretched)
    planned = scheduled["planned_arrival_at"] - scheduled["planned_departure_at"]
    assert (planned == pd.Timedelta(minutes=600)).all()
    too_long = shipments.ShipmentFlags(
        base.long_dwell, base.expired_device, base.min_departure_delay_min, np.array([72 * 60 + 1])
    )
    with pytest.raises(GenerationError, match=r"longer than trip_duration_hours\.max"):
        shipments.schedule_trips(app_config, plan, stops, stage_rng(1, "x"), too_long)


def test_assign_loggers_due_date_is_inclusive(app_config) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")])
    loggers = shipments.assign_loggers(
        app_config, plan, _devices(date(2026, 3, 2)), stage_rng(1, "x")
    )
    assert loggers["device_id"].tolist() == ["DEV-000001"]


def test_allocate_lots_fails_without_eligible_lot(app_config, starter_tables) -> None:
    plan = _plan([("SHP-2026-000001", "2026-03-01T08:00", "2026-03-02T08:00")]).assign(
        primary_product_id="PRD-0001",
        planned_departure_at=_utc("2026-03-01T08:00"),
        planned_arrival_at=_utc("2026-03-02T08:00"),
    )
    lots = starter_tables[TableName.LOTS].assign(lot_status=LotStatus.ON_HOLD.value)
    with pytest.raises(GenerationError, match="No eligible released lot"):
        shipments.allocate_lots(
            app_config, plan, starter_tables[TableName.PRODUCTS], lots, stage_rng(1, "x")
        )


def test_choose_packaging_fails_when_trip_outlasts_every_profile(starter_tables) -> None:
    plan = pd.DataFrame(
        {
            "shipment_id": ["SHP-2026-000001"],
            "policy_code": ["REFRIG_2_8"],
            "planned_departure_at": [_utc("2026-03-01")],
            "planned_arrival_at": [_utc("2026-04-01")],  # 31 days > every max_hold_hours
        }
    )
    with pytest.raises(GenerationError, match="No compatible packaging"):
        shipments.choose_packaging(plan, starter_tables[TableName.PACKAGING], stage_rng(1, "x"))


@pytest.mark.parametrize("count", [0, 10**6])
def test_shipment_count_bounds(app_config, starter_tables, count: int) -> None:
    with pytest.raises(GenerationError, match="Shipment count"):
        shipments.plan_shipments(
            app_config,
            count,
            starter_tables[TableName.PARTIES],
            starter_tables[TableName.PRODUCTS],
            stage_rng(1, "x"),
        )


def test_planned_dwell_that_leaves_no_drive_time_is_rejected(raw_config, reference) -> None:
    raw_config["trip_duration_hours"] = {"min": 1, "max": 2}
    raw_config["generation"]["routes"]["intermediate_stops"] = {"min": 3, "max": 3}
    with pytest.raises(GenerationError, match="no drive time"):
        generate_tables(parse_config(raw_config), reference, "starter")


def test_same_city_trip_gets_the_minimum_duration(raw_config, reference, app_config) -> None:
    """With no hubs and every party in one city, every planned trip is clipped to the minimum.

    The exception is a CUMULATIVE_DURATION breach, whose trip is stretched to fit its design.
    """
    raw_config["generation"]["routes"]["intermediate_stops"] = {"min": 0, "max": 0}
    weights = raw_config["scenario_weights"]
    weights["NORMAL"] += weights["LONG_DWELL"]  # LONG_DWELL needs an intermediate stop
    weights["LONG_DWELL"] = 0.0
    config = parse_config(raw_config)
    one_city = reference.model_copy(
        update={"locations": tuple(loc for loc in reference.locations if loc.city == "Mumbai")}
    )
    tables = generate_tables(config, one_city, "starter")
    ships = tables[TableName.SHIPMENTS]
    hours = (ships["planned_arrival_at"] - ships["planned_departure_at"]).dt.total_seconds() / 3600
    outcomes = tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    codes = outcomes.loc[ships["shipment_id"], "expected_reason_codes"].fillna("")
    stretched = codes.str.contains("CUMULATIVE_DURATION_BREACH").to_numpy()
    assert stretched.sum() == 1
    assert (hours[~stretched] == app_config.trip_duration_hours.min).all()
    assert (hours[stretched] > app_config.trip_duration_hours.min).all()
    assert (tables[TableName.ROUTE_STOPS].groupby("shipment_id").size() == 2).all()
