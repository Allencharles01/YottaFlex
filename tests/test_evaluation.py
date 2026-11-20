"""Validation and evaluation on starter data, in memory (Phase 6 acceptance).

* the clean baseline (generator output + the engine's output on it) has **zero** findings in
  every check, none skipped;
* on injected data no integrity check fails, and every business-rule finding is explained by
  an anomaly ("unexplained findings: 0");
* ``evaluate_tables`` scores 100 % at starter, and fails when a metric drops below 100 % (strict
  scale only) or a finding is unexplained (every scale).
"""

import copy

import pandas as pd
import pytest

from coldchain.compliance import check_tables
from coldchain.enums import CheckStatus, CheckTier, Scale, TableName
from coldchain.errors import EvaluationError
from coldchain.evaluation import EVALUATION_INPUTS, evaluate_tables
from coldchain.evaluation.metrics import EXPECTED_FINDINGS
from coldchain.inject import INJECT_INPUTS, inject_tables
from coldchain.io import coerce_to_schema
from coldchain.schemas import SCHEMAS
from coldchain.validation import RAW_INPUTS, validate_tables
from coldchain.validation.checks import CHECKS
from coldchain.validation.summary import summary_payload

T = TableName


@pytest.fixture(scope="module")
def pipeline(app_config, reference, starter_tables) -> dict:
    """Baseline and injected starter data, the engine's output and the validation of each."""
    baseline_raw = {name: starter_tables[name] for name in RAW_INPUTS}
    baseline_engine = check_tables(app_config, baseline_raw)
    injected = inject_tables(app_config, {name: starter_tables[name] for name in INJECT_INPUTS})
    raw = {name: injected[name] for name in RAW_INPUTS}
    engine = check_tables(app_config, raw)
    validation = validate_tables(app_config, reference, {**raw, **engine})
    return {
        "baseline": validate_tables(app_config, reference, {**baseline_raw, **baseline_engine}),
        "validation": validation,
        "payload": summary_payload(validation, scale="starter", source="raw", inputs={}),
        "tables": {name: (engine | raw | injected)[name] for name in EVALUATION_INPUTS},
    }


def _evaluate(app_config, pipeline, *, scale=Scale.STARTER, payload=None, tables=None):
    payload = copy.deepcopy(payload or pipeline["payload"])
    payload["scale"] = scale.value
    return evaluate_tables(app_config, scale, tables or pipeline["tables"], payload)


def test_baseline_has_zero_findings_in_every_check(pipeline) -> None:
    baseline = pipeline["baseline"]
    assert baseline.count(CheckStatus.PASS) == len(CHECKS)
    assert baseline.findings.empty


def test_injected_data_breaks_business_rules_only(pipeline) -> None:
    validation = pipeline["validation"]
    assert validation.passed
    assert set(validation.findings["tier"]) == {CheckTier.BUSINESS_RULE.value}
    expected_checks = set().union(*EXPECTED_FINDINGS.values())
    assert set(validation.findings["check_id"]) == expected_checks


def test_starter_evaluation_is_perfect(app_config, pipeline) -> None:
    result = _evaluate(app_config, pipeline)
    payload = result.payload
    assert result.passed and payload["verdict"] == "PASS" and payload["strict"]
    assert payload["validation"]["unexplained"] == 0
    assert payload["validation"]["explained"] == len(pipeline["validation"].findings) > 0
    assert payload["validation"]["surfaced"] == payload["validation"]["surfaced_of"] == 4
    assert payload["fully_correct_shipments"] == payload["shipments"] == 50
    assert payload["excursions"]["exact_match_rate"] == 1.0
    for row in payload["issue_types"]:
        assert (row["precision"], row["recall"], row["f1"]) == (1.0, 1.0, 1.0), row
    for row in payload["reason_codes"]:
        assert row["support"] > 0, row["reason_code"]  # every code occurs at starter
        assert (row["precision"], row["recall"]) == (1.0, 1.0), row
    assert all(row["accuracy"] == 1.0 for row in payload["cases"])
    assert {row["case"] for row in payload["cases"]} >= {"NORMAL", "injected:logger_not_assigned"}


def test_a_wrong_status_fails_only_at_a_strict_scale(app_config, pipeline) -> None:
    tables = dict(pipeline["tables"])
    decisions = tables[T.COMPLIANCE_DECISIONS].copy()
    natural = decisions["reason_codes"].isna()
    decisions.loc[decisions.index[natural][0], "status"] = "COMPLIANT_WITH_WARNINGS"
    tables[T.COMPLIANCE_DECISIONS] = decisions
    strict = _evaluate(app_config, pipeline, tables=tables)
    assert not strict.passed
    assert any(f.startswith("status accuracy = 0.98") for f in strict.failures), strict.failures
    assert strict.payload["status"]["confusion"]["COMPLIANT"]["COMPLIANT_WITH_WARNINGS"] == 1
    lenient_config = app_config.model_copy(
        update={"evaluation": app_config.evaluation.model_copy(update={"strict_scales": ()})}
    )
    lenient = _evaluate(lenient_config, pipeline, tables=tables)
    assert lenient.passed and not lenient.payload["strict"]
    assert lenient.payload["status"]["accuracy"] == 0.98


def test_starter_and_full_are_strict_by_default(app_config) -> None:
    assert set(app_config.evaluation.strict_scales) == {Scale.STARTER, Scale.FULL}


def test_full_scale_is_strict(app_config, pipeline) -> None:
    tables = dict(pipeline["tables"])
    decisions = tables[T.COMPLIANCE_DECISIONS].copy()
    natural = decisions["reason_codes"].isna()
    decisions.loc[decisions.index[natural][0], "status"] = "COMPLIANT_WITH_WARNINGS"
    tables[T.COMPLIANCE_DECISIONS] = decisions
    result = _evaluate(app_config, pipeline, scale=Scale.FULL, tables=tables)
    assert not result.passed and result.payload["strict"]
    assert any(f.startswith("status accuracy = 0.98") for f in result.failures), result.failures


def test_an_unexplained_finding_fails_at_every_scale(app_config, pipeline) -> None:
    payload = copy.deepcopy(pipeline["payload"])
    shipments = set(pipeline["tables"][T.SHIPMENTS]["shipment_id"])
    natural = sorted(shipments - set(pipeline["tables"][T.ANOMALIES]["shipment_id"]))[0]
    payload["findings"].append(
        {
            "check_id": "rule.custody_events.continuous_chain",
            "tier": "BUSINESS_RULE",
            "table": "custody_events",
            "record_id": f"CUS-{natural}-03",
            "shipment_id": natural,
            "detail": "sequence skips a number",
        }
    )
    for scale in (Scale.STARTER, Scale.FULL):
        result = _evaluate(app_config, pipeline, scale=scale, payload=payload)
        assert result.failures[0] == "1 unexplained validation finding(s)"
        assert result.payload["validation"]["unexplained_findings"][0]["shipment_id"] == natural


def test_an_errored_validation_check_fails(app_config, pipeline) -> None:
    payload = copy.deepcopy(pipeline["payload"])
    payload["totals"]["error"] = 1
    result = _evaluate(app_config, pipeline, scale=Scale.FULL, payload=payload)
    assert result.failures == ("1 validation check(s) errored",)


def test_a_baseline_validation_summary_is_rejected(app_config, pipeline) -> None:
    payload = copy.deepcopy(pipeline["payload"])
    payload["source"] = "baseline"
    with pytest.raises(EvaluationError, match="raw data"):
        evaluate_tables(app_config, Scale.STARTER, pipeline["tables"], payload)


def test_a_missing_input_table_is_rejected(app_config, pipeline) -> None:
    tables = dict(pipeline["tables"])
    del tables[T.LOGGERS]
    with pytest.raises(EvaluationError, match="loggers"):
        evaluate_tables(app_config, Scale.STARTER, tables, pipeline["payload"])


def test_natural_data_without_injection(app_config, reference, starter_tables) -> None:
    raw = {name: starter_tables[name] for name in RAW_INPUTS}
    engine = check_tables(app_config, raw)
    validation = validate_tables(app_config, reference, {**raw, **engine})
    no_anomalies = coerce_to_schema(
        T.ANOMALIES, pd.DataFrame({column: [] for column in SCHEMAS[T.ANOMALIES].columns})
    )
    tables = {
        **raw,
        **engine,
        T.EXPECTED_OUTCOMES: starter_tables[T.EXPECTED_OUTCOMES],
        T.ANOMALIES: no_anomalies,
    }
    payload = summary_payload(validation, scale="starter", source="raw", inputs={})
    inputs = {name: tables[name] for name in EVALUATION_INPUTS}
    result = evaluate_tables(app_config, Scale.STARTER, inputs, payload)
    assert result.passed, result.failures
    assert validation.findings.empty
    for row in result.payload["issue_types"]:
        assert (row["support"], row["predicted"], row["recall"]) == (0, 0, None), row
    assert result.payload["validation"]["surfaced_rate"] is None
