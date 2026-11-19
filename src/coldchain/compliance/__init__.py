"""Compliance engine: decides per shipment whether it stayed within its policy.

:func:`check_tables` applies ``docs/compliance_rules.md`` to the raw tables only (never to the
ground truth), in the documented evaluation order (§2):

1. policy resolution from the products and actual departure (§1.1) — :mod:`.policy`;
2. logger assignment, trusted readings and calibration (§1.2, §1.7) — :mod:`.logger_check`;
3. coverage and gaps (§1.3, §1.4) — :mod:`.gaps`;
4. excursions (§1.5) and calibration uncertainty (§1.6) — :mod:`.excursions`;
5. custody chain (§1.8) and dwell (§1.9) — :mod:`.custody`;
6. most severe status → action, then the carrier claim (§2, §1.10), and the appended status
   events (§4) — :mod:`.report`.

It is a pure function (DataFrames in, DataFrames out); file I/O stays in the CLI.
"""

import logging
from collections.abc import Mapping

import pandas as pd

from coldchain.compliance import custody, excursions, gaps, logger_check, policy, report
from coldchain.config import AppConfig
from coldchain.enums import TableName
from coldchain.errors import ComplianceError
from coldchain.io import coerce_to_schema, sort_by_primary_key
from coldchain.schemas import validate

logger = logging.getLogger(__name__)

ENGINE_INPUTS: tuple[TableName, ...] = (
    TableName.PARTIES,
    TableName.THRESHOLD_POLICIES,
    TableName.PRODUCTS,
    TableName.LOTS,
    TableName.DEVICES,
    TableName.SHIPMENTS,
    TableName.SHIPMENT_LOTS,
    TableName.LOGGERS,
    TableName.TEMPERATURE_READINGS,
    TableName.ROUTE_STOPS,
    TableName.CUSTODY_EVENTS,
    TableName.SHIPMENT_STATUS_EVENTS,
)
"""The raw tables the engine reads. All of them belong to the ``raw`` layer (tested)."""

ENGINE_OUTPUTS: tuple[TableName, ...] = (
    TableName.EXCURSION_REPORTS,
    TableName.EXCURSION_REPORT_READINGS,
    TableName.COMPLIANCE_DECISIONS,
    TableName.COMPLIANCE_STATUS_EVENTS,
)
"""The tables the engine writes (the ``compliance`` layer)."""


def check_tables(
    config: AppConfig, raw: Mapping[TableName, pd.DataFrame]
) -> dict[TableName, pd.DataFrame]:
    """Run every compliance rule and build the engine's output tables.

    Args:
        config: Validated configuration (``reading_interval_min`` for shipments without a
            logger, ``long_dwell_threshold_min``).
        raw: The raw tables in :data:`ENGINE_INPUTS` (extra tables are ignored).

    Returns:
        The four compliance tables in :data:`ENGINE_OUTPUTS`, coerced to their schema dtypes,
        sorted by primary key and validated.

    Raises:
        ComplianceError: If an input table is missing or an invariant is broken (mixed policy
            codes, no lots, no effective policy version, unknown device, no reviewer party).
        SchemaValidationError: If an output table fails its schema.
    """
    missing = [name.value for name in ENGINE_INPUTS if name not in raw]
    if missing:
        raise ComplianceError(f"Missing raw input tables: {missing}")
    shipments = raw[TableName.SHIPMENTS]
    index = pd.Index(shipments["shipment_id"], name="shipment_id")

    # 1. Applied policy (§1.1).
    policies = policy.resolve_policies(
        shipments,
        raw[TableName.SHIPMENT_LOTS],
        raw[TableName.LOTS],
        raw[TableName.PRODUCTS],
        raw[TableName.THRESHOLD_POLICIES],
    )
    # 2. Logger, trusted readings, calibration (§1.2, §1.7).
    windows = logger_check.assignment_windows(
        shipments, raw[TableName.LOGGERS], raw[TableName.DEVICES], config.reading_interval_min
    )
    screened = logger_check.screen_readings(raw[TableName.TEMPERATURE_READINGS], windows)
    trusted = logger_check.trusted_readings(screened)
    logger_flags = logger_check.logger_flags(windows, screened)
    # 3. Coverage and gaps (§1.3, §1.4).
    coverage = gaps.coverage_and_gaps(windows, trusted, policies)
    # 4. Excursions and uncertainty (§1.5, §1.6).
    reports, bridge = excursions.detect_excursions(trusted, windows, policies)
    summary = excursions.excursion_summary(reports, policies)
    near_limit = excursions.near_limit_flags(trusted, windows, policies)
    # 5. Custody and dwell (§1.8, §1.9).
    custody_gap = custody.custody_gap_flags(shipments, raw[TableName.CUSTODY_EVENTS])
    long_dwell = custody.long_dwell_flags(
        shipments, raw[TableName.ROUTE_STOPS], config.long_dwell_threshold_min
    )
    # 6. Status, action, carrier claim (§2, §1.10); appended lifecycle events (§4).
    by_shipment = policies.set_index("shipment_id")
    matrix = report.reason_matrix(
        index, [by_shipment, logger_flags, coverage, summary, near_limit, custody_gap, long_dwell]
    )
    decisions = report.build_decisions(shipments, by_shipment, coverage, summary, matrix)
    events = report.status_events(
        decisions, windows, raw[TableName.SHIPMENT_STATUS_EVENTS], raw[TableName.PARTIES]
    )

    built = {
        TableName.EXCURSION_REPORTS: reports,
        TableName.EXCURSION_REPORT_READINGS: bridge,
        TableName.COMPLIANCE_DECISIONS: decisions,
        TableName.COMPLIANCE_STATUS_EVENTS: events,
    }
    result = {}
    for name in ENGINE_OUTPUTS:
        typed = sort_by_primary_key(name, coerce_to_schema(name, built[name]))
        result[name] = validate(name, typed)
    logger.info(
        "Checked %d shipments: %s; %d excursion reports",
        len(decisions),
        ", ".join(
            f"{status} {count}"
            for status, count in decisions["status"].value_counts().sort_index().items()
        )
        or "none",
        len(reports),
    )
    return result
