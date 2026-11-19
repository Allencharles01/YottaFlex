import pandas as pd
import pytest

from coldchain.enums import ShipmentStatus, StopType, TableName
from coldchain.generate import events
from coldchain.rng import stage_rng
from coldchain.schemas import TIMESTAMP_DTYPE

ALLOWED_NEXT = {
    ShipmentStatus.PLANNED: {ShipmentStatus.PICKED_UP},
    ShipmentStatus.PICKED_UP: {ShipmentStatus.IN_TRANSIT},
    ShipmentStatus.IN_TRANSIT: {ShipmentStatus.AT_STOP, ShipmentStatus.DELIVERED},
    ShipmentStatus.AT_STOP: {ShipmentStatus.IN_TRANSIT},
    ShipmentStatus.DELIVERED: set(),
}
"""The generator's lifecycle (data_dictionary.md, Lifecycle states)."""


def _with_previous(frame: pd.DataFrame, *columns: str) -> pd.DataFrame:
    ordered = frame.sort_values(["shipment_id", "sequence"]).reset_index(drop=True)
    for column in columns:
        ordered[f"previous_{column}"] = ordered.groupby("shipment_id")[column].shift()
    return ordered


# --------------------------------------------------------------------------------------------
# Custody
# --------------------------------------------------------------------------------------------


def test_custody_sequences_contiguous_and_counted(starter_tables) -> None:
    custody = starter_tables[TableName.CUSTODY_EVENTS]
    stops = starter_tables[TableName.ROUTE_STOPS]
    grouped = custody.groupby("shipment_id")["sequence"]
    assert (grouped.min() == 1).all() and (grouped.max() == grouped.size()).all()
    hubs = stops[stops["stop_type"].isin([StopType.WAREHOUSE, StopType.CROSS_DOCK])]
    expected = 2 + 2 * hubs.groupby("shipment_id").size().reindex(
        grouped.size().index, fill_value=0
    )
    assert (grouped.size() == expected).all()


def test_custody_chain_is_continuous_and_strictly_timed(starter_tables) -> None:
    chain = _with_previous(starter_tables[TableName.CUSTODY_EVENTS], "to_party_id", "event_at")
    later = chain.dropna(subset=["previous_to_party_id"])
    assert (later["from_party_id"] == later["previous_to_party_id"]).all()
    assert (later["event_at"] > later["previous_event_at"]).all()


def test_custody_starts_with_pickup_and_ends_with_delivery(starter_tables) -> None:
    custody = starter_tables[TableName.CUSTODY_EVENTS]
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    first = custody[custody["sequence"] == 1].set_index("shipment_id")
    last = custody.loc[custody.groupby("shipment_id")["sequence"].idxmax()].set_index("shipment_id")
    assert (first["event_type"] == "PICKUP").all() and (last["event_type"] == "DELIVERY").all()
    assert (first["from_party_id"] == ships.loc[first.index, "supplier_party_id"]).all()
    assert (first["to_party_id"] == ships.loc[first.index, "carrier_party_id"]).all()
    assert (last["to_party_id"] == ships.loc[last.index, "receiver_party_id"]).all()
    assert (first["event_at"] == ships.loc[first.index, "actual_departure_at"]).all()
    assert (last["event_at"] == ships.loc[last.index, "actual_arrival_at"]).all()
    middle = custody[(custody["sequence"] > 1) & (custody["sequence"] < custody["sequence"].max())]
    assert set(middle["event_type"]) <= {"HANDOFF", "DELIVERY"}


def test_custody_events_happen_at_their_stop(starter_tables) -> None:
    custody = starter_tables[TableName.CUSTODY_EVENTS]
    stops = starter_tables[TableName.ROUTE_STOPS].set_index("stop_id").loc[custody["stop_id"]]
    at_stop = (custody["event_at"].to_numpy() == stops["arrived_at"].to_numpy()) | (
        custody["event_at"].to_numpy() == stops["departed_at"].to_numpy()
    )
    assert at_stop.all()
    handoffs = custody["event_type"] == "HANDOFF"
    party = stops["party_id"].to_numpy()
    touches = (custody["from_party_id"].to_numpy() == party) | (
        custody["to_party_id"].to_numpy() == party
    )
    assert touches[handoffs.to_numpy()].all()


# --------------------------------------------------------------------------------------------
# Status history
# --------------------------------------------------------------------------------------------


def test_status_transitions_follow_the_lifecycle(starter_tables) -> None:
    history = _with_previous(starter_tables[TableName.SHIPMENT_STATUS_EVENTS], "status", "event_at")
    grouped = history.groupby("shipment_id")
    assert (grouped["sequence"].min() == 1).all()
    assert (grouped["sequence"].max() == grouped.size()).all()
    assert (grouped["status"].first() == ShipmentStatus.PLANNED).all()
    assert (grouped["status"].last() == ShipmentStatus.DELIVERED).all()
    later = history.dropna(subset=["previous_status"])
    legal = [
        ShipmentStatus(cur) in ALLOWED_NEXT[ShipmentStatus(prev)]
        for prev, cur in zip(later["previous_status"], later["status"], strict=True)
    ]
    assert all(legal)
    assert (later["event_at"] >= later["previous_event_at"]).all()


def test_status_times_and_actors(starter_tables, app_config) -> None:
    history = starter_tables[TableName.SHIPMENT_STATUS_EVENTS]
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    by = {status: history[history["status"] == status] for status in ShipmentStatus}
    ship = lambda frame, column: ships.loc[frame["shipment_id"], column].to_numpy()  # noqa: E731
    picked, delivered = by[ShipmentStatus.PICKED_UP], by[ShipmentStatus.DELIVERED]
    assert (picked["event_at"].to_numpy() == ship(picked, "actual_departure_at")).all()
    assert (delivered["event_at"].to_numpy() == ship(delivered, "actual_arrival_at")).all()
    planned = by[ShipmentStatus.PLANNED]
    lead = (ship(planned, "planned_departure_at") - planned["event_at"].to_numpy()) / pd.Timedelta(
        hours=1
    )
    bounds = app_config.generation.planning_lead_hours
    assert ((lead >= bounds.min) & (lead <= bounds.max)).all()
    assert (planned["actor_party_id"].to_numpy() == ship(planned, "planner_party_id")).all()
    for status in (ShipmentStatus.PICKED_UP, ShipmentStatus.IN_TRANSIT):
        frame = by[status]
        assert (frame["actor_party_id"].to_numpy() == ship(frame, "carrier_party_id")).all()
    assert (delivered["actor_party_id"].to_numpy() == ship(delivered, "receiver_party_id")).all()
    at_stop = by[ShipmentStatus.AT_STOP].merge(
        starter_tables[TableName.ROUTE_STOPS],
        left_on=["shipment_id", "event_at"],
        right_on=["shipment_id", "arrived_at"],
    )
    assert len(at_stop) == len(by[ShipmentStatus.AT_STOP])
    assert (at_stop["actor_party_id"] == at_stop["party_id"]).all()


def test_status_actors_active_on_event_date(starter_tables) -> None:
    history = starter_tables[TableName.SHIPMENT_STATUS_EVENTS]
    active = starter_tables[TableName.PARTIES].set_index("party_id")["active_from"]
    assert (active.loc[history["actor_party_id"]].to_numpy() <= history["event_at"].dt.date).all()


# --------------------------------------------------------------------------------------------
# Hand-made example: one shipment with one warehouse stop
# --------------------------------------------------------------------------------------------


def _ts(*values: str | None) -> pd.Series:
    return pd.Series(pd.to_datetime(list(values), utc=True)).astype(TIMESTAMP_DTYPE)


@pytest.fixture
def one_trip():
    sid = "SHP-2026-000001"
    shipments = pd.DataFrame(
        {
            "shipment_id": [sid],
            "supplier_party_id": ["PTY-0001"],
            "carrier_party_id": ["PTY-0002"],
            "receiver_party_id": ["PTY-0004"],
            "planner_party_id": ["PTY-0005"],
            "planned_departure_at": _ts("2026-03-10 07:50"),
            "actual_departure_at": _ts("2026-03-10 08:00"),
            "actual_arrival_at": _ts("2026-03-10 12:00"),
        }
    )
    stops = pd.DataFrame(
        {
            "stop_id": [f"RST-{sid}-0{i}" for i in (1, 2, 3)],
            "shipment_id": [sid] * 3,
            "sequence": [1, 2, 3],
            "stop_type": ["ORIGIN", "WAREHOUSE", "DESTINATION"],
            "party_id": ["PTY-0001", "PTY-0003", "PTY-0004"],
            "arrived_at": _ts(None, "2026-03-10 10:00", "2026-03-10 12:00"),
            "departed_at": _ts("2026-03-10 08:00", "2026-03-10 10:30", None),
        }
    )
    return shipments, stops


def test_custody_for_one_trip(one_trip) -> None:
    shipments, stops = one_trip
    chain = events.custody_events(shipments, stops)
    assert chain["custody_event_id"].tolist() == [f"CUS-SHP-2026-000001-0{i}" for i in (1, 2, 3, 4)]
    assert chain["event_type"].tolist() == ["PICKUP", "HANDOFF", "HANDOFF", "DELIVERY"]
    assert chain["from_party_id"].tolist() == ["PTY-0001", "PTY-0002", "PTY-0003", "PTY-0002"]
    assert chain["to_party_id"].tolist() == ["PTY-0002", "PTY-0003", "PTY-0002", "PTY-0004"]
    assert chain["stop_id"].str[-2:].tolist() == ["01", "02", "02", "03"]


def test_status_history_for_one_trip(one_trip, app_config) -> None:
    shipments, stops = one_trip
    history = events.status_events(app_config, shipments, stops, stage_rng(1, "x"))
    assert history["status"].tolist() == [
        "PLANNED",
        "PICKED_UP",
        "IN_TRANSIT",
        "AT_STOP",
        "IN_TRANSIT",
        "DELIVERED",
    ]
    assert history["actor_party_id"].tolist() == [
        "PTY-0005",
        "PTY-0002",
        "PTY-0002",
        "PTY-0003",
        "PTY-0002",
        "PTY-0004",
    ]
    assert history["status_event_id"].iloc[-1] == "STE-SHP-2026-000001-06"
    assert (
        history["event_at"].iloc[1:].tolist()
        == _ts(
            "2026-03-10 08:00",
            "2026-03-10 08:00",
            "2026-03-10 10:00",
            "2026-03-10 10:30",
            "2026-03-10 12:00",
        ).tolist()
    )
