"""Render an evaluation payload as ``evaluation_report.md`` (the JSON is the payload itself)."""

from collections.abc import Mapping
from typing import Any


def _pct(value: float | None) -> str:
    """A ratio as a percentage with 2 dp, or ``n/a``."""
    return "n/a" if value is None else f"{100 * value:.2f} %"


def _cell(value: Any) -> str:
    """Markdown table cell text (pipes escaped)."""
    return str(value).replace("|", "\\|")


def _score_table(rows: list[Mapping[str, Any]], key: str, title: str) -> list[str]:
    lines = [
        f"| {title} | Support | Predicted | TP | FP | FN | Precision | Recall | F1 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| `{row[key]}` | {row['support']} | {row['predicted']} | {row['tp']} | {row['fp']} "
            f"| {row['fn']} | {_pct(row['precision'])} | {_pct(row['recall'])} "
            f"| {_pct(row['f1'])} |"
        )
    return [*lines, ""]


def report_markdown(payload: Mapping[str, Any]) -> str:
    """Render the evaluation payload as Markdown.

    Args:
        payload: The ``payload`` of an :class:`~coldchain.evaluation.EvaluationResult`.

    Returns:
        The Markdown text (ends with a newline).
    """
    validation = payload["validation"]
    lines = [
        "# Evaluation report",
        "",
        f"- Scale: `{payload['scale']}` · package {payload['package_version']} · strict: "
        f"{'yes (every defined metric must be 100 %)' if payload['strict'] else 'no'}",
        f"- **Verdict: {payload['verdict']}**",
        f"- Shipments: {payload['shipments']} "
        f"({payload['fully_correct_shipments']} fully correct); "
        f"injected: {payload['injected_shipments']} shipment(s), {payload['anomalies']} anomalies",
        f"- **Unexplained validation findings: {validation['unexplained']}** "
        f"({validation['explained']} of {validation['findings']} explained by an anomaly)",
        "",
        "Metric definitions: [`docs/evaluation.md`](../../../docs/evaluation.md).",
        "",
    ]
    if payload["failures"]:
        lines += ["## Failures", "", *[f"- {_cell(f)}" for f in payload["failures"]], ""]

    lines += [
        "## Detection per issue type",
        "",
        "A detection is a true positive when the engine's output carries the issue type's "
        "signature **and** the shipment's status, reason codes and excursion durations equal "
        "the anomaly's expected values.",
        "",
        *_score_table(payload["issue_types"], "issue_type", "Issue type"),
        "| Anomaly | Shipment | Issue type | Signature | Outcome | Expected → engine status |",
        "|---|---|---|---|---|---|",
    ]
    for row in payload["anomaly_detections"]:
        lines.append(
            f"| `{row['anomaly_id']}` | {row['shipment_id']} | `{row['issue_type']}` | "
            f"{'fired' if row['predicted'] else 'missed'} | "
            f"{'correct' if row['correct'] else 'wrong'} | "
            f"{row['expected_status']} → {row['engine_status']} |"
        )
    lines += ["", "## Detection per reason code", ""]
    lines += _score_table(payload["reason_codes"], "reason_code", "Reason code")

    status = payload["status"]
    labels = status["labels"]
    columns = list(next(iter(status["confusion"].values())))
    lines += [
        "## Status confusion matrix",
        "",
        f"Rows: expected status; columns: engine status. Accuracy {_pct(status['accuracy'])} "
        f"({status['correct']} of {payload['shipments']}).",
        "",
        "| expected \\ engine | " + " | ".join(columns) + " |",
        "|---|" + "---:|" * len(columns),
    ]
    for label in labels:
        row = status["confusion"][label]
        lines.append(f"| {label} | " + " | ".join(str(row[c]) for c in columns) + " |")

    exc = payload["excursions"]
    lines += [
        "",
        "## Excursion durations",
        "",
        f"- Expected excursions: {exc['expected']}; engine reports: {exc['engine']}",
        f"- Window matches (shipment, start, end): {exc['window_matches']}; "
        f"exact matches (also duration): {exc['exact_matches']}",
        f"- Missed: {exc['missed']}; spurious: {exc['spurious']}; "
        f"duration mismatches: {exc['duration_mismatches']}",
        f"- **Exact-match rate: {_pct(exc['exact_match_rate'])}**",
        "",
        "## Accuracy per scenario",
        "",
        "Injected shipments are grouped by issue type; the rest by natural scenario. *Fully "
        "correct* = status, action, reason codes and every excursion window and duration match.",
        "",
        "| Case | Shipments | Status correct | Fully correct | Status accuracy | Accuracy |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in payload["cases"]:
        lines.append(
            f"| `{row['case']}` | {row['shipments']} | {row['status_correct']} | "
            f"{row['fully_correct']} | {_pct(row['status_accuracy'])} | {_pct(row['accuracy'])} |"
        )
    lines += [
        "",
        "## Validation findings vs anomalies",
        "",
        f"- Validation status: {validation['status']}; checks errored: "
        f"{validation['checks_errored']}",
        f"- Findings: {validation['findings']}; explained: {validation['explained']}; "
        f"**unexplained: {validation['unexplained']}**",
        f"- Anomalies surfaced by validation (every expected check fired): "
        f"{validation['surfaced']} of {validation['surfaced_of']} "
        f"({_pct(validation['surfaced_rate'])})",
        "",
        "| Anomaly | Shipment | Issue type | Expected checks | Surfaced |",
        "|---|---|---|---|---|",
    ]
    for row in validation["anomalies"]:
        expected = (
            ", ".join(f"`{c}`" for c in row["expected_checks"]) or "— (none: a trap for the engine)"
        )
        surfaced = {True: "yes", False: "**no**", None: "n/a"}[row["surfaced"]]
        lines.append(
            f"| `{row['anomaly_id']}` | {row['shipment_id']} | `{row['issue_type']}` | "
            f"{expected} | {surfaced} |"
        )
    lines += ["", "### Unexplained findings", ""]
    if not validation["unexplained_findings"]:
        lines += ["None.", ""]
    else:
        lines += ["| Check | Record | Shipment | Detail |", "|---|---|---|---|"]
        for f in validation["unexplained_findings"]:
            lines.append(
                f"| `{f['check_id']}` | `{f['record_id']}` | {f['shipment_id'] or '—'} | "
                f"{_cell(f['detail'])} |"
            )
        lines.append("")
    return "\n".join(lines)
