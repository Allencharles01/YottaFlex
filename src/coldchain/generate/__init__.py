"""Synthetic data generation.

Phase 2 provides the master stage: parties → threshold policies → products → packaging → lots →
devices → shipments → shipment_lots → route_stops → loggers.
"""

import logging

import pandas as pd

from coldchain.config import AppConfig, ReferenceData
from coldchain.enums import Scale, TableName
from coldchain.generate import master, shipments
from coldchain.io import coerce_to_schema, sort_by_primary_key
from coldchain.rng import stage_rng
from coldchain.schemas import validate

logger = logging.getLogger(__name__)


def generate_master_tables(
    config: AppConfig, reference: ReferenceData, scale: Scale | str
) -> dict[TableName, pd.DataFrame]:
    """Generate every master and shipment table for one scale (pure: no file I/O).

    Each table is coerced to its schema dtypes, sorted by its primary key and validated.

    Args:
        config: Validated configuration (its ``seed`` drives every RNG stream).
        reference: Validated reference data.
        scale: Scale preset name.

    Returns:
        The ten tables, keyed by :class:`TableName`, in dependency order.

    Raises:
        ConfigError: If the scale is unknown.
        GenerationError: If an invariant cannot be satisfied.
        SchemaValidationError: If a generated table fails its schema.
    """
    volumes = config.scale(scale)

    def rng(stage: str):
        return stage_rng(config.seed, stage)

    parties = master.generate_parties(
        config.generation,
        reference,
        config.date_window.start,
        rng(master.STAGES[TableName.PARTIES]),
    )
    products = master.generate_products(reference, parties, rng(master.STAGES[TableName.PRODUCTS]))
    tables: dict[TableName, pd.DataFrame] = {
        TableName.PARTIES: parties,
        TableName.THRESHOLD_POLICIES: master.generate_threshold_policies(reference),
        TableName.PRODUCTS: products,
        TableName.PACKAGING: master.generate_packaging(reference),
        TableName.LOTS: master.generate_lots(
            config, reference, products, volumes.shipments, rng(master.STAGES[TableName.LOTS])
        ),
        TableName.DEVICES: master.generate_devices(
            config, volumes.devices, rng(master.STAGES[TableName.DEVICES])
        ),
    }
    streams = {key: rng(stage) for key, stage in shipments.STAGES.items()}
    tables.update(
        shipments.generate_shipment_tables(config, reference, tables, volumes.shipments, streams)
    )

    result = {}
    for name, frame in tables.items():
        typed = sort_by_primary_key(name, coerce_to_schema(name, frame))
        result[name] = validate(name, typed)
        logger.debug("Generated %s: %d rows", name.value, len(typed))
    return result
