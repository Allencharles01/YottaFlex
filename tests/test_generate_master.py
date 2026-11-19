from datetime import timedelta

import numpy as np
import pandas as pd
import pytest

from coldchain.enums import LotStatus, PartyType, TableName
from coldchain.errors import GenerationError
from coldchain.generate import master
from coldchain.rng import stage_rng


def test_party_counts_follow_config(starter_tables, app_config) -> None:
    counts = starter_tables[TableName.PARTIES]["party_type"].value_counts()
    for party_type, expected in app_config.generation.parties.items():
        assert counts[party_type.value] == expected


def test_party_cities_follow_reference_roles(starter_tables, reference) -> None:
    parties = starter_tables[TableName.PARTIES]
    hubs = {loc.city for loc in reference.locations if loc.is_hub}
    clusters = {loc.city for loc in reference.locations if loc.is_manufacturing_cluster}
    cities = {loc.city for loc in reference.locations}
    assert set(parties["city"]) <= cities
    assert set(parties.loc[parties["party_type"] == "WAREHOUSE", "city"]) <= hubs
    assert set(parties.loc[parties["party_type"] == "SUPPLIER", "city"]) <= clusters


def test_parties_are_active_before_the_window(starter_tables, app_config) -> None:
    active = starter_tables[TableName.PARTIES]["active_from"]
    lead = app_config.generation.party_active_lead_days
    start = app_config.date_window.start
    assert all(
        start - timedelta(days=lead.max) <= d <= start - timedelta(days=lead.min) for d in active
    )


def test_party_names_unique_and_labelled(starter_tables, app_config) -> None:
    parties = starter_tables[TableName.PARTIES]
    assert parties["name"].is_unique
    for party_type, label in app_config.generation.party_name_labels.items():
        names = parties.loc[parties["party_type"] == party_type.value, "name"]
        assert names.str.contains(f" {label} ", regex=False).all()


def test_policies_match_reference(starter_tables, reference) -> None:
    policies = starter_tables[TableName.THRESHOLD_POLICIES]
    assert len(policies) == len(reference.policies)
    refrig = policies[policies["policy_code"] == "REFRIG_2_8"].set_index("version")
    assert refrig.loc[1, "max_single_excursion_min"] == 30
    assert refrig.loc[2, "max_single_excursion_min"] == 20
    assert refrig.loc[1, "effective_to"] == refrig.loc[2, "effective_from"]
    assert refrig.loc[2, "effective_to"] is None


def test_products_follow_catalogue_and_have_supplier_parties(starter_tables, reference) -> None:
    products = starter_tables[TableName.PRODUCTS]
    parties = starter_tables[TableName.PARTIES].set_index("party_id")
    assert products["name"].tolist() == [p.name for p in reference.products]
    assert (parties.loc[products["supplier_party_id"], "party_type"] == "SUPPLIER").all()
    suppliers = parties.index[parties["party_type"] == "SUPPLIER"]
    assert set(products["supplier_party_id"]) == set(suppliers)  # every supplier has products


def test_products_need_a_supplier(reference) -> None:
    parties = pd.DataFrame({"party_id": ["PTY-0001"], "party_type": ["CARRIER"]})
    with pytest.raises(GenerationError, match="no SUPPLIER"):
        master.generate_products(reference, parties, stage_rng(1, "x"))


def test_packaging_matches_reference(starter_tables, reference) -> None:
    packaging = starter_tables[TableName.PACKAGING]
    assert packaging["name"].tolist() == [p.name for p in reference.packaging]
    assert packaging["packaging_id"].iloc[0] == "PKG-0001"


def test_lot_count_formula(app_config) -> None:
    # starter: 50 shipments x 2 mean lots / 17 products / 3 = 1.96 -> min 6
    assert master.lots_per_product(app_config, 50, 17) == 6
    # full: ceil(5000 x 2 / 17 / 3) = 197
    assert master.lots_per_product(app_config, 5000, 17) == 197


def test_lots_are_stratified_released_first_and_expire_by_shelf_life(
    starter_tables, app_config
) -> None:
    lots = starter_tables[TableName.LOTS]
    products = starter_tables[TableName.PRODUCTS].set_index("product_id")
    per_product = master.lots_per_product(app_config, 50, len(products))
    assert lots.groupby("product_id").size().eq(per_product).all()
    shelf = products.loc[lots["product_id"], "shelf_life_days"].to_numpy()
    expected = [
        m + timedelta(days=int(s)) for m, s in zip(lots["manufactured_on"], shelf, strict=True)
    ]
    assert lots["expires_on"].tolist() == expected
    first_day = app_config.date_window.start - timedelta(
        days=app_config.generation.lots.manufacture_lead_days
    )
    assert lots["manufactured_on"].min() >= first_day
    assert lots["manufactured_on"].max() <= app_config.date_window.end
    earliest = lots.sort_values("manufactured_on").groupby("product_id").head(1)
    assert (earliest["lot_status"] == LotStatus.RELEASED).all()


def test_lot_ids_follow_manufacture_order(starter_tables) -> None:
    lots = starter_tables[TableName.LOTS]
    assert lots["manufactured_on"].is_monotonic_increasing


def test_too_many_lots_rejected(app_config, reference, starter_tables) -> None:
    with pytest.raises(GenerationError, match="lot IDs"):
        master.generate_lots(
            app_config, reference, starter_tables[TableName.PRODUCTS], 10**7, stage_rng(1, "x")
        )


def test_devices_calibration_and_uncertainty(starter_tables, app_config) -> None:
    devices = starter_tables[TableName.DEVICES]
    settings = app_config.generation.devices
    assert len(devices) == app_config.scale("starter").devices
    assert devices["serial_number"].is_unique
    validity = timedelta(days=settings.calibration_validity_days)
    assert devices["calibration_due_on"].tolist() == [
        c + validity for c in devices["calibrated_on"]
    ]
    assert (devices["calibrated_on"] <= app_config.date_window.start).all()
    assert (
        devices["uncertainty_c"]
        .between(settings.uncertainty_c.min, settings.uncertainty_c.max)
        .all()
    )
    assert devices["model"].isin(settings.models).all()
    assert (devices["reading_interval_min"] == app_config.reading_interval_min).all()


@pytest.mark.parametrize("count", [0, 10**6])
def test_device_count_bounds(app_config, count: int) -> None:
    with pytest.raises(GenerationError, match="Device count"):
        master.generate_devices(app_config, count, stage_rng(1, "x"))


def test_party_generation_is_deterministic(app_config, reference) -> None:
    def build(seed: int) -> pd.DataFrame:
        return master.generate_parties(
            app_config.generation,
            reference,
            app_config.date_window.start,
            stage_rng(seed, master.STAGES[TableName.PARTIES]),
        )

    pd.testing.assert_frame_equal(build(5), build(5))
    assert not build(5).equals(build(6))


def test_party_types_in_declaration_order(starter_tables) -> None:
    order = {t.value: i for i, t in enumerate(PartyType)}
    ranks = starter_tables[TableName.PARTIES]["party_type"].map(order)
    assert np.all(np.diff(ranks.to_numpy()) >= 0)
