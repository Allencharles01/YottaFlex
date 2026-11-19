"""Event tables derived from the route stops: custody handoffs and the shipment status history.

**Custody chain** (continuous: each event's ``to_party_id`` is the next event's
``from_party_id``):

* PICKUP at the ORIGIN, supplier → carrier, at the origin's ``departed_at``;
* at every intermediate stop, a HANDOFF carrier → stop party at ``arrived_at`` and a HANDOFF
  stop party → carrier at ``departed_at``;
* DELIVERY at the DESTINATION, carrier → receiver, at its ``arrived_at``.

**Status history** (``PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → DELIVERED``):

* PLANNED by the planner, ``planning_lead_hours`` before ``planned_departure_at``;
* PICKED_UP and IN_TRANSIT by the carrier at ``actual_departure_at``;
* per intermediate stop, AT_STOP by the stop's party at ``arrived_at`` and IN_TRANSIT by the
  carrier at ``departed_at``;
* DELIVERED by the receiver at ``actual_arrival_at``.

Both tables are built with vectorised frame operations and numbered 1..n per shipment.
"""

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.config import AppConfig
from coldchain.enums import CustodyEventType, ShipmentStatus, StopType, TableName
from coldchain.schemas import SCHEMAS

STAGES: dict[str, str] = {"status": "events.status"}
"""RNG stream name of every random step of the event stage."""

_PARTY_COLUMNS = ["shipment_id", "supplier_party_id", "carrier_party_id", "receiver_party_id"]


def _number(events: pd.DataFrame, builder) -> pd.DataFrame:
    """Order events per shipment by (``rank``, ``phase``) and assign sequences and IDs."""
    events = events.sort_values(["shipment_id", "rank", "phase"], kind="stable")
    events = events.reset_index(drop=True)
    events["sequence"] = events.groupby("shipment_id").cumcount().astype("int64") + 1
    return events.assign(event_id=builder(events["shipment_id"], events["sequence"]))


def custody_events(shipments: pd.DataFrame, route_stops: pd.DataFrame) -> pd.DataFrame:
    """Build the continuous custody chain of every shipment from its route stops.

    Args:
        shipments: The shipments frame (supplier, carrier and receiver party IDs).
        route_stops: The ``route_stops`` table.

    Returns:
        The ``custody_events`` table (schema columns).
    """
    stops = route_stops.merge(shipments[_PARTY_COLUMNS], on="shipment_id")
    kind = stops["stop_type"]
    origin = stops[kind == StopType.ORIGIN]
    destination = stops[kind == StopType.DESTINATION]
    hubs = stops[kind.isin([StopType.WAREHOUSE, StopType.CROSS_DOCK])]

    def events(
        frame: pd.DataFrame, event_type: CustodyEventType, source: str, target: str, at: str, phase
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "shipment_id": frame["shipment_id"].to_numpy(),
                "stop_id": frame["stop_id"].to_numpy(),
                "event_type": event_type.value,
                "from_party_id": frame[source].to_numpy(),
                "to_party_id": frame[target].to_numpy(),
                "event_at": frame[at].to_numpy(),
                "rank": frame["sequence"].to_numpy(),
                "phase": phase,
            }
        )

    frames = [
        events(
            origin,
            CustodyEventType.PICKUP,
            "supplier_party_id",
            "carrier_party_id",
            "departed_at",
            1,
        ),
        events(hubs, CustodyEventType.HANDOFF, "carrier_party_id", "party_id", "arrived_at", 0),
        events(hubs, CustodyEventType.HANDOFF, "party_id", "carrier_party_id", "departed_at", 1),
        events(
            destination,
            CustodyEventType.DELIVERY,
            "carrier_party_id",
            "receiver_party_id",
            "arrived_at",
            0,
        ),
    ]
    chain = _number(pd.concat(frames, ignore_index=True), ids.custody_event_ids)
    chain = chain.rename(columns={"event_id": "custody_event_id"})
    return chain[list(SCHEMAS[TableName.CUSTODY_EVENTS].columns)]


def status_events(
    config: AppConfig,
    shipments: pd.DataFrame,
    route_stops: pd.DataFrame,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Build the raw lifecycle history of every shipment.

    Args:
        config: Validated configuration (``planning_lead_hours``).
        shipments: The shipments frame.
        route_stops: The ``route_stops`` table.
        rng: The ``events.status`` stream.

    Returns:
        The ``shipment_status_events`` table (schema columns).
    """
    lead = config.generation.planning_lead_hours
    hours = rng.integers(lead.min, lead.max + 1, size=len(shipments))
    ships = shipments.reset_index(drop=True)
    final = route_stops.groupby("shipment_id")["sequence"].max()
    destination = final.reindex(ships["shipment_id"]).to_numpy()

    def events(
        shipment: pd.Series, status: ShipmentStatus, at: pd.Series, actor: pd.Series, rank, phase
    ) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "shipment_id": shipment.to_numpy(),
                "status": status.value,
                "event_at": at.to_numpy(),
                "actor_party_id": actor.to_numpy(),
                "rank": rank,
                "phase": phase,
            }
        )

    sid, carrier = ships["shipment_id"], ships["carrier_party_id"]
    departure = ships["actual_departure_at"]
    hubs = route_stops[route_stops["stop_type"].isin([StopType.WAREHOUSE, StopType.CROSS_DOCK])]
    hub_carrier = hubs[["shipment_id"]].merge(ships[["shipment_id", "carrier_party_id"]])
    frames = [
        events(
            sid,
            ShipmentStatus.PLANNED,
            ships["planned_departure_at"] - pd.to_timedelta(hours, unit="h"),
            ships["planner_party_id"],
            0,
            0,
        ),
        events(sid, ShipmentStatus.PICKED_UP, departure, carrier, 1, 0),
        events(sid, ShipmentStatus.IN_TRANSIT, departure, carrier, 1, 1),
        events(
            hubs["shipment_id"],
            ShipmentStatus.AT_STOP,
            hubs["arrived_at"],
            hubs["party_id"],
            hubs["sequence"].to_numpy(),
            0,
        ),
        events(
            hubs["shipment_id"],
            ShipmentStatus.IN_TRANSIT,
            hubs["departed_at"],
            hub_carrier["carrier_party_id"],
            hubs["sequence"].to_numpy(),
            1,
        ),
        events(
            sid,
            ShipmentStatus.DELIVERED,
            ships["actual_arrival_at"],
            ships["receiver_party_id"],
            destination,
            0,
        ),
    ]
    history = _number(pd.concat(frames, ignore_index=True), ids.status_event_ids)
    history = history.rename(columns={"event_id": "status_event_id"})
    return history[list(SCHEMAS[TableName.SHIPMENT_STATUS_EVENTS].columns)]
