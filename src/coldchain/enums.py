"""All allowed categorical values used across the project, and the fixed mappings between them.

Every enum is a :class:`enum.StrEnum`, so members compare equal to their string values and
serialise naturally to CSV/JSON/YAML. The status/reason/action mappings below are the single
source of the decision table in ``docs/compliance_rules.md``.
"""

from collections.abc import Iterable
from enum import StrEnum


class Scale(StrEnum):
    """Run size presets defined in ``config/default.yaml``."""

    STARTER = "starter"
    FULL = "full"


class PartyType(StrEnum):
    """Role of a party (an actor from the brief, represented as data)."""

    SUPPLIER = "SUPPLIER"
    CARRIER = "CARRIER"
    WAREHOUSE = "WAREHOUSE"
    RECEIVER = "RECEIVER"
    PLANNER = "PLANNER"
    LOGISTICS_ANALYST = "LOGISTICS_ANALYST"
    OPERATIONS_MANAGER = "OPERATIONS_MANAGER"


class ProductCategory(StrEnum):
    """Product category."""

    VACCINE = "VACCINE"
    BIOLOGIC = "BIOLOGIC"
    MEDICINE = "MEDICINE"
    FROZEN = "FROZEN"
    AMBIENT_PHARMA = "AMBIENT_PHARMA"


class PolicyCode(StrEnum):
    """Temperature class of a threshold policy."""

    REFRIG_2_8 = "REFRIG_2_8"
    COOL_8_15 = "COOL_8_15"
    FROZEN = "FROZEN"
    AMBIENT_15_25 = "AMBIENT_15_25"


class LotStatus(StrEnum):
    """Release status of a production lot."""

    RELEASED = "RELEASED"
    ON_HOLD = "ON_HOLD"


class PackagingType(StrEnum):
    """Type of thermal packaging."""

    ACTIVE_REEFER = "ACTIVE_REEFER"
    PASSIVE_GEL = "PASSIVE_GEL"
    DRY_ICE = "DRY_ICE"
    INSULATED_BOX = "INSULATED_BOX"


class CarrierReportedStatus(StrEnum):
    """The carrier's own pass/fail claim for a shipment (an input, may be wrong)."""

    PASS = "PASS"
    FAIL = "FAIL"


class StopType(StrEnum):
    """Type of a route stop."""

    ORIGIN = "ORIGIN"
    WAREHOUSE = "WAREHOUSE"
    CROSS_DOCK = "CROSS_DOCK"
    DESTINATION = "DESTINATION"


class CustodyEventType(StrEnum):
    """Type of a custody transfer event."""

    PICKUP = "PICKUP"
    HANDOFF = "HANDOFF"
    DELIVERY = "DELIVERY"


class ShipmentStatus(StrEnum):
    """Shipment lifecycle status (generator states, then compliance-engine states)."""

    PLANNED = "PLANNED"
    PICKED_UP = "PICKED_UP"
    IN_TRANSIT = "IN_TRANSIT"
    AT_STOP = "AT_STOP"
    DELIVERED = "DELIVERED"
    UNDER_REVIEW = "UNDER_REVIEW"
    RELEASED = "RELEASED"
    QUARANTINED = "QUARANTINED"


class ExcursionDirection(StrEnum):
    """Direction of a temperature excursion."""

    HIGH = "HIGH"
    LOW = "LOW"


class BreachType(StrEnum):
    """Classification of a single excursion report."""

    WITHIN_ALLOWANCE = "WITHIN_ALLOWANCE"
    SINGLE_DURATION_BREACH = "SINGLE_DURATION_BREACH"
    ABSOLUTE_LIMIT_BREACH = "ABSOLUTE_LIMIT_BREACH"


class ComplianceStatus(StrEnum):
    """Final compliance status of a shipment."""

    COMPLIANT = "COMPLIANT"
    COMPLIANT_WITH_WARNINGS = "COMPLIANT_WITH_WARNINGS"
    INCONCLUSIVE = "INCONCLUSIVE"
    NON_COMPLIANT = "NON_COMPLIANT"


class PolicySource(StrEnum):
    """How the applied policy relates to the planner's declared policy."""

    DECLARED_CONFIRMED = "DECLARED_CONFIRMED"
    PRODUCT_OVERRIDE = "PRODUCT_OVERRIDE"


class DecidedAction(StrEnum):
    """Action that follows from a compliance status."""

    RELEASE = "RELEASE"
    QUARANTINE = "QUARANTINE"
    REVIEW = "REVIEW"


class ReasonCode(StrEnum):
    """Reason codes attached to compliance decisions (see PROJECT_PLAN.md section 6)."""

    ABSOLUTE_LIMIT_BREACH = "ABSOLUTE_LIMIT_BREACH"
    SINGLE_DURATION_BREACH = "SINGLE_DURATION_BREACH"
    CUMULATIVE_DURATION_BREACH = "CUMULATIVE_DURATION_BREACH"
    LOGGER_NOT_ASSIGNED = "LOGGER_NOT_ASSIGNED"
    DATA_GAP = "DATA_GAP"
    LOW_COVERAGE = "LOW_COVERAGE"
    CUSTODY_GAP = "CUSTODY_GAP"
    CALIBRATION_EXPIRED = "CALIBRATION_EXPIRED"
    EXCURSION_WITHIN_ALLOWANCE = "EXCURSION_WITHIN_ALLOWANCE"
    NEAR_LIMIT_UNCERTAIN = "NEAR_LIMIT_UNCERTAIN"
    POLICY_MISMATCH_DECLARED = "POLICY_MISMATCH_DECLARED"
    LONG_DWELL = "LONG_DWELL"
    CARRIER_STATUS_CONTRADICTED = "CARRIER_STATUS_CONTRADICTED"


class Scenario(StrEnum):
    """Natural (valid-data) scenarios assigned by the generator."""

    NORMAL = "NORMAL"
    MINOR_SPIKE_WITHIN_ALLOWANCE = "MINOR_SPIKE_WITHIN_ALLOWANCE"
    BOUNDARY = "BOUNDARY"
    EXCURSION_BREACH = "EXCURSION_BREACH"
    LONG_DWELL = "LONG_DWELL"
    CALIBRATION_EXPIRED = "CALIBRATION_EXPIRED"


class ScenarioVariant(StrEnum):
    """How a scenario is shaped (generator-internal; the ground truth records the scenario).

    ``NONE`` is used by scenarios with a single shape (NORMAL, LONG_DWELL, CALIBRATION_EXPIRED).
    """

    NONE = "NONE"
    SPIKE = "SPIKE"
    AT_LIMIT = "AT_LIMIT"
    EXACT_ALLOWANCE = "EXACT_ALLOWANCE"
    SINGLE_DURATION = "SINGLE_DURATION"
    ABSOLUTE_LIMIT = "ABSOLUTE_LIMIT"
    CUMULATIVE_DURATION = "CUMULATIVE_DURATION"


class IssueType(StrEnum):
    """Deliberately injected edge-case types (ground-truth anomalies)."""

    LOGGER_NOT_ASSIGNED = "logger_not_assigned"
    PRODUCT_THRESHOLD_MISMATCH = "product_threshold_mismatch"
    MISSING_DATA_SILENTLY_PASSED = "missing_data_silently_passed"
    CUSTODY_GAP_NOT_FLAGGED = "custody_gap_not_flagged"
    EXCURSION_ACROSS_UNRELATED_SHIPMENTS = "excursion_across_unrelated_shipments"


class Layer(StrEnum):
    """Output layer (sub-folder of ``output/<scale>/``) that a table belongs to."""

    RAW = "raw"
    COMPLIANCE = "compliance"
    GROUND_TRUTH = "ground_truth"


class FileFormat(StrEnum):
    """On-disk table format; the value is also the file extension."""

    PARQUET = "parquet"
    CSV = "csv"
    JSON = "json"


class TableName(StrEnum):
    """Every table in the data model; the value is also the file stem on disk."""

    PARTIES = "parties"
    THRESHOLD_POLICIES = "threshold_policies"
    PRODUCTS = "products"
    PACKAGING = "packaging"
    LOTS = "lots"
    DEVICES = "devices"
    SHIPMENTS = "shipments"
    SHIPMENT_LOTS = "shipment_lots"
    LOGGERS = "loggers"
    TEMPERATURE_READINGS = "temperature_readings"
    ROUTE_STOPS = "route_stops"
    CUSTODY_EVENTS = "custody_events"
    SHIPMENT_STATUS_EVENTS = "shipment_status_events"
    EXCURSION_REPORTS = "excursion_reports"
    EXCURSION_REPORT_READINGS = "excursion_report_readings"
    COMPLIANCE_DECISIONS = "compliance_decisions"
    COMPLIANCE_STATUS_EVENTS = "compliance_status_events"
    EXPECTED_OUTCOMES = "expected_outcomes"
    DEVICE_TRUTH = "device_truth"
    ANOMALIES = "anomalies"


STATUS_SEVERITY: dict[ComplianceStatus, int] = {
    ComplianceStatus.COMPLIANT: 1,
    ComplianceStatus.COMPLIANT_WITH_WARNINGS: 2,
    ComplianceStatus.INCONCLUSIVE: 3,
    ComplianceStatus.NON_COMPLIANT: 4,
}
"""Severity of each compliance status (higher is worse); see docs/compliance_rules.md."""

STATUS_ACTION: dict[ComplianceStatus, DecidedAction] = {
    ComplianceStatus.COMPLIANT: DecidedAction.RELEASE,
    ComplianceStatus.COMPLIANT_WITH_WARNINGS: DecidedAction.RELEASE,
    ComplianceStatus.INCONCLUSIVE: DecidedAction.REVIEW,
    ComplianceStatus.NON_COMPLIANT: DecidedAction.QUARANTINE,
}
"""The action that follows from each compliance status."""

REASON_STATUS: dict[ReasonCode, ComplianceStatus | None] = {
    ReasonCode.ABSOLUTE_LIMIT_BREACH: ComplianceStatus.NON_COMPLIANT,
    ReasonCode.SINGLE_DURATION_BREACH: ComplianceStatus.NON_COMPLIANT,
    ReasonCode.CUMULATIVE_DURATION_BREACH: ComplianceStatus.NON_COMPLIANT,
    ReasonCode.LOGGER_NOT_ASSIGNED: ComplianceStatus.INCONCLUSIVE,
    ReasonCode.DATA_GAP: ComplianceStatus.INCONCLUSIVE,
    ReasonCode.LOW_COVERAGE: ComplianceStatus.INCONCLUSIVE,
    ReasonCode.CUSTODY_GAP: ComplianceStatus.INCONCLUSIVE,
    ReasonCode.CALIBRATION_EXPIRED: ComplianceStatus.INCONCLUSIVE,
    ReasonCode.EXCURSION_WITHIN_ALLOWANCE: ComplianceStatus.COMPLIANT_WITH_WARNINGS,
    ReasonCode.NEAR_LIMIT_UNCERTAIN: ComplianceStatus.COMPLIANT_WITH_WARNINGS,
    ReasonCode.POLICY_MISMATCH_DECLARED: ComplianceStatus.COMPLIANT_WITH_WARNINGS,
    ReasonCode.LONG_DWELL: ComplianceStatus.COMPLIANT_WITH_WARNINGS,
    ReasonCode.CARRIER_STATUS_CONTRADICTED: None,
}
"""Status each reason code triggers; ``None`` means informational (does not affect status)."""


def worst_status(reason_codes: Iterable[ReasonCode | str]) -> ComplianceStatus:
    """Return the most severe status triggered by a set of reason codes.

    Args:
        reason_codes: Reason codes (enum members or their string values).

    Returns:
        The most severe triggered status, or ``COMPLIANT`` if none triggers a status.

    Raises:
        ValueError: If a code is not a valid :class:`ReasonCode`.
    """
    triggered = [REASON_STATUS[ReasonCode(code)] for code in reason_codes]
    statuses = [status for status in triggered if status is not None]
    return max(statuses, key=STATUS_SEVERITY.__getitem__, default=ComplianceStatus.COMPLIANT)
