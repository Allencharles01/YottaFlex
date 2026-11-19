"""Policy resolution (compliance_rules.md §1.1)."""

import pandas as pd
import pytest

from coldchain.compliance.policy import resolve_policies
from coldchain.enums import TableName
from coldchain.errors import ComplianceError
from conftest import SHP, _ts


def _resolve(raw) -> pd.DataFrame:
    return resolve_policies(
        raw[TableName.SHIPMENTS],
        raw[TableName.SHIPMENT_LOTS],
        raw[TableName.LOTS],
        raw[TableName.PRODUCTS],
        raw[TableName.THRESHOLD_POLICIES],
    )


def _depart(raw, when: str, declared: str = "THR-REFRIG_2_8-V1") -> None:
    shipments = raw[TableName.SHIPMENTS]
    shipments["actual_departure_at"] = _ts(when)
    shipments["declared_policy_id"] = declared


@pytest.mark.parametrize(
    ("departure", "applied"),
    [
        ("2026-06-30 23:50", "THR-REFRIG_2_8-V1"),
        ("2026-06-30 23:59", "THR-REFRIG_2_8-V1"),
        ("2026-07-01 00:00", "THR-REFRIG_2_8-V2"),  # half-open window: V2 starts at midnight
        ("2026-07-01 00:10", "THR-REFRIG_2_8-V2"),
    ],
)
def test_version_follows_actual_departure_around_v2_boundary(raw_one, departure, applied) -> None:
    _depart(raw_one, departure)
    row = _resolve(raw_one).iloc[0]
    assert row["applied_policy_id"] == applied
    assert row["max_single_excursion_min"] == (30 if applied.endswith("V1") else 20)


def test_matching_declaration_is_confirmed(raw_one) -> None:
    row = _resolve(raw_one).iloc[0]
    assert row["applied_policy_id"] == "THR-REFRIG_2_8-V1"
    assert row["policy_source"] == "DECLARED_CONFIRMED"
    assert not row["POLICY_MISMATCH_DECLARED"]


def test_version_only_mismatch_is_flagged(raw_one) -> None:
    """Declared V1 (at planning) but departed after the V2 boundary (DECISIONS.md #26)."""
    _depart(raw_one, "2026-07-01 01:00", declared="THR-REFRIG_2_8-V1")
    row = _resolve(raw_one).iloc[0]
    assert row["applied_policy_id"] == "THR-REFRIG_2_8-V2"
    assert row["policy_source"] == "PRODUCT_OVERRIDE"
    assert row["POLICY_MISMATCH_DECLARED"]


def test_declared_policy_is_never_trusted(raw_one) -> None:
    """A wrong temperature class in the declaration does not change the applied policy."""
    policies = raw_one[TableName.THRESHOLD_POLICIES]
    ambient = policies.iloc[[0]].assign(
        policy_id="THR-AMBIENT_15_25-V1",
        policy_code="AMBIENT_15_25",
        min_c=15.0,
        max_c=25.0,
        abs_min_c=10.0,
        abs_max_c=30.0,
    )
    raw_one[TableName.THRESHOLD_POLICIES] = pd.concat([policies, ambient], ignore_index=True)
    raw_one[TableName.SHIPMENTS]["declared_policy_id"] = "THR-AMBIENT_15_25-V1"
    row = _resolve(raw_one).iloc[0]
    assert row["applied_policy_id"] == "THR-REFRIG_2_8-V1"
    assert (row["min_c"], row["max_c"]) == (2.0, 8.0)
    assert row["POLICY_MISMATCH_DECLARED"]


def test_limits_are_carried(raw_one) -> None:
    row = _resolve(raw_one).iloc[0]
    assert row["max_reading_gap_min"] == 30
    assert row["min_coverage_pct"] == 95.0
    assert (row["abs_min_c"], row["abs_max_c"]) == (0.0, 12.0)


def test_mixed_policy_codes_raise(raw_one) -> None:
    products = raw_one[TableName.PRODUCTS]
    frozen = products.assign(product_id="PRD-0002", name="Frozen X", policy_code="FROZEN")
    raw_one[TableName.PRODUCTS] = pd.concat([products, frozen], ignore_index=True)
    lots = raw_one[TableName.LOTS]
    raw_one[TableName.LOTS] = pd.concat(
        [lots, lots.assign(lot_id="LOT-2026-000002", product_id="PRD-0002")], ignore_index=True
    )
    sl = raw_one[TableName.SHIPMENT_LOTS]
    raw_one[TableName.SHIPMENT_LOTS] = pd.concat(
        [sl, sl.assign(lot_id="LOT-2026-000002")], ignore_index=True
    )
    with pytest.raises(ComplianceError, match="mix policy codes"):
        _resolve(raw_one)


def test_shipment_without_lots_raises(raw_one) -> None:
    raw_one[TableName.SHIPMENT_LOTS] = raw_one[TableName.SHIPMENT_LOTS].iloc[0:0]
    with pytest.raises(ComplianceError, match=f"without lots.*{SHP}"):
        _resolve(raw_one)


def test_unknown_lot_raises(raw_one) -> None:
    raw_one[TableName.SHIPMENT_LOTS]["lot_id"] = "LOT-2026-000099"
    with pytest.raises(ComplianceError, match="known lot"):
        _resolve(raw_one)


def test_no_effective_version_raises(raw_one) -> None:
    _depart(raw_one, "2025-12-31 23:59")
    with pytest.raises(ComplianceError, match="No policy version effective"):
        _resolve(raw_one)


def test_empty_shipments(raw_one) -> None:
    for name in (TableName.SHIPMENTS, TableName.SHIPMENT_LOTS):
        raw_one[name] = raw_one[name].iloc[0:0]
    assert _resolve(raw_one).empty
