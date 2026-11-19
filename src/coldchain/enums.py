"""All allowed categorical values used across the project.

Every enum is a :class:`enum.StrEnum`, so members compare equal to their string values and
serialise naturally to CSV/JSON/YAML.
"""

from enum import StrEnum


class Scale(StrEnum):
    """Run size presets defined in ``config/default.yaml``."""

    STARTER = "starter"
    FULL = "full"


class PartyType(StrEnum):
    """Role of a party in the supply chain."""

    SUPPLIER = "SUPPLIER"
    CARRIER = "CARRIER"
    WAREHOUSE = "WAREHOUSE"
    RECEIVER = "RECEIVER"


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


class IssueType(StrEnum):
    """Deliberately injected edge-case types (ground-truth anomalies)."""

    LOGGER_NOT_ASSIGNED = "logger_not_assigned"
    PRODUCT_THRESHOLD_MISMATCH = "product_threshold_mismatch"
    MISSING_DATA_SILENTLY_PASSED = "missing_data_silently_passed"
    CUSTODY_GAP_NOT_FLAGGED = "custody_gap_not_flagged"
    EXCURSION_ACROSS_UNRELATED_SHIPMENTS = "excursion_across_unrelated_shipments"
