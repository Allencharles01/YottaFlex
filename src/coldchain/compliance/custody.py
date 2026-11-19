"""Custody chain and route-stop dwell (compliance_rules.md §1.8 and §1.9).

CUSTODY_GAP when a shipment's custody events (sorted by ``sequence``):

* are missing entirely;
* are not numbered contiguously ``1..n`` (an event is missing);
* break the chain (``from_party_id`` ≠ the previous event's ``to_party_id``);
* do not start with a PICKUP from the shipment's supplier;
* do not end with a DELIVERY to the shipment's receiver.

LONG_DWELL when any route stop dwells longer than ``long_dwell_threshold_min`` (strict).
"""

import pandas as pd

from coldchain.enums import CustodyEventType, ReasonCode


def custody_gap_flags(shipments: pd.DataFrame, custody_events: pd.DataFrame) -> pd.Series:
    """Evaluate CUSTODY_GAP for every shipment.

    Args:
        shipments: The ``shipments`` table (``supplier_party_id``, ``receiver_party_id``).
        custody_events: The ``custody_events`` table.

    Returns:
        Boolean Series indexed by ``shipment_id`` (``shipments`` order).
    """
    index = pd.Index(shipments["shipment_id"], name="shipment_id")
    events = custody_events.sort_values(["shipment_id", "sequence"], kind="stable")
    events = events.merge(
        shipments[["shipment_id", "supplier_party_id", "receiver_party_id"]],
        on="shipment_id",
        how="inner",
    )
    by_ship = events.groupby("shipment_id", sort=False)
    position = by_ship.cumcount() + 1
    count = by_ship["sequence"].transform("size")
    previous_to = by_ship["to_party_id"].shift()
    is_first = position == 1
    is_last = position == count

    broken = (
        (events["sequence"] != position)
        | (~is_first & (events["from_party_id"] != previous_to))
        | (
            is_first
            & (
                (events["event_type"] != CustodyEventType.PICKUP.value)
                | (events["from_party_id"] != events["supplier_party_id"])
            )
        )
        | (
            is_last
            & (
                (events["event_type"] != CustodyEventType.DELIVERY.value)
                | (events["to_party_id"] != events["receiver_party_id"])
            )
        )
    )
    flags = broken.groupby(events["shipment_id"]).any()
    has_events = index.isin(flags.index)
    result = flags.reindex(index, fill_value=False).to_numpy(dtype=bool) | ~has_events
    return pd.Series(result, index=index, name=ReasonCode.CUSTODY_GAP.value)


def long_dwell_flags(
    shipments: pd.DataFrame, route_stops: pd.DataFrame, threshold_min: int
) -> pd.Series:
    """Evaluate LONG_DWELL for every shipment.

    Args:
        shipments: The ``shipments`` table.
        route_stops: The ``route_stops`` table (``dwell_min`` is null at ORIGIN/DESTINATION).
        threshold_min: ``long_dwell_threshold_min`` from the config.

    Returns:
        Boolean Series indexed by ``shipment_id`` (``shipments`` order).
    """
    index = pd.Index(shipments["shipment_id"], name="shipment_id")
    long_stop = (route_stops["dwell_min"] > threshold_min).fillna(False).astype(bool)
    flags = long_stop.groupby(route_stops["shipment_id"]).any()
    return flags.reindex(index, fill_value=False).astype(bool).rename(ReasonCode.LONG_DWELL.value)
