"""Shipment decisions and appended status events (compliance_rules.md §2, §1.10 and §4).

Every rule module returns boolean columns named after the :class:`ReasonCode` it evaluates.
:func:`reason_matrix` collects them into one shipment x reason-code matrix; the status is the
most severe status triggered (:data:`REASON_STATUS`, :data:`STATUS_SEVERITY`), the action
follows from it (:data:`STATUS_ACTION`), and finally the carrier's claim is checked against the
action (CARRIER_STATUS_CONTRADICTED, informational).
"""

from collections.abc import Iterable

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.enums import (
    REASON_STATUS,
    STATUS_ACTION,
    STATUS_SEVERITY,
    CarrierReportedStatus,
    ComplianceStatus,
    DecidedAction,
    PartyType,
    ReasonCode,
    ShipmentStatus,
    TableName,
)
from coldchain.errors import ComplianceError
from coldchain.schemas import LIST_SEPARATOR, SCHEMAS

DECISION_COLUMNS = tuple(SCHEMAS[TableName.COMPLIANCE_DECISIONS].columns)
"""Columns of the ``compliance_decisions`` table, in schema order."""

EVENT_COLUMNS = tuple(SCHEMAS[TableName.COMPLIANCE_STATUS_EVENTS].columns)
"""Columns of the ``compliance_status_events`` table, in schema order."""

FINAL_STATUS: dict[DecidedAction, ShipmentStatus] = {
    DecidedAction.RELEASE: ShipmentStatus.RELEASED,
    DecidedAction.QUARANTINE: ShipmentStatus.QUARANTINED,
}
"""Lifecycle status appended after UNDER_REVIEW; REVIEW appends nothing (waits for a human)."""


def reason_matrix(index: pd.Index, sources: Iterable[pd.DataFrame | pd.Series]) -> pd.DataFrame:
    """Collect the reason flags of every rule into one boolean matrix.

    Args:
        index: Shipment IDs (the matrix rows).
        sources: Frames or Series indexed by ``shipment_id``; every column (or Series name)
            that is a :class:`ReasonCode` value is taken as that code's flag.

    Returns:
        Boolean frame indexed like ``index`` with one column per :class:`ReasonCode`, in
        declaration order (codes no rule raised are all ``False``).
    """
    matrix = pd.DataFrame(False, index=index, columns=[code.value for code in ReasonCode])
    for source in sources:
        frame = source.to_frame() if isinstance(source, pd.Series) else source
        for column in frame.columns.intersection(matrix.columns):
            flag = frame[column].reindex(index, fill_value=False).to_numpy(dtype=bool)
            matrix[column] = matrix[column].to_numpy() | flag
    return matrix


def reason_codes_text(matrix: pd.DataFrame) -> pd.Series:
    """Join the raised codes of each row with ``|`` in canonical order (null when none)."""
    text = pd.Series("", index=matrix.index, dtype=object)
    for code in ReasonCode:
        text = text + np.where(matrix[code.value].to_numpy(), code.value + LIST_SEPARATOR, "")
    text = text.str.rstrip(LIST_SEPARATOR)
    return text.where(text != "", None)


def statuses(matrix: pd.DataFrame) -> pd.Series:
    """Most severe status triggered by each row's reason codes (COMPLIANT when none)."""
    base = STATUS_SEVERITY[ComplianceStatus.COMPLIANT]
    severity = np.full(len(matrix), base, dtype="int64")
    for code, status in REASON_STATUS.items():
        if status is not None:
            raised = matrix[code.value].to_numpy()
            severity = np.maximum(severity, np.where(raised, STATUS_SEVERITY[status], base))
    by_level = {level: status.value for status, level in STATUS_SEVERITY.items()}
    return pd.Series(severity, index=matrix.index).map(by_level)


def build_decisions(
    shipments: pd.DataFrame,
    policies: pd.DataFrame,
    coverage: pd.DataFrame,
    excursion_summary: pd.DataFrame,
    matrix: pd.DataFrame,
) -> pd.DataFrame:
    """Build the ``compliance_decisions`` table.

    Args:
        shipments: The ``shipments`` table (``carrier_reported_status``).
        policies: Applied policy per shipment, indexed by ``shipment_id``.
        coverage: Coverage and gap metrics, indexed by ``shipment_id``.
        excursion_summary: Excursion totals and report IDs, indexed by ``shipment_id``.
        matrix: Reason matrix (:func:`reason_matrix`) without the carrier code.

    Returns:
        One decision per shipment with :data:`DECISION_COLUMNS`. The matrix gains the
        CARRIER_STATUS_CONTRADICTED column in place.
    """
    index = matrix.index
    status = statuses(matrix)
    action = status.map({s.value: a.value for s, a in STATUS_ACTION.items()})
    claim = shipments.set_index("shipment_id")["carrier_reported_status"].reindex(index)
    says_pass = claim == CarrierReportedStatus.PASS.value
    releases = action == DecidedAction.RELEASE.value
    matrix[ReasonCode.CARRIER_STATUS_CONTRADICTED.value] = (says_pass != releases).to_numpy()

    return pd.DataFrame(
        {
            "shipment_id": index.to_numpy(),
            "applied_policy_id": policies.loc[index, "applied_policy_id"].to_numpy(),
            "policy_source": policies.loc[index, "policy_source"].to_numpy(),
            "status": status.to_numpy(),
            "reason_codes": reason_codes_text(matrix).to_numpy(),
            "reading_count": coverage.loc[index, "reading_count"].to_numpy(),
            "expected_reading_count": coverage.loc[index, "expected_reading_count"].to_numpy(),
            "coverage_pct": coverage.loc[index, "coverage_pct"].to_numpy(),
            "max_gap_min": coverage.loc[index, "max_gap_min"].to_numpy(),
            "total_excursion_min": excursion_summary.loc[index, "total_excursion_min"].to_numpy(),
            "excursion_report_ids": excursion_summary.loc[index, "excursion_report_ids"].to_numpy(),
            "decided_action": action.to_numpy(),
        }
    )[list(DECISION_COLUMNS)]


def _round_robin(parties: pd.DataFrame, party_type: PartyType, count: int) -> np.ndarray:
    """Cycle through the parties of one type (sorted by ID) for ``count`` shipments."""
    pool = np.sort(parties.loc[parties["party_type"] == party_type.value, "party_id"].to_numpy())
    if count and not len(pool):
        raise ComplianceError(f"No {party_type.value} party to record compliance status events")
    return pool[np.arange(count) % len(pool)] if count else pool[:0]


def status_events(
    decisions: pd.DataFrame,
    windows: pd.DataFrame,
    raw_status_events: pd.DataFrame,
    parties: pd.DataFrame,
) -> pd.DataFrame:
    """Build the engine's appended lifecycle events (``compliance_status_events``).

    Every shipment gets UNDER_REVIEW (a logistics analyst); RELEASE and QUARANTINE decisions
    then get RELEASED / QUARANTINED (an operations manager). Sequence numbers continue the raw
    history. ``event_at`` is the end of the shipment's window (logger ``unassigned_at``, or
    ``actual_arrival_at`` without a logger), never the wall clock. Actors are assigned
    round-robin: the k-th shipment in ``shipment_id`` order gets analyst (and manager)
    ``k mod n`` of those sorted by ``party_id`` (DECISIONS.md #61).

    Args:
        decisions: The ``compliance_decisions`` table.
        windows: Per-shipment windows with ``window_end``.
        raw_status_events: The raw ``shipment_status_events`` table.
        parties: The ``parties`` table.

    Returns:
        The ``compliance_status_events`` table (:data:`EVENT_COLUMNS`).

    Raises:
        ComplianceError: If there are shipments but no LOGISTICS_ANALYST or OPERATIONS_MANAGER.
    """
    ordered = decisions.sort_values("shipment_id", kind="stable", ignore_index=True)
    ships = ordered["shipment_id"]
    last = raw_status_events.groupby("shipment_id")["sequence"].max()
    last = last.reindex(ships, fill_value=0).to_numpy(dtype="int64")
    event_at = windows.set_index("shipment_id").loc[ships, "window_end"].to_numpy()
    analysts = _round_robin(parties, PartyType.LOGISTICS_ANALYST, len(ships))
    managers = _round_robin(parties, PartyType.OPERATIONS_MANAGER, len(ships))

    review = pd.DataFrame(
        {
            "shipment_id": ships.to_numpy(),
            "sequence": last + 1,
            "status": ShipmentStatus.UNDER_REVIEW.value,
            "event_at": event_at,
            "actor_party_id": analysts,
        }
    )
    final_names = {action.value: status.value for action, status in FINAL_STATUS.items()}
    final_status = ordered["decided_action"].map(final_names)
    closes = final_status.notna().to_numpy()
    final = pd.DataFrame(
        {
            "shipment_id": ships.to_numpy()[closes],
            "sequence": last[closes] + 2,
            "status": final_status.to_numpy()[closes],
            "event_at": event_at[closes],
            "actor_party_id": managers[closes],
        }
    )
    events = pd.concat([review, final], ignore_index=True)
    events["sequence"] = events["sequence"].astype("int64")
    events["status_event_id"] = ids.status_event_ids(events["shipment_id"], events["sequence"])
    return events[list(EVENT_COLUMNS)]
