"""Evaluation (``coldchain evaluate``): the engine's output against the answer key.

:func:`evaluate_tables` compares the compliance decisions and excursion reports with
``ground_truth/expected_outcomes`` and ``anomalies``, and cross-references the validation
findings with the injected anomalies. Unlike the engine and the validation suite, this stage
*may* read the answer key: that is its job. Metric definitions: ``docs/evaluation.md``.

The run fails when any validation finding is unexplained (or a validation check errored), at
every scale. At a *strict* scale (``evaluation.strict_scales`` in the config, ``starter`` and
``full`` by default) it also fails when any defined metric is below 100 %.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import pandas as pd

from coldchain import __version__
from coldchain.config import AppConfig
from coldchain.enums import ComplianceStatus, IssueType, Scale, TableName
from coldchain.errors import EvaluationError
from coldchain.evaluation import metrics
from coldchain.schemas import split_list

logger = logging.getLogger(__name__)

EVALUATION_INPUTS: tuple[TableName, ...] = (
    TableName.EXPECTED_OUTCOMES,
    TableName.ANOMALIES,
    TableName.COMPLIANCE_DECISIONS,
    TableName.EXCURSION_REPORTS,
    TableName.SHIPMENTS,
    TableName.LOGGERS,
    TableName.THRESHOLD_POLICIES,
)
"""Tables the evaluation reads (answer key, engine output, and three raw tables)."""

REPORT_STEM = "evaluation_report"
"""File stem of the evaluation reports."""

_FINDING_COLUMNS = ("check_id", "tier", "table", "record_id", "shipment_id", "detail")

_ANOMALY_COLUMNS = (
    "anomaly_id",
    "shipment_id",
    "issue_type",
    "predicted",
    "correct",
    "expected_status",
    "engine_status",
    "expected_reason_codes",
    "engine_reason_codes",
    "expected_durations_min",
    "engine_durations_min",
)


@dataclass(frozen=True)
class EvaluationResult:
    """The evaluation report and its verdict.

    Attributes:
        payload: JSON-serialisable report content (rendered by :mod:`.report`).
        failures: One line per reason the run fails (empty when it passes).
    """

    payload: dict[str, Any]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        """``True`` when there is no failure."""
        return not self.failures


def _compare_shipments(
    outcomes: pd.DataFrame, decisions: pd.DataFrame, reports: pd.DataFrame
) -> pd.DataFrame:
    """One row per expected shipment with expected and engine facts and the match flags."""
    engine = decisions.set_index("shipment_id")
    frame = outcomes.set_index("shipment_id")
    expected_exc = metrics.excursion_signatures(metrics.expected_excursions(outcomes))
    engine_exc = metrics.excursion_signatures(metrics.engine_excursions(reports))
    compared = pd.DataFrame(index=frame.index)
    compared["case"] = metrics.case_labels(outcomes).to_numpy()
    compared["expected_status"] = frame["expected_status"]
    compared["engine_status"] = engine["status"].reindex(frame.index)
    compared["expected_codes"] = metrics.code_sets(frame["expected_reason_codes"])
    compared["engine_codes"] = metrics.code_sets(engine["reason_codes"].reindex(frame.index))
    empty = pd.Series([()] * len(frame), index=frame.index, dtype=object)
    compared["expected_excursions"] = expected_exc.reindex(frame.index).fillna(empty)
    compared["engine_excursions"] = engine_exc.reindex(frame.index).fillna(empty)
    compared["status_ok"] = compared["engine_status"] == compared["expected_status"]
    action_ok = engine["decided_action"].reindex(frame.index) == frame["expected_action"]
    codes_ok = compared["engine_codes"] == compared["expected_codes"]
    excursions_ok = compared["engine_excursions"] == compared["expected_excursions"]
    compared["full_ok"] = compared["status_ok"] & action_ok & codes_ok & excursions_ok
    return compared


def _anomaly_rows(
    anomalies: pd.DataFrame,
    compared: pd.DataFrame,
    reports: pd.DataFrame,
    predicted: pd.DataFrame,
) -> pd.DataFrame:
    """One row per anomaly row: was its type predicted, and is the outcome exactly right?

    *Correct* means the engine's status equals the anomaly's ``expected_status``, its reason
    codes equal ``expected_reason_codes`` (as sets), and its excursion durations equal
    ``expected_excursion_durations_min`` (as sorted lists).
    """
    durations = reports.groupby("shipment_id")["duration_min"].agg(
        lambda s: tuple(sorted(int(v) for v in s))
    )
    flagged = set(zip(predicted["shipment_id"], predicted["issue_type"], strict=True))
    rows = []
    for row in anomalies.itertuples(index=False):
        shipment = row.shipment_id
        engine_status = compared["engine_status"].get(shipment)
        engine_codes = compared["engine_codes"].get(shipment, frozenset())
        expected_codes = frozenset(split_list(row.expected_reason_codes))
        expected_durations = tuple(
            sorted(int(d) for d in split_list(row.expected_excursion_durations_min))
        )
        engine_durations = durations.get(shipment, ())
        rows.append(
            {
                "anomaly_id": row.anomaly_id,
                "shipment_id": shipment,
                "issue_type": row.issue_type,
                "predicted": (shipment, row.issue_type) in flagged,
                "correct": bool(
                    engine_status == row.expected_status
                    and engine_codes == expected_codes
                    and engine_durations == expected_durations
                ),
                "expected_status": row.expected_status,
                "engine_status": engine_status,
                "expected_reason_codes": sorted(expected_codes),
                "engine_reason_codes": sorted(engine_codes),
                "expected_durations_min": list(expected_durations),
                "engine_durations_min": list(engine_durations),
            }
        )
    return pd.DataFrame(rows, columns=list(_ANOMALY_COLUMNS))


def _issue_scores(anomaly_rows: pd.DataFrame, predicted: pd.DataFrame) -> list[dict[str, Any]]:
    """Precision / recall / F1 per issue type (docs/evaluation.md §2.1)."""
    result = []
    for issue in IssueType:
        mine = anomaly_rows[anomaly_rows["issue_type"] == issue.value]
        flagged = predicted.loc[predicted["issue_type"] == issue.value, "shipment_id"]
        correct = mine.loc[mine["correct"], "shipment_id"]
        result.append(
            {
                "issue_type": issue.value,
                **metrics.set_score(mine["shipment_id"], flagged, correct).as_dict(),
            }
        )
    return result


def _findings_frame(validation: Mapping[str, Any]) -> pd.DataFrame:
    """The validation findings as a frame (empty frame with the right columns if none)."""
    records = validation.get("findings", [])
    return pd.DataFrame.from_records(records, columns=list(_FINDING_COLUMNS))


def _strict_failures(payload: Mapping[str, Any]) -> list[str]:
    """Every defined metric below 100 % (only enforced at a strict scale)."""
    failures = []

    def require(label: str, value: float | None) -> None:
        if value is not None and value < 1.0:
            failures.append(f"{label} = {value:.4f} < 1.0")

    for row in payload["issue_types"]:
        for name in ("precision", "recall", "f1"):
            require(f"issue type {row['issue_type']} {name}", row[name])
    for row in payload["reason_codes"]:
        for name in ("precision", "recall", "f1"):
            require(f"reason code {row['reason_code']} {name}", row[name])
    require("status accuracy", payload["status"]["accuracy"])
    require("excursion duration exact-match rate", payload["excursions"]["exact_match_rate"])
    for row in payload["cases"]:
        require(f"case {row['case']} accuracy", row["accuracy"])
    require("anomalies surfaced by validation", payload["validation"]["surfaced_rate"])
    return failures


def evaluate_tables(
    config: AppConfig,
    scale: Scale,
    tables: Mapping[TableName, pd.DataFrame],
    validation: Mapping[str, Any],
) -> EvaluationResult:
    """Evaluate the engine against the answer key and explain the validation findings.

    Args:
        config: Pipeline configuration (``evaluation.strict_scales``).
        scale: The scale being evaluated.
        tables: The tables in :data:`EVALUATION_INPUTS` (``anomalies`` may be empty).
        validation: The ``validation_summary.json`` payload of the same data.

    Returns:
        The report payload and its failures.

    Raises:
        EvaluationError: If an input table is missing or the validation summary is not of the
            raw data.
    """
    missing = [name.value for name in EVALUATION_INPUTS if name not in tables]
    if missing:
        raise EvaluationError(f"Missing evaluation input tables: {missing}")
    if validation.get("source") != "raw" or validation.get("scale") != scale.value:
        raise EvaluationError(
            "The validation summary must be of the raw data at this scale "
            f"(got source={validation.get('source')!r}, scale={validation.get('scale')!r})"
        )
    outcomes = tables[TableName.EXPECTED_OUTCOMES]
    anomalies = tables[TableName.ANOMALIES]
    decisions = tables[TableName.COMPLIANCE_DECISIONS]
    reports = tables[TableName.EXCURSION_REPORTS]
    strict = scale in config.evaluation.strict_scales

    compared = _compare_shipments(outcomes, decisions, reports)
    predicted = metrics.predicted_issue_types(
        decisions,
        reports,
        tables[TableName.SHIPMENTS],
        tables[TableName.LOGGERS],
        tables[TableName.THRESHOLD_POLICIES],
    )
    anomaly_rows = _anomaly_rows(anomalies, compared, reports, predicted)
    confusion = metrics.status_confusion(
        compared["expected_status"], compared["engine_status"].dropna()
    )
    statuses = [s.value for s in ComplianceStatus]
    cases = metrics.case_accuracy(compared["case"], compared["status_ok"], compared["full_ok"])
    findings = metrics.explain_findings(_findings_frame(validation), anomalies)
    surfaced = metrics.surfaced_anomalies(anomalies, findings)
    applicable = surfaced["surfaced"].dropna()
    unexplained = findings[~findings["explained"]]

    payload: dict[str, Any] = {
        "report": REPORT_STEM,
        "package_version": __version__,
        "scale": scale.value,
        "strict": strict,
        "verdict": "",
        "failures": [],
        "shipments": len(compared),
        "fully_correct_shipments": int(compared["full_ok"].sum()),
        "injected_shipments": int(outcomes["issue_type"].notna().sum()),
        "anomalies": int(anomalies["anomaly_id"].nunique()),
        "issue_types": _issue_scores(anomaly_rows, predicted),
        "anomaly_detections": anomaly_rows.to_dict(orient="records"),
        "reason_codes": [
            {"reason_code": code, **s.as_dict()}
            for code, s in metrics.reason_code_scores(
                compared["expected_codes"].to_dict(), compared["engine_codes"].to_dict()
            ).items()
        ],
        "status": {
            "labels": statuses,
            "confusion": {
                status: {column: int(confusion.loc[status, column]) for column in confusion.columns}
                for status in statuses
            },
            "correct": int(compared["status_ok"].sum()),
            "accuracy": metrics.ratio(int(compared["status_ok"].sum()), len(compared)),
        },
        "excursions": metrics.excursion_matching(
            metrics.expected_excursions(outcomes), metrics.engine_excursions(reports)
        ),
        "cases": [{"case": case, **row} for case, row in cases.to_dict(orient="index").items()],
        "validation": {
            "status": validation.get("status"),
            "checks_errored": int(validation.get("totals", {}).get("error", 0)),
            "findings": len(findings),
            "explained": int(findings["explained"].sum()),
            "unexplained": len(unexplained),
            "unexplained_findings": unexplained[list(_FINDING_COLUMNS)].to_dict(orient="records"),
            "explained_by_check": {
                check: int(n)
                for check, n in findings[findings["explained"]].groupby("check_id").size().items()
            },
            "surfaced": int(applicable.sum()),
            "surfaced_of": len(applicable),
            "surfaced_rate": metrics.ratio(int(applicable.sum()), len(applicable)),
            "anomalies": surfaced.to_dict(orient="records"),
        },
    }
    failures = []
    if len(unexplained):
        failures.append(f"{len(unexplained)} unexplained validation finding(s)")
    if payload["validation"]["checks_errored"]:
        failures.append(f"{payload['validation']['checks_errored']} validation check(s) errored")
    if strict:
        failures += _strict_failures(payload)
    payload["verdict"] = "FAIL" if failures else "PASS"
    payload["failures"] = failures
    logger.info(
        "Evaluated %d shipments (%d fully correct), %d anomalies; %d/%d findings explained; %s",
        len(compared),
        payload["fully_correct_shipments"],
        payload["anomalies"],
        payload["validation"]["explained"],
        len(findings),
        payload["verdict"],
    )
    return EvaluationResult(payload, tuple(failures))
