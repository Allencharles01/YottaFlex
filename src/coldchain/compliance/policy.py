"""Applied-policy resolution (compliance_rules.md §1.1).

The applied policy comes from the *products* in a shipment (``shipment_lots → lots →
products → policy_code``) and the version effective at ``actual_departure_at``. The planner's
``declared_policy_id`` is only compared against it, never trusted on its own (DECISIONS.md #26).
The half-open effective-window rule lives in one place,
:func:`coldchain.schemas.effective_policy_ids` (DECISIONS.md #35), which the generator also uses.
"""

import pandas as pd

from coldchain.enums import PolicySource, ReasonCode
from coldchain.errors import ComplianceError
from coldchain.schemas import effective_policy_ids

POLICY_COLUMNS = (
    "min_c",
    "max_c",
    "abs_min_c",
    "abs_max_c",
    "max_single_excursion_min",
    "max_cumulative_excursion_min",
    "max_reading_gap_min",
    "min_coverage_pct",
)
"""Policy limits carried next to the applied policy of every shipment."""


def resolve_policies(
    shipments: pd.DataFrame,
    shipment_lots: pd.DataFrame,
    lots: pd.DataFrame,
    products: pd.DataFrame,
    policies: pd.DataFrame,
) -> pd.DataFrame:
    """Resolve the applied policy of every shipment and compare it with the declared one.

    Args:
        shipments: The ``shipments`` table.
        shipment_lots: The ``shipment_lots`` table.
        lots: The ``lots`` table.
        products: The ``products`` table.
        policies: The ``threshold_policies`` table.

    Returns:
        One row per shipment, in ``shipments`` order: ``shipment_id``, ``policy_code``,
        ``applied_policy_id``, ``policy_source``, a boolean ``POLICY_MISMATCH_DECLARED`` column
        and the limits in :data:`POLICY_COLUMNS`.

    Raises:
        ComplianceError: If a shipment has no lots, mixes policy codes (DECISIONS.md #21), or no
            version of its code is effective at its actual departure.
    """
    codes = shipment_lots[["shipment_id", "lot_id"]].merge(
        lots[["lot_id", "product_id"]], on="lot_id", how="left"
    )
    codes = codes.merge(products[["product_id", "policy_code"]], on="product_id", how="left")
    if codes["policy_code"].isna().any():
        broken = sorted(codes.loc[codes["policy_code"].isna(), "shipment_id"].unique())
        raise ComplianceError(f"Shipment lots without a known lot/product: {broken}")
    per_shipment = codes.groupby("shipment_id")["policy_code"].agg(["nunique", "first"])
    mixed = per_shipment.index[per_shipment["nunique"] > 1].tolist()
    if mixed:
        raise ComplianceError(f"Shipments mix policy codes (one container = one class): {mixed}")

    frame = shipments[["shipment_id", "declared_policy_id", "actual_departure_at"]].copy()
    frame["policy_code"] = per_shipment["first"].reindex(frame["shipment_id"]).to_numpy()
    without_lots = frame.loc[frame["policy_code"].isna(), "shipment_id"].tolist()
    if without_lots:
        raise ComplianceError(f"Shipments without lots: {without_lots}")
    frame["applied_policy_id"] = effective_policy_ids(
        policies, frame["policy_code"], frame["actual_departure_at"]
    ).to_numpy()
    unresolved = frame.loc[frame["applied_policy_id"].isna(), "shipment_id"].tolist()
    if unresolved:
        raise ComplianceError(f"No policy version effective at actual departure: {unresolved}")

    mismatch = frame["declared_policy_id"] != frame["applied_policy_id"]
    frame[ReasonCode.POLICY_MISMATCH_DECLARED.value] = mismatch.astype(bool)
    frame["policy_source"] = mismatch.map(
        {True: PolicySource.PRODUCT_OVERRIDE.value, False: PolicySource.DECLARED_CONFIRMED.value}
    ).astype(object)
    limits = policies[["policy_id", *POLICY_COLUMNS]].rename(
        columns={"policy_id": "applied_policy_id"}
    )
    frame = frame.merge(limits, on="applied_policy_id", how="left", validate="many_to_one")
    return frame[
        [
            "shipment_id",
            "policy_code",
            "applied_policy_id",
            "policy_source",
            ReasonCode.POLICY_MISMATCH_DECLARED.value,
            *POLICY_COLUMNS,
        ]
    ].reset_index(drop=True)
