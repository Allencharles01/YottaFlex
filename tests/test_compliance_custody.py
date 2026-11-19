"""Custody chain and dwell (compliance_rules.md §1.8, §1.9)."""

import pandas as pd
import pytest

from coldchain.compliance.custody import custody_gap_flags, long_dwell_flags
from coldchain.enums import TableName
from conftest import SHP, SHP2, add_back_to_back_shipment


def _gap(raw) -> pd.Series:
    return custody_gap_flags(raw[TableName.SHIPMENTS], raw[TableName.CUSTODY_EVENTS])


def test_continuous_chain_passes(raw_one) -> None:
    assert not _gap(raw_one).loc[SHP]


def test_deleted_middle_handoff_breaks_the_chain(raw_one) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    raw_one[TableName.CUSTODY_EVENTS] = events[events["sequence"] != 3].reset_index(drop=True)
    assert _gap(raw_one).loc[SHP]


def test_non_contiguous_sequence_alone_is_a_gap(raw_one) -> None:
    """Sequences 1, 2, 3, 5: parties still chain, but an event is missing."""
    events = raw_one[TableName.CUSTODY_EVENTS]
    events.loc[events["sequence"] == 4, "sequence"] = 5
    assert _gap(raw_one).loc[SHP]


def test_broken_party_handoff_is_a_gap(raw_one) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    events.loc[events["sequence"] == 3, "from_party_id"] = "PTY-0001"
    assert _gap(raw_one).loc[SHP]


@pytest.mark.parametrize(
    ("column", "value"),
    [("event_type", "HANDOFF"), ("from_party_id", "PTY-0005")],
)
def test_first_event_must_be_pickup_from_supplier(raw_one, column, value) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    events.loc[events["sequence"] == 1, column] = value
    assert _gap(raw_one).loc[SHP]


@pytest.mark.parametrize(
    ("column", "value"),
    [("event_type", "HANDOFF"), ("to_party_id", "PTY-0005")],
)
def test_last_event_must_be_delivery_to_receiver(raw_one, column, value) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    events.loc[events["sequence"] == 4, column] = value
    assert _gap(raw_one).loc[SHP]


def test_missing_last_event_is_a_gap(raw_one) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    raw_one[TableName.CUSTODY_EVENTS] = events[events["sequence"] != 4].reset_index(drop=True)
    assert _gap(raw_one).loc[SHP]


def test_no_events_is_a_gap(raw_one) -> None:
    raw_one[TableName.CUSTODY_EVENTS] = raw_one[TableName.CUSTODY_EVENTS].iloc[0:0]
    assert _gap(raw_one).loc[SHP]


def test_events_out_of_row_order_are_sorted_by_sequence(raw_one) -> None:
    events = raw_one[TableName.CUSTODY_EVENTS]
    raw_one[TableName.CUSTODY_EVENTS] = events.iloc[::-1].reset_index(drop=True)
    assert not _gap(raw_one).loc[SHP]


def test_shipments_are_judged_separately(raw_one) -> None:
    add_back_to_back_shipment(raw_one)
    events = raw_one[TableName.CUSTODY_EVENTS]
    broken = (events["shipment_id"] == SHP2) & (events["sequence"] == 2)
    raw_one[TableName.CUSTODY_EVENTS] = events[~broken].reset_index(drop=True)
    flags = _gap(raw_one)
    assert not flags.loc[SHP] and flags.loc[SHP2]


@pytest.mark.parametrize(("dwell", "flag"), [(30, False), (120, False), (121, True)])
def test_long_dwell_boundary(raw_one, dwell, flag) -> None:
    stops = raw_one[TableName.ROUTE_STOPS]
    stops.loc[stops["sequence"] == 2, "dwell_min"] = dwell
    flags = long_dwell_flags(raw_one[TableName.SHIPMENTS], stops, 120)
    assert flags.loc[SHP] == flag


def test_long_dwell_ignores_null_dwell_and_missing_stops(raw_one) -> None:
    stops = raw_one[TableName.ROUTE_STOPS]
    assert stops["dwell_min"].isna().sum() == 2
    assert not long_dwell_flags(raw_one[TableName.SHIPMENTS], stops, 120).loc[SHP]
    assert not long_dwell_flags(raw_one[TableName.SHIPMENTS], stops.iloc[0:0], 120).loc[SHP]
