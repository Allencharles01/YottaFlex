"""Edge-case injection: the five deliberate anomalies and their ground truth (Phase 5).

:func:`inject_tables` runs after generation on the clean baseline (DECISIONS.md #67):

1. resolve every shipment's applied policy (the generator's rule, §1.1);
2. choose disjoint targets among the NORMAL, COMPLIANT shipments with the seeded
   ``inject.targets`` stream (:func:`coldchain.inject.anomalies.select_targets`);
3. run the five injectors in :class:`~coldchain.enums.IssueType` order, each on its own
   ``inject.<issue_type>`` stream; they touch only their targets' records;
4. derive the targets' expected outcomes from the facts the injectors built in, with the
   documented rules (:func:`coldchain.generate.expected.derive_expected_outcomes`), never by
   running the compliance engine;
5. build ``anomalies`` (one row per affected shipment, DECISIONS.md #8).

Injection is the one deliberate exception to "raw tables are never modified" (DECISIONS.md
#38); the clean tables are kept in the baseline snapshot so every change stays provable. It is
a pure function (DataFrames in, DataFrames out); file I/O stays in the CLI.
"""

import logging
from collections.abc import Mapping

import numpy as np
import pandas as pd

from coldchain.config import AppConfig
from coldchain.enums import IssueType, TableName
from coldchain.errors import InjectionError
from coldchain.generate.expected import applied_policies, derive_expected_outcomes
from coldchain.inject import anomalies
from coldchain.io import BASELINE_TABLES, coerce_to_schema, raw_tables, sort_by_primary_key
from coldchain.rng import stage_rng
from coldchain.schemas import validate

logger = logging.getLogger(__name__)

INJECT_INPUTS: tuple[TableName, ...] = BASELINE_TABLES
"""Tables the injector reads: every raw table and the natural ``expected_outcomes``."""

INJECT_OUTPUTS: tuple[TableName, ...] = (
    *raw_tables(),
    TableName.EXPECTED_OUTCOMES,
    TableName.ANOMALIES,
)
"""Tables the injector returns: the raw tables (with the anomalies), and the ground truth."""


def _applied(tables: Mapping[TableName, pd.DataFrame]) -> pd.DataFrame:
    """Applied policy of every shipment (the generator's §1.1 resolution)."""
    return applied_policies(
        tables[TableName.SHIPMENTS],
        tables[TableName.SHIPMENT_LOTS],
        tables[TableName.LOTS],
        tables[TableName.PRODUCTS],
        tables[TableName.THRESHOLD_POLICIES],
    )


def inject_tables(
    config: AppConfig, tables: Mapping[TableName, pd.DataFrame]
) -> dict[TableName, pd.DataFrame]:
    """Inject every anomaly into the baseline tables and build the updated ground truth.

    Args:
        config: Validated configuration (``seed``, ``injection_rates``, ``injection``).
        tables: The baseline tables in :data:`INJECT_INPUTS` (extra tables are ignored).

    Returns:
        The tables in :data:`INJECT_OUTPUTS`, coerced to their schema dtypes, sorted by primary
        key and validated. Tables and rows of non-target shipments are unchanged.

    Raises:
        InjectionError: If an input table is missing or an issue type cannot get its targets.
        GenerationError: If a baseline invariant is broken (e.g. a shipment without lots).
        SchemaValidationError: If an output table fails its schema.
    """
    missing = [name.value for name in INJECT_INPUTS if name not in tables]
    if missing:
        raise InjectionError(f"Missing baseline input tables: {missing}")

    def rng(key: str) -> np.random.Generator:
        return stage_rng(config.seed, anomalies.STAGES[key])

    raw = {name: tables[name] for name in raw_tables()}
    outcomes = tables[TableName.EXPECTED_OUTCOMES]
    applied = _applied(raw)
    targets = anomalies.select_targets(config, raw, outcomes, applied, rng("targets"))

    injections = []
    for issue in IssueType:
        chosen = targets[targets["issue_type"] == issue.value].reset_index(drop=True)
        if chosen.empty:
            continue
        injection = anomalies.INJECTORS[issue](config, raw, chosen, applied, rng(issue.value))
        raw.update(injection.tables)
        injections.append(injection)
        logger.info("Injected %d %s anomalies", chosen["anomaly"].nunique(), issue.value)

    ships = targets["shipment_id"]
    is_target = outcomes["shipment_id"].isin(ships)
    derived = derive_expected_outcomes(
        config,
        outcomes.loc[is_target, ["shipment_id", "scenario"]],
        _applied(raw).pipe(lambda frame: frame[frame["shipment_id"].isin(ships)]),
        raw[TableName.LOGGERS],
        raw[TableName.DEVICES],
        raw[TableName.ROUTE_STOPS],
        pd.concat([i.segments for i in injections] or [anomalies.empty_segments()]),
        pd.concat([i.evidence for i in injections]) if injections else None,
    )
    derived["issue_type"] = derived["shipment_id"].map(
        targets.set_index("shipment_id")["issue_type"]
    )
    updated = pd.concat([outcomes[~is_target], derived], ignore_index=True)
    records = (
        pd.concat([i.records for i in injections], ignore_index=True)
        if injections
        else pd.DataFrame(columns=list(anomalies.RECORD_COLUMNS))
    )

    built = {
        **raw,
        TableName.EXPECTED_OUTCOMES: updated,
        TableName.ANOMALIES: anomalies.anomaly_table(records, updated),
    }
    result = {}
    for name in INJECT_OUTPUTS:
        typed = sort_by_primary_key(name, coerce_to_schema(name, built[name]))
        result[name] = validate(name, typed)
    return result
