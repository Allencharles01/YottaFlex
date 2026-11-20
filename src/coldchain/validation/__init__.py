"""Validation suite (``coldchain validate``): judges data integrity on its own.

:func:`validate_tables` runs every check in :data:`~coldchain.validation.checks.CHECKS` on the
raw tables and, when present, the compliance engine's output. It reads nothing else: the suite
never sees the answer key, and never imports the engine (it recomputes what it needs).

Each check ends PASS, FAIL (with one finding per failing record), ERROR (it could not run on
the data) or SKIPPED (a table it needs is not part of the input). INTEGRITY failures and errors
make the run fail; BUSINESS_RULE findings are reported for ``coldchain evaluate`` to explain
(DECISIONS.md #84).
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass

import duckdb
import pandas as pd

from coldchain.config import AppConfig, ReferenceData
from coldchain.enums import CheckStatus, CheckTier, Layer, TableName
from coldchain.errors import ValidationError
from coldchain.schemas import TABLE_LAYERS
from coldchain.validation.checks import CHECKS, FINDING_FIELDS, Check, empty_findings
from coldchain.validation.context import Limits, ValidationContext

logger = logging.getLogger(__name__)

RAW_INPUTS: tuple[TableName, ...] = tuple(
    t for t, layer in TABLE_LAYERS.items() if layer is Layer.RAW
)
"""Raw tables the suite requires."""

COMPLIANCE_INPUTS: tuple[TableName, ...] = tuple(
    t for t, layer in TABLE_LAYERS.items() if layer is Layer.COMPLIANCE
)
"""Engine output tables the suite judges when they are present (all or none)."""

FINDING_COLUMNS: tuple[str, ...] = ("check_id", "tier", "table", *FINDING_FIELDS)
"""Columns of :attr:`ValidationResult.findings`."""

_CHECK_ERRORS = (KeyError, ValueError, TypeError, IndexError, AttributeError, duckdb.Error)
"""Exceptions that mean a check could not run on malformed data (reported as ERROR)."""


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one check.

    Attributes:
        check: The registered check.
        status: PASS, FAIL, ERROR or SKIPPED.
        checked: Number of records examined (0 when skipped or errored).
        findings: One row per failing record (:data:`FINDING_FIELDS`), sorted.
        message: Why the check was skipped or errored, else empty.
    """

    check: Check
    status: CheckStatus
    checked: int
    findings: pd.DataFrame
    message: str = ""

    @property
    def failed(self) -> int:
        """Number of failing records."""
        return len(self.findings)

    @property
    def shipments(self) -> int:
        """Number of distinct shipments with a finding."""
        return int(self.findings["shipment_id"].dropna().nunique())


@dataclass(frozen=True)
class ValidationResult:
    """Every check result of one validation run.

    Attributes:
        results: One result per registered check, in report order.
    """

    results: tuple[CheckResult, ...]

    @property
    def findings(self) -> pd.DataFrame:
        """All findings, with ``check_id``, ``tier`` and ``table`` columns, sorted."""
        frames = [
            r.findings.assign(
                check_id=r.check.check_id, tier=r.check.tier.value, table=r.check.table.value
            )
            for r in self.results
            if r.failed
        ]
        if not frames:
            return pd.DataFrame({name: pd.Series(dtype=object) for name in FINDING_COLUMNS})
        combined = pd.concat(frames, ignore_index=True)[list(FINDING_COLUMNS)]
        return combined.sort_values(["check_id", "record_id", "detail"], kind="stable").reset_index(
            drop=True
        )

    def count(self, status: CheckStatus, tier: CheckTier | None = None) -> int:
        """Number of checks with a status (optionally within one tier)."""
        return sum(
            r.status is status and (tier is None or r.check.tier is tier) for r in self.results
        )

    @property
    def integrity_failures(self) -> tuple[CheckResult, ...]:
        """INTEGRITY checks that failed or could not run."""
        return tuple(
            r
            for r in self.results
            if r.check.tier is CheckTier.INTEGRITY
            and r.status in (CheckStatus.FAIL, CheckStatus.ERROR)
        )

    @property
    def business_rule_findings(self) -> int:
        """Number of findings from BUSINESS_RULE checks."""
        return sum(r.failed for r in self.results if r.check.tier is CheckTier.BUSINESS_RULE)

    @property
    def passed(self) -> bool:
        """``True`` when no INTEGRITY check failed or errored (business findings allowed)."""
        return not self.integrity_failures


def run_check(check: Check, ctx: ValidationContext) -> CheckResult:
    """Run one check, turning missing inputs into SKIPPED and crashes on bad data into ERROR.

    Args:
        check: The check.
        ctx: Shared inputs.

    Returns:
        Its result.
    """
    missing = [name.value for name in check.needs if not ctx.has(name)]
    if missing:
        return CheckResult(check, CheckStatus.SKIPPED, 0, empty_findings(), f"needs {missing}")
    try:
        outcome = check.run(ctx)
    except _CHECK_ERRORS as exc:
        logger.warning("Check %s could not run: %s", check.check_id, exc)
        message = f"{type(exc).__name__}: {exc}"
        return CheckResult(check, CheckStatus.ERROR, 0, empty_findings(), message)
    findings = outcome.findings[list(FINDING_FIELDS)].astype(object)
    findings = findings.where(findings.notna(), None).astype({"record_id": str, "detail": str})
    findings = findings.sort_values(["record_id", "detail"], kind="stable").reset_index(drop=True)
    status = CheckStatus.FAIL if len(findings) else CheckStatus.PASS
    return CheckResult(check, status, outcome.checked, findings)


def validate_tables(
    config: AppConfig,
    reference: ReferenceData,
    tables: Mapping[TableName, pd.DataFrame],
    checks: tuple[Check, ...] = CHECKS,
) -> ValidationResult:
    """Run the validation suite on a set of tables.

    Args:
        config: Pipeline configuration (reading interval, date window, trip and lead ranges).
        reference: Reference data (cities).
        tables: Every table of :data:`RAW_INPUTS`, plus optionally all of
            :data:`COMPLIANCE_INPUTS`. Tables should keep their schema dtypes (read them with
            ``check_schema=False`` so that schema violations become findings).
        checks: The checks to run (default: all).

    Returns:
        The result of every check.

    Raises:
        ValidationError: If a raw table is missing, a table outside the raw and compliance
            layers is given, or only some compliance tables are given.
    """
    missing = [name.value for name in RAW_INPUTS if name not in tables]
    if missing:
        raise ValidationError(f"Missing raw input tables: {missing}")
    foreign = [name.value for name in tables if name not in (*RAW_INPUTS, *COMPLIANCE_INPUTS)]
    if foreign:
        raise ValidationError(f"Validation judges raw and compliance tables only, got {foreign}")
    given = [name for name in COMPLIANCE_INPUTS if name in tables]
    if given and len(given) != len(COMPLIANCE_INPUTS):
        absent = sorted(name.value for name in set(COMPLIANCE_INPUTS) - set(given))
        raise ValidationError(f"Give all compliance tables or none; missing {absent}")
    ctx = ValidationContext(dict(tables), Limits.from_config(config, reference))
    try:
        results = tuple(run_check(check, ctx) for check in checks)
    finally:
        ctx.close()
    result = ValidationResult(results)
    logger.info(
        "Validated %d checks: %d pass, %d fail, %d error, %d skipped; %d integrity failure(s), "
        "%d business-rule finding(s)",
        len(results),
        result.count(CheckStatus.PASS),
        result.count(CheckStatus.FAIL),
        result.count(CheckStatus.ERROR),
        result.count(CheckStatus.SKIPPED),
        len(result.integrity_failures),
        result.business_rule_findings,
    )
    return result
