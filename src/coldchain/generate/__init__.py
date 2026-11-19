"""Synthetic data generation: master data, shipments, natural scenarios, readings and events.

:func:`generate_tables` runs the whole generator in dependency order (DECISIONS.md #38):

1. master data: parties → threshold policies → products → packaging → lots → devices;
2. shipment plan, then the scenario stage *before* any shipment table exists: policy
   transitions, scenario assignment and the per-shipment flags;
3. shipments → shipment_lots → route_stops → loggers, built with those flags;
4. the applied policy and the designed segments of every shaped scenario;
5. the expected outcomes (ground truth), derived from the documented rules; the carrier's
   claim follows from the expected action (RELEASE → PASS, otherwise FAIL);
6. the hidden device offsets (``device_truth``, ground truth only), then temperature
   readings (vectorised), custody events and status events.

No table is modified after it is assembled.
"""

import logging

import numpy as np
import pandas as pd

from coldchain.config import AppConfig, ReferenceData
from coldchain.enums import CarrierReportedStatus, DecidedAction, Scale, TableName
from coldchain.generate import events, expected, master, readings, scenarios, shipments
from coldchain.io import coerce_to_schema, sort_by_primary_key
from coldchain.rng import stage_rng
from coldchain.schemas import validate

logger = logging.getLogger(__name__)


def generate_tables(
    config: AppConfig, reference: ReferenceData, scale: Scale | str
) -> dict[TableName, pd.DataFrame]:
    """Generate every raw table and the expected outcomes for one scale (pure: no file I/O).

    Each table is coerced to its schema dtypes, sorted by its primary key and validated.

    Args:
        config: Validated configuration (its ``seed`` drives every RNG stream).
        reference: Validated reference data.
        scale: Scale preset name.

    Returns:
        The thirteen raw tables in dependency order, then the ground-truth tables
        ``expected_outcomes`` and ``device_truth``, keyed by :class:`TableName`.

    Raises:
        ConfigError: If the scale is unknown.
        GenerationError: If an invariant cannot be satisfied.
        SchemaValidationError: If a generated table fails its schema.
    """
    volumes = config.scale(scale)

    def rng(stage: str) -> np.random.Generator:
        return stage_rng(config.seed, stage)

    def streams(stages: dict[str, str]) -> dict[str, np.random.Generator]:
        return {key: rng(stage) for key, stage in stages.items()}

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

    # Scenarios are assigned on the plan, before any shipment table is built.
    plan = shipments.plan_shipments(
        config, volumes.shipments, parties, products, rng(shipments.STAGES["plan"])
    )
    plan, min_delay = scenarios.force_policy_transitions(
        config,
        plan,
        tables[TableName.THRESHOLD_POLICIES],
        products,
        rng(scenarios.STAGES["transitions"]),
    )
    assignment = scenarios.assign_scenarios(
        config, plan, tables[TableName.DEVICES], rng(scenarios.STAGES["assign"])
    )
    tables.update(
        shipments.generate_shipment_tables(
            config,
            reference,
            tables,
            plan,
            scenarios.shipment_flags(
                config, assignment, plan, tables[TableName.THRESHOLD_POLICIES], min_delay
            ),
            streams(shipments.STAGES),
        )
    )

    applied = expected.applied_policies(
        tables[TableName.SHIPMENTS],
        tables[TableName.SHIPMENT_LOTS],
        tables[TableName.LOTS],
        products,
        tables[TableName.THRESHOLD_POLICIES],
    )
    segments = scenarios.design_segments(
        config,
        assignment,
        tables[TableName.SHIPMENTS],
        tables[TableName.LOGGERS],
        applied,
        rng(scenarios.STAGES["design"]),
    )
    outcomes = expected.derive_expected_outcomes(
        config,
        assignment,
        applied,
        tables[TableName.LOGGERS],
        tables[TableName.DEVICES],
        tables[TableName.ROUTE_STOPS],
        segments,
    )
    release = outcomes.set_index("shipment_id")["expected_action"] == DecidedAction.RELEASE
    claim = np.where(
        release.loc[tables[TableName.SHIPMENTS]["shipment_id"]].to_numpy(),
        CarrierReportedStatus.PASS.value,
        CarrierReportedStatus.FAIL.value,
    )
    tables[TableName.SHIPMENTS] = tables[TableName.SHIPMENTS].assign(carrier_reported_status=claim)

    reading_rngs = streams(readings.STAGES)
    tables[TableName.DEVICE_TRUTH] = readings.device_truth(
        tables[TableName.DEVICES], reading_rngs["offsets"]
    )
    tables[TableName.TEMPERATURE_READINGS] = readings.generate_readings(
        config, tables, applied, segments, reading_rngs
    )
    tables[TableName.CUSTODY_EVENTS] = events.custody_events(
        tables[TableName.SHIPMENTS], tables[TableName.ROUTE_STOPS]
    )
    tables[TableName.SHIPMENT_STATUS_EVENTS] = events.status_events(
        config,
        tables[TableName.SHIPMENTS],
        tables[TableName.ROUTE_STOPS],
        rng(events.STAGES["status"]),
    )
    tables[TableName.EXPECTED_OUTCOMES] = outcomes

    result = {}
    for name in TableName:
        if name not in tables:
            continue
        typed = sort_by_primary_key(name, coerce_to_schema(name, tables[name]))
        result[name] = validate(name, typed)
        logger.debug("Generated %s: %d rows", name.value, len(typed))
    return result
