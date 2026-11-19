"""Master-data generators: parties, threshold policies, products, packaging, lots and devices.

Every function is pure (configuration + random generator in, DataFrame out) and returns a frame
in its table's schema column order and dtypes. Each table draws from its own named RNG stream
(see :data:`STAGES`), so changing one generator never changes another's random numbers.
"""

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.config import AppConfig, GenerationConfig, ReferenceData
from coldchain.enums import LotStatus, PartyType, TableName
from coldchain.errors import GenerationError

STAGES: dict[TableName, str] = {
    TableName.PARTIES: "master.parties",
    TableName.PRODUCTS: "master.products",
    TableName.LOTS: "master.lots",
    TableName.DEVICES: "master.devices",
}
"""RNG stream name of every master table that uses randomness."""


def _id_column(builder, count: int) -> pd.Series:
    """Build ``count`` sequential IDs (1..count) with a scalar ID builder."""
    return pd.Series([builder(n) for n in range(1, count + 1)], dtype="str")


def _days_before(anchor: date, days: np.ndarray) -> pd.Series:
    """Dates ``anchor - days`` as an object column of ``datetime.date``."""
    return pd.Series([anchor - timedelta(days=int(d)) for d in days], dtype=object)


def _uniform_int(rng: np.random.Generator, low: int, high: int, size: int) -> np.ndarray:
    """Uniform integers in the inclusive range ``[low, high]``."""
    return rng.integers(low, high + 1, size=size)


# --------------------------------------------------------------------------------------------
# Parties
# --------------------------------------------------------------------------------------------


def _party_cities(
    party_type: PartyType, count: int, reference: ReferenceData, rng: np.random.Generator
) -> np.ndarray:
    """Pick a city per party: suppliers in manufacturing clusters, warehouses in hubs.

    Cities are drawn without replacement while there are enough of them, so parties of one
    type spread over as many cities as possible.
    """
    if party_type is PartyType.SUPPLIER:
        pool = [loc.city for loc in reference.locations if loc.is_manufacturing_cluster]
    elif party_type is PartyType.WAREHOUSE:
        pool = [loc.city for loc in reference.locations if loc.is_hub]
    else:
        pool = [loc.city for loc in reference.locations]
    return rng.choice(np.array(pool), size=count, replace=count > len(pool))


def generate_parties(
    generation: GenerationConfig,
    reference: ReferenceData,
    window_start: date,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate the parties: every actor of the brief, as data.

    IDs are numbered in :class:`PartyType` declaration order. Names are
    ``"<city> <label> <nn>"`` with a per-type counter, so they are unique.

    Args:
        generation: Generation settings (counts, name labels, activity lead time).
        reference: Reference data (locations).
        window_start: First day of the shipment date window; every party is active before it.
        rng: The ``master.parties`` stream.

    Returns:
        The ``parties`` table.
    """
    frames = []
    for party_type in PartyType:
        count = generation.parties[party_type]
        cities = _party_cities(party_type, count, reference, rng)
        label = generation.party_name_labels[party_type]
        frames.append(
            pd.DataFrame(
                {
                    "party_type": party_type.value,
                    "name": [f"{city} {label} {k:02d}" for k, city in enumerate(cities, 1)],
                    "city": cities,
                }
            )
        )
    parties = pd.concat(frames, ignore_index=True)
    lead = generation.party_active_lead_days
    return pd.DataFrame(
        {
            "party_id": _id_column(ids.party_id, len(parties)),
            "party_type": parties["party_type"].astype("str"),
            "name": parties["name"].astype("str"),
            "city": parties["city"].astype("str"),
            "active_from": _days_before(
                window_start, _uniform_int(rng, lead.min, lead.max, len(parties))
            ),
        }
    )


# --------------------------------------------------------------------------------------------
# Reference-driven tables
# --------------------------------------------------------------------------------------------


def generate_threshold_policies(reference: ReferenceData) -> pd.DataFrame:
    """Build the ``threshold_policies`` table from the reference policies (no randomness).

    Args:
        reference: Reference data.

    Returns:
        The ``threshold_policies`` table.
    """
    rows = [
        {"policy_id": ids.policy_id(p.policy_code.value, p.version), **p.model_dump(mode="python")}
        for p in reference.policies
    ]
    frame = pd.DataFrame(rows)
    frame["policy_code"] = frame["policy_code"].map(str).astype("str")
    frame["policy_id"] = frame["policy_id"].astype("str")
    frame["effective_to"] = (
        frame["effective_to"].astype(object).where(frame["effective_to"].notna(), None)
    )
    for column in ("min_c", "max_c", "abs_min_c", "abs_max_c", "min_coverage_pct"):
        frame[column] = frame[column].astype("float64").round(2)
    columns = [
        "policy_id",
        "policy_code",
        "version",
        "min_c",
        "max_c",
        "abs_min_c",
        "abs_max_c",
        "max_single_excursion_min",
        "max_cumulative_excursion_min",
        "max_reading_gap_min",
        "min_coverage_pct",
        "effective_from",
        "effective_to",
    ]
    return frame[columns]


def generate_products(
    reference: ReferenceData, parties: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Build the ``products`` table and assign each product to a supplier.

    Products keep the reference catalogue order (``PRD-0001`` is the first entry). Suppliers
    are dealt round-robin over a shuffled product order, so every supplier gets products when
    there are at least as many products as suppliers.

    Args:
        reference: Reference data (product catalogue).
        parties: The ``parties`` table.
        rng: The ``master.products`` stream.

    Returns:
        The ``products`` table.

    Raises:
        GenerationError: If there are no SUPPLIER parties.
    """
    suppliers = parties.loc[parties["party_type"] == PartyType.SUPPLIER, "party_id"].to_numpy()
    if len(suppliers) == 0:
        raise GenerationError("Cannot assign products: there are no SUPPLIER parties")
    count = len(reference.products)
    order = rng.permutation(count)
    dealt = rng.permutation(suppliers)
    supplier_of = np.empty(count, dtype=object)
    supplier_of[order] = dealt[np.arange(count) % len(dealt)]
    return pd.DataFrame(
        {
            "product_id": _id_column(ids.product_id, count),
            "name": pd.Series([p.name for p in reference.products], dtype="str"),
            "category": pd.Series([p.category.value for p in reference.products], dtype="str"),
            "supplier_party_id": pd.Series(supplier_of, dtype="str"),
            "policy_code": pd.Series(
                [p.policy_code.value for p in reference.products], dtype="str"
            ),
            "shelf_life_days": np.array([p.shelf_life_days for p in reference.products]),
        }
    )


def generate_packaging(reference: ReferenceData) -> pd.DataFrame:
    """Build the ``packaging`` table from the reference profiles (no randomness).

    Args:
        reference: Reference data (packaging profiles).

    Returns:
        The ``packaging`` table.
    """
    profiles = reference.packaging
    return pd.DataFrame(
        {
            "packaging_id": _id_column(ids.packaging_id, len(profiles)),
            "name": pd.Series([p.name for p in profiles], dtype="str"),
            "packaging_type": pd.Series([p.packaging_type.value for p in profiles], dtype="str"),
            "supported_policy_code": pd.Series(
                [p.supported_policy_code.value for p in profiles], dtype="str"
            ),
            "setpoint_c": np.round([p.setpoint_c for p in profiles], 2),
            "max_hold_hours": np.round([p.max_hold_hours for p in profiles], 2),
            "thermal_response": np.round([p.thermal_response for p in profiles], 2),
        }
    )


# --------------------------------------------------------------------------------------------
# Lots
# --------------------------------------------------------------------------------------------


def lots_per_product(config: AppConfig, shipments: int, products: int) -> int:
    """Number of lots generated per product for a run of ``shipments`` shipments.

    ``max(min_per_product, ceil(expected shipment-lot pairs / products / shipments_per_lot))``.

    Args:
        config: Validated configuration.
        shipments: Number of shipments in the run.
        products: Number of products.

    Returns:
        Lots per product (at least ``generation.lots.min_per_product``).
    """
    settings = config.generation
    mean_lots = (settings.lots_per_shipment.min + settings.lots_per_shipment.max) / 2
    needed = math.ceil(shipments * mean_lots / products / settings.lots.shipments_per_lot)
    return max(settings.lots.min_per_product, needed)


def generate_lots(
    config: AppConfig,
    reference: ReferenceData,
    products: pd.DataFrame,
    shipments: int,
    rng: np.random.Generator,
) -> pd.DataFrame:
    """Generate production lots for every product.

    Each product gets :func:`lots_per_product` lots whose manufacture dates are *stratified*
    over ``[date_window.start - manufacture_lead_days, date_window.end]``: lot *k* falls in the
    *k*-th equal slice. That keeps a recently made lot available for every departure date.
    The first lot of each product is always RELEASED, so even the earliest shipments have
    stock. IDs are numbered in order of (manufacture date, product).

    Args:
        config: Validated configuration.
        reference: Reference data (lot sizes per product template).
        products: The ``products`` table (same order as the reference catalogue).
        shipments: Number of shipments in the run (drives the lot count).
        rng: The ``master.lots`` stream.

    Returns:
        The ``lots`` table.

    Raises:
        GenerationError: If more lots are needed than the lot-ID width allows.
    """
    settings = config.generation.lots
    per_product = lots_per_product(config, shipments, len(products))
    total = per_product * len(products)
    if total >= 10**6:
        raise GenerationError(f"{total} lots requested; lot IDs allow at most 999999")

    first_day = config.date_window.start - timedelta(days=settings.manufacture_lead_days)
    span_days = (config.date_window.end - first_day).days + 1
    product_index = np.repeat(np.arange(len(products)), per_product)
    slot = np.tile(np.arange(per_product), len(products))
    offset_days = np.floor((slot + rng.random(total)) * span_days / per_product).astype(int)
    manufactured = pd.Series(
        [first_day + timedelta(days=int(d)) for d in offset_days], dtype=object
    )
    shelf = products["shelf_life_days"].to_numpy()[product_index]
    expires = pd.Series(
        [m + timedelta(days=int(s)) for m, s in zip(manufactured, shelf, strict=True)],
        dtype=object,
    )
    low = np.array([reference.products[i].lot_size_units.min for i in product_index])
    high = np.array([reference.products[i].lot_size_units.max for i in product_index])
    quantity = rng.integers(low, high + 1)
    on_hold = (rng.random(total) < settings.on_hold_rate) & (slot > 0)
    lots = pd.DataFrame(
        {
            "product_id": products["product_id"].to_numpy()[product_index],
            "manufactured_on": manufactured,
            "expires_on": expires,
            "quantity_units": quantity.astype("int64"),
            "lot_status": np.where(on_hold, LotStatus.ON_HOLD.value, LotStatus.RELEASED.value),
        }
    )
    lots = lots.sort_values(["manufactured_on", "product_id"], kind="stable").reset_index(drop=True)
    lots.insert(0, "lot_id", _id_column(ids.lot_id, len(lots)))
    for column in ("product_id", "lot_status"):
        lots[column] = lots[column].astype("str")
    return lots


# --------------------------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------------------------


def generate_devices(config: AppConfig, count: int, rng: np.random.Generator) -> pd.DataFrame:
    """Generate the physical logger devices.

    Each device has one calibration record: ``calibrated_on`` falls before the date window and
    ``calibration_due_on = calibrated_on + calibration_validity_days``, so some devices expire
    during the window and can no longer be assigned afterwards.

    Args:
        config: Validated configuration.
        count: Number of devices (from the scale preset).
        rng: The ``master.devices`` stream.

    Returns:
        The ``devices`` table.

    Raises:
        GenerationError: If ``count`` does not fit the device-ID width.
    """
    if not 1 <= count < 10**6:
        raise GenerationError(f"Device count must be within 1..999999, got {count}")
    settings = config.generation.devices
    models = rng.choice(np.array(settings.models), size=count)
    serials = rng.choice(10**7, size=count, replace=False)
    before = settings.calibrated_before_window_days
    calibrated = _days_before(
        config.date_window.start, _uniform_int(rng, before.min, before.max, count)
    )
    validity = timedelta(days=settings.calibration_validity_days)
    uncertainty = rng.uniform(settings.uncertainty_c.min, settings.uncertainty_c.max, count)
    return pd.DataFrame(
        {
            "device_id": _id_column(ids.device_id, count),
            "model": pd.Series(models, dtype="str"),
            "serial_number": pd.Series(
                [f"{m}-{s:07d}" for m, s in zip(models, serials, strict=True)], dtype="str"
            ),
            "calibrated_on": calibrated,
            "calibration_due_on": pd.Series([c + validity for c in calibrated], dtype=object),
            "uncertainty_c": np.round(uncertainty, 2),
            "reading_interval_min": np.full(count, config.reading_interval_min, dtype="int64"),
        }
    )
