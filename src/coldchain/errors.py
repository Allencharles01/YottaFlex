"""Custom exceptions for the coldchain package.

All package-specific errors derive from :class:`ColdchainError`, so callers (e.g. the CLI)
can catch one base class and exit cleanly with a clear message.
"""


class ColdchainError(Exception):
    """Base class for every coldchain error."""


class ConfigError(ColdchainError):
    """Raised when configuration is missing, unreadable or fails validation."""


class IdFormatError(ColdchainError, ValueError):
    """Raised when an ID cannot be built from the given parts (bad number, width or type)."""


class SchemaValidationError(ColdchainError):
    """Raised when a table fails its pandera schema (see :mod:`coldchain.schemas`)."""


class GenerationError(ColdchainError):
    """Raised when the data generator cannot satisfy an invariant (e.g. no free device)."""


class TableIOError(ColdchainError):
    """Raised when a table file is missing, unreadable or has the wrong columns."""


class ComplianceError(ColdchainError):
    """Raised when the compliance engine meets a broken invariant it must not guess around.

    Examples: a shipment that mixes policy codes, has no lots, has no policy version effective
    at departure, or whose logger references an unknown device.
    """


class InjectionError(ColdchainError):
    """Raised when the anomaly injector cannot place an anomaly (e.g. too few eligible targets)."""


class ValidationError(ColdchainError):
    """Raised when the validation suite cannot run (e.g. a required input table is missing)."""


class EvaluationError(ColdchainError):
    """Raised when the evaluation cannot run (missing inputs, or a stale validation summary)."""


class ExportError(ColdchainError):
    """Raised when the export cannot be written or proved (stale inputs, SQL that fails to load)."""


class ProvenanceError(ColdchainError):
    """Raised when stage provenance is missing or no longer matches the files (stale pipeline)."""


class ReproducibilityError(ColdchainError):
    """Raised when ``coldchain reproduce`` cannot run (e.g. the control seed equals the seed)."""
