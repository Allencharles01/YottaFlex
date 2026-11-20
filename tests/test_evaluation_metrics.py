"""Evaluation metric functions on tiny, hand-calculated examples (docs/evaluation.md)."""

import pandas as pd
import pytest

from coldchain.enums import IssueType, ReasonCode
from coldchain.evaluation import metrics
from coldchain.schemas import TIMESTAMP_DTYPE
from coldchain.validation.checks import (
    BUSINESS_RULE_CHECKS,
    RULE_ASSIGNED_DEVICE,
    RULE_CARRIER_CLAIM,
    RULE_CUSTODY_CHAIN,
)


def _ts(*values: str) -> pd.Series:
    return pd.Series(pd.to_datetime(list(values), utc=True)).astype(TIMESTAMP_DTYPE)


# --------------------------------------------------------------------------------------------
# Precision / recall / F1
# --------------------------------------------------------------------------------------------


def test_score_hand_calculated() -> None:
    s = metrics.score(tp=2, fp=1, fn=1)  # P = 2/3, R = 2/3, F1 = 4/6
    assert (s.support, s.predicted) == (3, 3)
    assert (s.precision, s.recall, s.f1) == (0.6667, 0.6667, 0.6667)
    s = metrics.score(tp=3, fp=0, fn=1)  # P = 1, R = 3/4, F1 = 6/7
    assert (s.precision, s.recall, s.f1) == (1.0, 0.75, 0.8571)


def test_score_undefined_ratios_are_none() -> None:
    assert metrics.score(0, 0, 0).as_dict() == {
        "support": 0,
        "predicted": 0,
        "tp": 0,
        "fp": 0,
        "fn": 0,
        "precision": None,
        "recall": None,
        "f1": None,
    }
    only_false_alarms = metrics.score(0, 2, 0)
    assert (only_false_alarms.precision, only_false_alarms.recall) == (0.0, None)
    only_misses = metrics.score(0, 0, 3)
    assert (only_misses.precision, only_misses.recall, only_misses.f1) == (None, 0.0, None)
    assert metrics.score(0, 1, 1).f1 == 0.0


def test_score_rejects_negative_counts() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        metrics.score(-1, 0, 0)


def test_set_score_requires_a_correct_outcome_for_a_true_positive() -> None:
    s = metrics.set_score(actual={"a", "b", "c"}, predicted={"b", "c", "d"}, correct={"b", "d"})
    # b: TP; c: predicted and actual but wrong outcome → FP and FN; d: not actual → FP.
    assert (s.tp, s.fp, s.fn) == (1, 2, 2)


def test_reason_code_scores() -> None:
    expected = {"S1": frozenset({"DATA_GAP"}), "S2": frozenset(), "S3": frozenset({"DATA_GAP"})}
    engine = {"S1": frozenset({"DATA_GAP"}), "S2": frozenset({"DATA_GAP", "LONG_DWELL"})}
    scores = metrics.reason_code_scores(expected, engine)
    assert list(scores) == [code.value for code in ReasonCode]
    gap = scores["DATA_GAP"]
    assert (gap.tp, gap.fp, gap.fn) == (1, 1, 1)  # S1 hit, S2 false alarm, S3 missed (no row)
    assert scores["LONG_DWELL"].precision == 0.0 and scores["LONG_DWELL"].recall is None
    assert scores["CUSTODY_GAP"].support == 0 and scores["CUSTODY_GAP"].precision is None


def test_status_confusion() -> None:
    expected = pd.Series(
        ["COMPLIANT", "COMPLIANT", "INCONCLUSIVE", "NON_COMPLIANT"], index=["a", "b", "c", "d"]
    )
    engine = pd.Series(["COMPLIANT", "INCONCLUSIVE", "INCONCLUSIVE"], index=["a", "b", "c"])
    matrix = metrics.status_confusion(expected, engine)
    assert matrix.loc["COMPLIANT", "COMPLIANT"] == 1
    assert matrix.loc["COMPLIANT", "INCONCLUSIVE"] == 1
    assert matrix.loc["INCONCLUSIVE", "INCONCLUSIVE"] == 1
    assert matrix.loc["NON_COMPLIANT", "NO_DECISION"] == 1
    assert int(matrix.to_numpy().sum()) == 4
    assert list(matrix.index)[-1] == "NON_COMPLIANT" and list(matrix.columns)[-1] == "NO_DECISION"


# --------------------------------------------------------------------------------------------
# Excursions
# --------------------------------------------------------------------------------------------


def test_expected_excursions_explodes_windows_and_durations() -> None:
    outcomes = pd.DataFrame(
        {
            "shipment_id": ["S1", "S2"],
            "expected_excursion_windows": [
                "2026-03-10T09:00:00Z/2026-03-10T09:20:00Z|2026-03-10T11:00:00Z/2026-03-10T11:00:00Z",
                None,
            ],
            "expected_excursion_durations_min": ["30|10", None],
        }
    )
    frame = metrics.expected_excursions(outcomes)
    assert frame.to_dict(orient="records") == [
        {
            "shipment_id": "S1",
            "started_at": "2026-03-10T09:00:00Z",
            "ended_at": "2026-03-10T09:20:00Z",
            "duration_min": 30,
        },
        {
            "shipment_id": "S1",
            "started_at": "2026-03-10T11:00:00Z",
            "ended_at": "2026-03-10T11:00:00Z",
            "duration_min": 10,
        },
    ]


def test_excursion_matching_hand_calculated() -> None:
    expected = pd.DataFrame(
        {
            "shipment_id": ["S1", "S1", "S2"],
            "started_at": ["t1", "t3", "t5"],
            "ended_at": ["t2", "t4", "t6"],
            "duration_min": [30, 20, 10],
        }
    )
    engine = pd.DataFrame(
        {
            "shipment_id": ["S1", "S1", "S3"],
            "started_at": ["t1", "t3", "t7"],
            "ended_at": ["t2", "t4", "t8"],
            "duration_min": [30, 40, 10],  # t3 window matches but its duration is wrong
        }
    )
    result = metrics.excursion_matching(expected, engine)
    assert result == {
        "expected": 3,
        "engine": 3,
        "window_matches": 2,
        "exact_matches": 1,
        "missed": 1,
        "spurious": 1,
        "duration_mismatches": 1,
        "exact_match_rate": 0.25,  # 1 exact of 4 distinct excursions (3 + 3 - 2)
    }
    empty = expected.iloc[0:0]
    assert metrics.excursion_matching(empty, empty)["exact_match_rate"] is None


def test_engine_excursions_uses_the_iso_text_of_the_answer_key() -> None:
    reports = pd.DataFrame(
        {
            "shipment_id": pd.Series(["S1"], dtype="str"),
            "started_at": _ts("2026-03-10 09:00"),
            "ended_at": _ts("2026-03-10 09:20"),
            "duration_min": [30],
        }
    )
    assert metrics.engine_excursions(reports).iloc[0].to_dict() == {
        "shipment_id": "S1",
        "started_at": "2026-03-10T09:00:00Z",
        "ended_at": "2026-03-10T09:20:00Z",
        "duration_min": 30,
    }


# --------------------------------------------------------------------------------------------
# Accuracy per case
# --------------------------------------------------------------------------------------------


def test_case_labels_group_injected_shipments_by_issue_type() -> None:
    outcomes = pd.DataFrame(
        {
            "scenario": ["NORMAL", "NORMAL", "BOUNDARY"],
            "issue_type": [None, "custody_gap_not_flagged", None],
        }
    )
    assert metrics.case_labels(outcomes).tolist() == [
        "NORMAL",
        "injected:custody_gap_not_flagged",
        "BOUNDARY",
    ]


def test_case_accuracy() -> None:
    cases = pd.Series(["A", "A", "A", "B"])
    table = metrics.case_accuracy(
        cases,
        status_ok=pd.Series([True, True, False, True]),
        full_ok=pd.Series([True, False, False, True]),
    )
    assert table.loc["A"].to_dict() == {
        "shipments": 3,
        "status_correct": 2,
        "fully_correct": 1,
        "status_accuracy": 0.6667,
        "accuracy": 0.3333,
    }
    assert table.loc["B", "accuracy"] == 1.0


# --------------------------------------------------------------------------------------------
# Detection signatures
# --------------------------------------------------------------------------------------------

POLICIES = pd.DataFrame(
    {
        "policy_id": ["THR-REFRIG_2_8-V1", "THR-REFRIG_2_8-V2", "THR-AMBIENT_15_25-V1"],
        "policy_code": ["REFRIG_2_8", "REFRIG_2_8", "AMBIENT_15_25"],
    }
)


def _decision(shipment: str, codes: str | None, applied: str = "THR-REFRIG_2_8-V1") -> dict:
    return {"shipment_id": shipment, "reason_codes": codes, "applied_policy_id": applied}


def _empty_reports() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "shipment_id": pd.Series(dtype="str"),
            "started_at": pd.Series(dtype=TIMESTAMP_DTYPE),
            "ended_at": pd.Series(dtype=TIMESTAMP_DTYPE),
            "direction": pd.Series(dtype="str"),
            "breach_type": pd.Series(dtype="str"),
        }
    )


def _loggers(rows: list[tuple[str, str, str, str]]) -> pd.DataFrame:
    shipment, device, start, end = zip(*rows, strict=True)
    return pd.DataFrame(
        {
            "shipment_id": list(shipment),
            "device_id": list(device),
            "assigned_at": _ts(*start),
            "unassigned_at": _ts(*end),
        }
    )


def test_reason_code_signatures() -> None:
    decisions = pd.DataFrame(
        [
            _decision(
                "S1", "LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE|CARRIER_STATUS_CONTRADICTED"
            ),
            _decision("S2", "POLICY_MISMATCH_DECLARED|CARRIER_STATUS_CONTRADICTED"),
            _decision("S3", "POLICY_MISMATCH_DECLARED", applied="THR-REFRIG_2_8-V2"),
            _decision("S4", "DATA_GAP|CARRIER_STATUS_CONTRADICTED"),
            _decision("S5", "CUSTODY_GAP|CARRIER_STATUS_CONTRADICTED"),
            _decision("S6", "DATA_GAP"),  # carrier already said FAIL: not "silently passed"
            _decision("S7", None),
        ]
    )
    shipments = pd.DataFrame(
        {
            "shipment_id": [f"S{i}" for i in range(1, 8)],
            "declared_policy_id": [
                "THR-REFRIG_2_8-V1",
                "THR-AMBIENT_15_25-V1",  # S2: another class → product mismatch
                "THR-REFRIG_2_8-V1",  # S3: version only (V1 declared, V2 applied) → natural
                *["THR-REFRIG_2_8-V1"] * 4,
            ],
        }
    )
    loggers = _loggers([("S1", "D1", "2026-03-01 00:00", "2026-03-01 10:00")])
    predicted = metrics.predicted_issue_types(
        decisions, _empty_reports(), shipments, loggers, POLICIES
    )
    assert sorted(zip(predicted["shipment_id"], predicted["issue_type"], strict=True)) == [
        ("S1", IssueType.LOGGER_NOT_ASSIGNED.value),
        ("S2", IssueType.PRODUCT_THRESHOLD_MISMATCH.value),
        ("S4", IssueType.MISSING_DATA_SILENTLY_PASSED.value),
        ("S5", IssueType.CUSTODY_GAP_NOT_FLAGGED.value),
    ]


def _edge_reports(rows: list[tuple[str, str, str, str, str]]) -> pd.DataFrame:
    shipment, start, end, direction, breach = zip(*rows, strict=True)
    return pd.DataFrame(
        {
            "shipment_id": list(shipment),
            "started_at": _ts(*start),
            "ended_at": _ts(*end),
            "direction": list(direction),
            "breach_type": list(breach),
        }
    )


LOGGERS = _loggers(
    [
        ("A", "D1", "2026-03-01 00:00", "2026-03-01 10:00"),
        ("B", "D1", "2026-03-01 11:00", "2026-03-01 20:00"),
        ("C", "D1", "2026-03-02 00:00", "2026-03-02 10:00"),
        ("X", "D2", "2026-03-01 11:00", "2026-03-01 20:00"),
    ]
)


def test_back_to_back_pair_within_allowance_is_the_trap() -> None:
    reports = _edge_reports(
        [
            ("A", "2026-03-01 09:50", "2026-03-01 10:00", "HIGH", "WITHIN_ALLOWANCE"),
            ("B", "2026-03-01 11:00", "2026-03-01 11:20", "HIGH", "WITHIN_ALLOWANCE"),
        ]
    )
    pairs = metrics.back_to_back_edge_pairs(reports, LOGGERS)
    assert pairs.to_dict(orient="records") == [
        {
            "device_id": "D1",
            "first_shipment_id": "A",
            "second_shipment_id": "B",
            "direction": "HIGH",
        }
    ]


@pytest.mark.parametrize(
    ("rows", "why"),
    [
        (
            [
                ("A", "2026-03-01 09:50", "2026-03-01 10:00", "HIGH", "WITHIN_ALLOWANCE"),
                ("B", "2026-03-01 11:00", "2026-03-01 11:20", "LOW", "WITHIN_ALLOWANCE"),
            ],
            "opposite directions do not merge",
        ),
        (
            [
                ("A", "2026-03-01 09:40", "2026-03-01 09:50", "HIGH", "WITHIN_ALLOWANCE"),
                ("B", "2026-03-01 11:00", "2026-03-01 11:20", "HIGH", "WITHIN_ALLOWANCE"),
            ],
            "A's excursion ends before its last instant",
        ),
        (
            [
                ("A", "2026-03-01 09:50", "2026-03-01 10:00", "HIGH", "WITHIN_ALLOWANCE"),
                ("X", "2026-03-01 11:00", "2026-03-01 11:20", "HIGH", "WITHIN_ALLOWANCE"),
            ],
            "X is on another device",
        ),
        (
            [
                ("A", "2026-03-01 09:50", "2026-03-01 10:00", "HIGH", "WITHIN_ALLOWANCE"),
                ("C", "2026-03-02 00:00", "2026-03-02 00:20", "HIGH", "WITHIN_ALLOWANCE"),
            ],
            "C is not A's next assignment",
        ),
        (
            [
                ("A", "2026-03-01 00:00", "2026-03-01 10:00", "HIGH", "SINGLE_DURATION_BREACH"),
                ("B", "2026-03-01 11:00", "2026-03-01 20:00", "HIGH", "SINGLE_DURATION_BREACH"),
            ],
            "two whole-trip breaches (e.g. two product mismatches) merge into no new conclusion",
        ),
    ],
)
def test_back_to_back_pair_negatives(rows, why) -> None:
    assert metrics.back_to_back_edge_pairs(_edge_reports(rows), LOGGERS).empty, why


# --------------------------------------------------------------------------------------------
# Validation findings vs anomalies
# --------------------------------------------------------------------------------------------

ANOMALIES = pd.DataFrame(
    {
        "anomaly_id": ["ANOM-000001", "ANOM-000002", "ANOM-000002"],
        "shipment_id": ["S1", "S2", "S3"],
        "issue_type": [
            IssueType.LOGGER_NOT_ASSIGNED.value,
            IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value,
            IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value,
        ],
    }
)


def test_expected_findings_are_business_rule_checks() -> None:
    assert set(metrics.EXPECTED_FINDINGS) == set(IssueType)
    for checks in metrics.EXPECTED_FINDINGS.values():
        assert checks <= BUSINESS_RULE_CHECKS


def test_explain_findings() -> None:
    findings = pd.DataFrame(
        {
            "check_id": [
                RULE_ASSIGNED_DEVICE,
                RULE_CUSTODY_CHAIN,
                "pk.shipments",
                RULE_CARRIER_CLAIM,
            ],
            "shipment_id": ["S1", "S1", "S1", "S9"],
            "record_id": ["r1", "r2", "r3", "r4"],
        }
    )
    explained = metrics.explain_findings(findings, ANOMALIES)
    assert explained["explained"].tolist() == [True, False, False, False]
    assert explained["anomaly_id"].tolist() == ["ANOM-000001", None, None, None]


def test_surfaced_anomalies() -> None:
    findings = pd.DataFrame({"check_id": [RULE_ASSIGNED_DEVICE], "shipment_id": ["S1"]})
    surfaced = metrics.surfaced_anomalies(ANOMALIES, findings)
    first = surfaced.iloc[0]
    assert first["expected_checks"] == sorted({RULE_ASSIGNED_DEVICE, RULE_CARRIER_CLAIM})
    assert first["surfaced"] is False  # the carrier-claim finding is missing
    assert surfaced["surfaced"].tolist()[1:] == [None, None]  # the pair breaks no rule
