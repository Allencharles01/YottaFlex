"""Render a validation run as ``validation_summary.json`` (payload) and ``.md`` (text).

Both are pure functions of the result and a few run facts, with no wall-clock time, so the
same data always gives byte-identical reports.
"""

from collections.abc import Mapping
from typing import Any

from coldchain import __version__
from coldchain.enums import CheckCategory, CheckStatus, CheckTier
from coldchain.validation import CheckResult, ValidationResult

SUMMARY_STEM = "validation_summary"
"""File stem of the validation reports."""

MARKDOWN_EXAMPLES = 5
"""Findings listed per failing check in the Markdown report (the JSON lists all)."""


def _check_entry(result: CheckResult) -> dict[str, Any]:
    check = result.check
    return {
        "check_id": check.check_id,
        "category": check.category.value,
        "tier": check.tier.value,
        "table": check.table.value,
        "description": check.description,
        "status": result.status.value,
        "checked": result.checked,
        "failed": result.failed,
        "shipments": result.shipments,
        "message": result.message,
    }


def summary_payload(
    result: ValidationResult, *, scale: str, source: str, inputs: Mapping[str, str]
) -> dict[str, Any]:
    """Build the JSON payload of a validation run.

    Args:
        result: The validation result.
        scale: Scale name.
        source: Which raw copy was judged (``raw`` or ``baseline``).
        inputs: Input file (path relative to the output root) → sha256, in a stable order.

    Returns:
        A JSON-serialisable dict: run facts, totals, one entry per check, and every finding.
    """
    findings = result.findings
    integrity = CheckTier.INTEGRITY
    totals = {
        "checks": len(result.results),
        **{status.value.lower(): result.count(status) for status in CheckStatus},
        "findings": len(findings),
        "integrity_failures": len(result.integrity_failures),
        "integrity_findings": int((findings["tier"] == integrity.value).sum()),
        "business_rule_findings": result.business_rule_findings,
        "shipments_with_findings": int(findings["shipment_id"].dropna().nunique()),
    }
    return {
        "report": SUMMARY_STEM,
        "package_version": __version__,
        "scale": scale,
        "source": source,
        "status": CheckStatus.PASS.value if result.passed else CheckStatus.FAIL.value,
        "inputs": dict(inputs),
        "totals": totals,
        "checks": [_check_entry(r) for r in result.results],
        "findings": findings.to_dict(orient="records"),
    }


def _cell(value: Any) -> str:
    """Markdown table cell text (pipes escaped)."""
    return str(value).replace("|", "\\|")


def summary_markdown(payload: Mapping[str, Any]) -> str:
    """Render the payload as a Markdown report.

    Args:
        payload: The output of :func:`summary_payload`.

    Returns:
        The Markdown text (ends with a newline).
    """
    totals = payload["totals"]
    lines = [
        "# Validation summary",
        "",
        f"- Scale: `{payload['scale']}` · source: `{payload['source']}` · "
        f"package {payload['package_version']}",
        f"- **Overall: {payload['status']}** (INTEGRITY checks must all pass; "
        "BUSINESS_RULE findings are explained by `coldchain evaluate`)",
        f"- Checks: {totals['checks']} — {totals['pass']} PASS, {totals['fail']} FAIL, "
        f"{totals['error']} ERROR, {totals['skipped']} SKIPPED",
        f"- Findings: {totals['findings']} ({totals['integrity_findings']} integrity, "
        f"{totals['business_rule_findings']} business-rule) on "
        f"{totals['shipments_with_findings']} shipment(s)",
        "",
    ]
    checks = payload["checks"]
    for tier in CheckTier:
        lines += [f"## {tier.value.replace('_', '-').title()} checks", ""]
        for category in CheckCategory:
            members = [
                c for c in checks if c["tier"] == tier.value and c["category"] == category.value
            ]
            if not members:
                continue
            lines += [
                f"### {category.value.replace('_', ' ').title()}",
                "",
                "| Check | Status | Checked | Failed | Shipments | Rule |",
                "|---|---|---:|---:|---:|---|",
            ]
            for c in members:
                rule = c["description"] + (f" ({c['message']})" if c["message"] else "")
                lines.append(
                    f"| `{c['check_id']}` | {c['status']} | {c['checked']} | {c['failed']} | "
                    f"{c['shipments']} | {_cell(rule)} |"
                )
            lines.append("")
    failing = [c for c in checks if c["failed"]]
    lines += ["## Findings", ""]
    if not failing:
        lines += ["None.", ""]
    for c in failing:
        examples = [f for f in payload["findings"] if f["check_id"] == c["check_id"]]
        lines += [
            f"### `{c['check_id']}` — {c['failed']} finding(s), {c['tier']}",
            "",
            "| Record | Shipment | Detail |",
            "|---|---|---|",
        ]
        for f in examples[:MARKDOWN_EXAMPLES]:
            lines.append(
                f"| `{f['record_id']}` | {f['shipment_id'] or '—'} | {_cell(f['detail'])} |"
            )
        if len(examples) > MARKDOWN_EXAMPLES:
            lines.append(f"| … | | {len(examples) - MARKDOWN_EXAMPLES} more in the JSON report |")
        lines.append("")
    return "\n".join(lines)
