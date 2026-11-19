"""The single place where every entity ID is formatted (PROJECT_PLAN.md section 5.1).

Scalar builders take 1-based sequence numbers; :func:`reading_ids` is the vectorised builder
used for the (large) temperature-readings table. :data:`ID_PATTERNS` holds the matching regular
expressions so that schemas and tests validate against the same definition.
"""

import re
from datetime import UTC, datetime

import pandas as pd

from coldchain.errors import IdFormatError

DEFAULT_ID_YEAR = 2026
"""Year embedded in year-scoped IDs (lots, shipments, excursion reports)."""

READING_TS_FORMAT = "%Y%m%dT%H%M"
"""strftime format of the timestamp part of a reading ID (UTC)."""

_SHIPMENT = r"SHP-\d{4}-\d{6}"
_LOGGER = rf"LOG-{_SHIPMENT}-\d{{2}}"

ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "party": re.compile(r"^PTY-\d{4}$"),
    "product": re.compile(r"^PRD-\d{4}$"),
    "policy": re.compile(r"^THR-[A-Z0-9_]+-V[1-9]\d*$"),
    "packaging": re.compile(r"^PKG-\d{4}$"),
    "lot": re.compile(r"^LOT-\d{4}-\d{6}$"),
    "device": re.compile(r"^DEV-\d{6}$"),
    "shipment": re.compile(rf"^{_SHIPMENT}$"),
    "logger": re.compile(rf"^{_LOGGER}$"),
    "reading": re.compile(rf"^TEMP-{_LOGGER}-\d{{8}}T\d{{4}}$"),
    "route_stop": re.compile(rf"^RST-{_SHIPMENT}-\d{{2}}$"),
    "custody_event": re.compile(rf"^CUS-{_SHIPMENT}-\d{{2}}$"),
    "status_event": re.compile(rf"^STE-{_SHIPMENT}-\d{{2}}$"),
    "excursion_report": re.compile(r"^EXCR-\d{4}-\d{6}$"),
    "anomaly": re.compile(r"^ANOM-\d{6}$"),
}
"""Anchored regex per entity, keyed by a short entity name."""


def _padded(number: int, width: int) -> str:
    """Zero-pad a positive integer, failing fast if it is invalid or too wide.

    Args:
        number: 1-based sequence number.
        width: Number of digits.

    Returns:
        The zero-padded number as a string.

    Raises:
        IdFormatError: If ``number`` is not a positive int or does not fit in ``width`` digits.
    """
    if isinstance(number, bool) or not isinstance(number, int):
        raise IdFormatError(f"ID number must be an int, got {type(number).__name__}: {number!r}")
    if number < 1 or number >= 10**width:
        raise IdFormatError(f"ID number {number} is outside 1..{10**width - 1} (width {width})")
    return f"{number:0{width}d}"


def _year(year: int) -> str:
    """Format a 4-digit year, failing fast on anything else."""
    if isinstance(year, bool) or not isinstance(year, int) or not 1000 <= year <= 9999:
        raise IdFormatError(f"Year must be a 4-digit int, got {year!r}")
    return str(year)


def _check(kind: str, value: str) -> str:
    """Validate that ``value`` matches the pattern for ``kind``."""
    if not isinstance(value, str) or not ID_PATTERNS[kind].match(value):
        raise IdFormatError(f"Not a valid {kind} ID: {value!r}")
    return value


def party_id(number: int) -> str:
    """Build a party ID, e.g. ``PTY-0042``."""
    return f"PTY-{_padded(number, 4)}"


def product_id(number: int) -> str:
    """Build a product ID, e.g. ``PRD-0007``."""
    return f"PRD-{_padded(number, 4)}"


def policy_id(policy_code: str, version: int) -> str:
    """Build a threshold-policy ID, e.g. ``THR-REFRIG_2_8-V1``.

    Args:
        policy_code: Upper-case policy code (letters, digits, underscores).
        version: Policy version, starting at 1.

    Returns:
        The policy ID.

    Raises:
        IdFormatError: If the code or version is invalid.
    """
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise IdFormatError(f"Policy version must be an int >= 1, got {version!r}")
    return _check("policy", f"THR-{policy_code}-V{version}")


def packaging_id(number: int) -> str:
    """Build a packaging ID, e.g. ``PKG-0003``."""
    return f"PKG-{_padded(number, 4)}"


def lot_id(number: int, year: int = DEFAULT_ID_YEAR) -> str:
    """Build a lot ID, e.g. ``LOT-2026-000123``."""
    return f"LOT-{_year(year)}-{_padded(number, 6)}"


def device_id(number: int) -> str:
    """Build a physical device ID, e.g. ``DEV-000981``."""
    return f"DEV-{_padded(number, 6)}"


def shipment_id(number: int, year: int = DEFAULT_ID_YEAR) -> str:
    """Build a shipment ID, e.g. ``SHP-2026-004999``."""
    return f"SHP-{_year(year)}-{_padded(number, 6)}"


def logger_id(shipment: str, number: int = 1) -> str:
    """Build a logger-assignment ID, e.g. ``LOG-SHP-2026-000001-01``.

    Args:
        shipment: A valid shipment ID.
        number: Assignment number within the shipment (default 1).

    Returns:
        The logger ID.
    """
    return f"LOG-{_check('shipment', shipment)}-{_padded(number, 2)}"


def route_stop_id(shipment: str, sequence: int) -> str:
    """Build a route-stop ID, e.g. ``RST-SHP-2026-000001-02``."""
    return f"RST-{_check('shipment', shipment)}-{_padded(sequence, 2)}"


def custody_event_id(shipment: str, sequence: int) -> str:
    """Build a custody-event ID, e.g. ``CUS-SHP-2026-000001-03``."""
    return f"CUS-{_check('shipment', shipment)}-{_padded(sequence, 2)}"


def status_event_id(shipment: str, sequence: int) -> str:
    """Build a shipment-status-event ID, e.g. ``STE-SHP-2026-000001-04``."""
    return f"STE-{_check('shipment', shipment)}-{_padded(sequence, 2)}"


def excursion_report_id(number: int, year: int = DEFAULT_ID_YEAR) -> str:
    """Build an excursion-report ID, e.g. ``EXCR-2026-000057``."""
    return f"EXCR-{_year(year)}-{_padded(number, 6)}"


def anomaly_id(number: int) -> str:
    """Build a ground-truth anomaly ID, e.g. ``ANOM-000014``."""
    return f"ANOM-{_padded(number, 6)}"


def reading_id(logger: str, recorded_at: datetime) -> str:
    """Build a temperature-reading ID, e.g. ``TEMP-LOG-SHP-2026-000001-01-20260310T0910``.

    Args:
        logger: A valid logger ID.
        recorded_at: Timezone-aware timestamp; converted to UTC before formatting.

    Returns:
        The reading ID.

    Raises:
        IdFormatError: If the logger ID is invalid or the timestamp is naive.
    """
    if recorded_at.tzinfo is None:
        raise IdFormatError("recorded_at must be timezone-aware (UTC)")
    stamp = recorded_at.astimezone(UTC).strftime(READING_TS_FORMAT)
    return f"TEMP-{_check('logger', logger)}-{stamp}"


def reading_ids(loggers: pd.Series, recorded_at: pd.Series) -> pd.Series:
    """Vectorised :func:`reading_id` for whole columns.

    Args:
        loggers: Series of logger IDs (assumed already valid; validated by the schemas).
        recorded_at: Series of timezone-aware timestamps, aligned with ``loggers``.

    Returns:
        Series of reading IDs with the same index as ``loggers``.

    Raises:
        IdFormatError: If the series lengths differ or the timestamps are naive.
    """
    if len(loggers) != len(recorded_at):
        raise IdFormatError(f"Length mismatch: {len(loggers)} loggers vs {len(recorded_at)} times")
    times = pd.to_datetime(recorded_at)
    if times.dt.tz is None:
        raise IdFormatError("recorded_at must be timezone-aware (UTC)")
    stamps = times.dt.tz_convert("UTC").dt.strftime(READING_TS_FORMAT)
    return "TEMP-" + loggers.astype(str) + "-" + stamps.to_numpy()


def _check_lengths(left: pd.Series, right: pd.Series) -> None:
    """Fail if two aligned columns differ in length."""
    if len(left) != len(right):
        raise IdFormatError(f"Length mismatch: {len(left)} vs {len(right)} values")


def _padded_column(numbers: pd.Series, width: int) -> pd.Series:
    """Vectorised :func:`_padded`: zero-pad a column of positive ints."""
    values = pd.Series(numbers)
    if not pd.api.types.is_integer_dtype(values) or values.isna().any():
        raise IdFormatError(f"ID numbers must be a non-null integer column, got {values.dtype}")
    if len(values) and (values.min() < 1 or values.max() >= 10**width):
        raise IdFormatError(f"ID numbers must be within 1..{10**width - 1} (width {width})")
    return values.astype("int64").astype(str).str.zfill(width)


def logger_ids(shipments: pd.Series, number: int = 1) -> pd.Series:
    """Vectorised :func:`logger_id` for a column of shipment IDs.

    Args:
        shipments: Series of shipment IDs (assumed valid; validated by the schemas).
        number: Assignment number within each shipment (default 1).

    Returns:
        Series of logger IDs with the same index as ``shipments``.
    """
    return "LOG-" + shipments.astype(str) + f"-{_padded(number, 2)}"


def _scoped_ids(prefix: str, shipments: pd.Series, sequences: pd.Series) -> pd.Series:
    """Build ``<prefix>-<shipment-id>-<nn>`` IDs for aligned columns."""
    _check_lengths(shipments, sequences)
    padded = _padded_column(sequences, 2).to_numpy()
    return f"{prefix}-" + shipments.astype(str) + "-" + padded


def route_stop_ids(shipments: pd.Series, sequences: pd.Series) -> pd.Series:
    """Vectorised :func:`route_stop_id`."""
    return _scoped_ids("RST", shipments, sequences)


def custody_event_ids(shipments: pd.Series, sequences: pd.Series) -> pd.Series:
    """Vectorised :func:`custody_event_id`."""
    return _scoped_ids("CUS", shipments, sequences)


def status_event_ids(shipments: pd.Series, sequences: pd.Series) -> pd.Series:
    """Vectorised :func:`status_event_id`."""
    return _scoped_ids("STE", shipments, sequences)


def excursion_report_ids(numbers: pd.Series, year: int = DEFAULT_ID_YEAR) -> pd.Series:
    """Vectorised :func:`excursion_report_id` for a column of 1-based report numbers.

    Args:
        numbers: Integer column of report numbers (each within 1..999999).
        year: ID-series year.

    Returns:
        Series of report IDs (e.g. ``EXCR-2026-000057``) with the same index as ``numbers``.

    Raises:
        IdFormatError: If a number is missing, not an integer or out of range.
    """
    return f"EXCR-{_year(year)}-" + _padded_column(numbers, 6)


def embedded_shipment_ids(child_ids: pd.Series) -> pd.Series:
    """Extract the shipment ID embedded in shipment-scoped IDs.

    Works for logger, reading, route-stop, custody-event and status-event IDs.

    Args:
        child_ids: Series of shipment-scoped IDs.

    Returns:
        Series of shipment IDs (missing where no shipment ID is embedded).
    """
    return child_ids.astype(str).str.extract(f"({_SHIPMENT})", expand=False)
