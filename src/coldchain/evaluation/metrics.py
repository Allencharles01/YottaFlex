"""Metric functions: compare the engine's output with the answer key (``docs/evaluation.md``).

Every function is pure (frames in, frames or numbers out) and small enough to check by hand.
Undefined ratios (e.g. precision with no prediction) are ``None``, never 0 or 1, so a metric
that does not apply is never mistaken for a perfect or a failed one.
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass

import pandas as pd

from coldchain.enums import BreachType, ComplianceStatus, IssueType, ReasonCode
from coldchain.schemas import ISO_TIMESTAMP_FORMAT, split_list
from coldchain.validation.checks import (
    RULE_ASSIGNED_DEVICE,
    RULE_CARRIER_CLAIM,
    RULE_COMPLETE_STREAM,
    RULE_CUSTODY_CHAIN,
    RULE_CUSTODY_COVERAGE,
    RULE_DECLARED_POLICY,
)

EXPECTED_FINDINGS: dict[IssueType, frozenset[str]] = {
    IssueType.LOGGER_NOT_ASSIGNED: frozenset({RULE_ASSIGNED_DEVICE, RULE_CARRIER_CLAIM}),
    IssueType.PRODUCT_THRESHOLD_MISMATCH: frozenset({RULE_DECLARED_POLICY, RULE_CARRIER_CLAIM}),
    IssueType.MISSING_DATA_SILENTLY_PASSED: frozenset({RULE_COMPLETE_STREAM, RULE_CARRIER_CLAIM}),
    IssueType.CUSTODY_GAP_NOT_FLAGGED: frozenset(
        {RULE_CUSTODY_CHAIN, RULE_CUSTODY_COVERAGE, RULE_CARRIER_CLAIM}
    ),
    IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS: frozenset(),
}
"""The validation checks each issue type breaks on its target shipments (docs/evaluation.md §3).

A finding is *explained* only by an anomaly on the same shipment whose type lists the check.
The back-to-back excursion pair is a trap for the engine, not a data defect: it breaks no rule.
"""


# --------------------------------------------------------------------------------------------
# Precision / recall / F1
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Score:
    """Detection counts and the ratios derived from them.

    Attributes:
        support: Actual positives (``tp + fn``).
        predicted: Predicted positives (``tp + fp``).
        tp: True positives.
        fp: False positives.
        fn: False negatives.
        precision: ``tp / predicted``, or ``None`` when nothing was predicted.
        recall: ``tp / support``, or ``None`` when there is nothing to find.
        f1: Harmonic mean of precision and recall, or ``None`` if either is undefined.
    """

    support: int
    predicted: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None

    def as_dict(self) -> dict[str, int | float | None]:
        """Return the fields as a plain dict (for JSON)."""
        return asdict(self)


def ratio(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator`` rounded to 4 dp, or ``None`` when the denominator is 0."""
    return round(numerator / denominator, 4) if denominator else None


def score(tp: int, fp: int, fn: int) -> Score:
    """Compute precision, recall and F1 from detection counts.

    Args:
        tp: True positives.
        fp: False positives.
        fn: False negatives.

    Returns:
        The :class:`Score`.

    Raises:
        ValueError: If a count is negative.
    """
    if min(tp, fp, fn) < 0:
        raise ValueError(f"counts must be >= 0, got tp={tp} fp={fp} fn={fn}")
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    if precision is None or recall is None:
        f1 = None
    elif precision + recall == 0:
        f1 = 0.0
    else:
        f1 = round(2 * tp / (2 * tp + fp + fn), 4)
    return Score(tp + fn, tp + fp, tp, fp, fn, precision, recall, f1)


def set_score(actual: Iterable[str], predicted: Iterable[str], correct: Iterable[str]) -> Score:
    """Score a detector from the sets of actual, predicted and correctly handled items.

    A true positive is predicted, actual **and** correct; every other prediction is a false
    positive, and every other actual item a false negative.

    Args:
        actual: Items that really have the property.
        predicted: Items the detector flagged.
        correct: Items whose detection is counted as right (e.g. outcome matches).

    Returns:
        The :class:`Score`.
    """
    actual, predicted = set(actual), set(predicted)
    tp = len(actual & predicted & set(correct))
    return score(tp, len(predicted) - tp, len(actual) - tp)


# --------------------------------------------------------------------------------------------
# Reason codes and statuses
# --------------------------------------------------------------------------------------------


def code_sets(values: pd.Series) -> pd.Series:
    """Turn ``|``-lists of reason codes into frozensets (null → empty set)."""
    return values.map(lambda value: frozenset(split_list(value)))


def reason_code_scores(
    expected: Mapping[str, frozenset[str]], engine: Mapping[str, frozenset[str]]
) -> dict[str, Score]:
    """Precision, recall and F1 of every reason code over shipments.

    For a code *c*, a shipment is an actual positive when the answer key lists *c*, and a
    predicted positive when the engine does; every agreement is a true positive.

    Args:
        expected: Shipment → expected reason codes (the answer key).
        engine: Shipment → the engine's reason codes (a missing shipment has none).

    Returns:
        Code → :class:`Score`, in :class:`ReasonCode` order.
    """
    shipments = sorted(set(expected) | set(engine))
    result = {}
    for code in ReasonCode:
        actual = {s for s in shipments if code.value in expected.get(s, frozenset())}
        predicted = {s for s in shipments if code.value in engine.get(s, frozenset())}
        result[code.value] = set_score(actual, predicted, predicted)
    return result


def status_confusion(expected: pd.Series, engine: pd.Series) -> pd.DataFrame:
    """Confusion matrix of the expected status (rows) against the engine's (columns).

    Args:
        expected: Expected status per shipment (indexed by shipment).
        engine: The engine's status per shipment; a shipment missing here is counted in the
            ``NO_DECISION`` column.

    Returns:
        Integer frame with one row per status and one column per status plus ``NO_DECISION``.
    """
    statuses = [s.value for s in ComplianceStatus]
    columns = [*statuses, "NO_DECISION"]
    actual = engine.reindex(expected.index).fillna("NO_DECISION")
    matrix = pd.crosstab(expected, actual).reindex(index=statuses, columns=columns, fill_value=0)
    matrix.index.name, matrix.columns.name = "expected", "engine"
    return matrix.astype("int64")


# --------------------------------------------------------------------------------------------
# Excursions
# --------------------------------------------------------------------------------------------

EXCURSION_KEY = ("shipment_id", "started_at", "ended_at")
"""An excursion is identified by its shipment and its first and last reading times."""


def expected_excursions(outcomes: pd.DataFrame) -> pd.DataFrame:
    """Explode the answer key's excursion windows and durations into one row per excursion.

    Args:
        outcomes: ``expected_outcomes`` rows (``shipment_id``, ``expected_excursion_windows``,
            ``expected_excursion_durations_min``).

    Returns:
        ``shipment_id``, ``started_at``, ``ended_at`` (ISO-8601 text) and ``duration_min``.
    """
    rows = []
    for shipment, windows, durations in zip(
        outcomes["shipment_id"],
        outcomes["expected_excursion_windows"],
        outcomes["expected_excursion_durations_min"],
        strict=True,
    ):
        for window, duration in zip(split_list(windows), split_list(durations), strict=True):
            start, _, end = window.partition("/")
            rows.append((shipment, start, end, int(duration)))
    return pd.DataFrame(rows, columns=[*EXCURSION_KEY, "duration_min"])


def engine_excursions(reports: pd.DataFrame) -> pd.DataFrame:
    """The engine's excursion reports in the same shape as :func:`expected_excursions`."""
    return pd.DataFrame(
        {
            "shipment_id": reports["shipment_id"].astype(str),
            "started_at": reports["started_at"].dt.strftime(ISO_TIMESTAMP_FORMAT),
            "ended_at": reports["ended_at"].dt.strftime(ISO_TIMESTAMP_FORMAT),
            "duration_min": reports["duration_min"].astype("int64"),
        }
    )


def excursion_matching(
    expected: pd.DataFrame, engine: pd.DataFrame
) -> dict[str, int | float | None]:
    """Match expected and engine excursions by window, then compare their durations.

    A pair *matches* when shipment, ``started_at`` and ``ended_at`` are equal; it is an
    *exact match* when ``duration_min`` is equal too. The exact-match rate is
    ``exact / (expected + engine - matched)``: the share of all distinct excursions (either
    side) that both sides report identically, so misses and spurious reports both lower it.

    Args:
        expected: Output of :func:`expected_excursions`.
        engine: Output of :func:`engine_excursions`.

    Returns:
        Counts (``expected``, ``engine``, ``window_matches``, ``exact_matches``, ``missed``,
        ``spurious``, ``duration_mismatches``) and ``exact_match_rate`` (``None`` if both sides
        are empty).
    """
    merged = expected.merge(engine, on=list(EXCURSION_KEY), suffixes=("_expected", "_engine"))
    matched = len(merged)
    exact = int((merged["duration_min_expected"] == merged["duration_min_engine"]).sum())
    union = len(expected) + len(engine) - matched
    return {
        "expected": len(expected),
        "engine": len(engine),
        "window_matches": matched,
        "exact_matches": exact,
        "missed": len(expected) - matched,
        "spurious": len(engine) - matched,
        "duration_mismatches": matched - exact,
        "exact_match_rate": ratio(exact, union),
    }


def excursion_signatures(excursions: pd.DataFrame) -> pd.Series:
    """Per shipment, the sorted tuple of its excursions ``(start, end, duration)``."""
    if excursions.empty:
        return pd.Series(dtype=object)
    rows = excursions.sort_values(list(EXCURSION_KEY), kind="stable")
    keyed = list(zip(rows["started_at"], rows["ended_at"], rows["duration_min"], strict=True))
    return pd.Series(keyed, index=rows["shipment_id"].to_numpy()).groupby(level=0).agg(tuple)


def duration_signatures(durations: pd.Series) -> pd.Series:
    """Turn ``|``-lists of durations into sorted tuples of ints (null → empty tuple)."""
    return durations.map(lambda value: tuple(sorted(int(d) for d in split_list(value))))


# --------------------------------------------------------------------------------------------
# Accuracy per scenario
# --------------------------------------------------------------------------------------------


def case_labels(outcomes: pd.DataFrame) -> pd.Series:
    """The evaluation case of each shipment: its issue type if injected, else its scenario."""
    injected = outcomes["issue_type"].notna()
    return outcomes["scenario"].astype(str).where(~injected, "injected:" + outcomes["issue_type"])


def case_accuracy(cases: pd.Series, status_ok: pd.Series, full_ok: pd.Series) -> pd.DataFrame:
    """Accuracy per evaluation case.

    Args:
        cases: Case label per shipment.
        status_ok: Whether the engine's status equals the expected status.
        full_ok: Whether status, reason codes and every excursion (window and duration) match.

    Returns:
        One row per case (sorted): ``shipments``, ``status_correct``, ``fully_correct``,
        ``status_accuracy`` and ``accuracy`` (= fully correct / shipments).
    """
    frame = pd.DataFrame({"case": cases, "status_ok": status_ok, "full_ok": full_ok})
    grouped = frame.groupby("case", sort=True)
    table = pd.DataFrame(
        {
            "shipments": grouped.size(),
            "status_correct": grouped["status_ok"].sum().astype("int64"),
            "fully_correct": grouped["full_ok"].sum().astype("int64"),
        }
    )
    table["status_accuracy"] = [
        ratio(int(c), int(n))
        for c, n in zip(table["status_correct"], table["shipments"], strict=True)
    ]
    table["accuracy"] = [
        ratio(int(c), int(n))
        for c, n in zip(table["fully_correct"], table["shipments"], strict=True)
    ]
    return table


# --------------------------------------------------------------------------------------------
# Detection signatures (what the engine's output says about each issue type)
# --------------------------------------------------------------------------------------------


def back_to_back_edge_pairs(reports: pd.DataFrame, loggers: pd.DataFrame) -> pd.DataFrame:
    """Consecutive assignments of one device whose excursions meet at the hand-over.

    A pair (A, B) qualifies when B is the device's next assignment after A, A has an excursion
    ending at its window's last instant (``unassigned_at``), B has one starting at its first
    (``assigned_at``), both run in the same direction, and **both are within their own
    allowance** (``WITHIN_ALLOWANCE``). A device-level merge would join them into one long
    excursion and invent a breach: that is the ``excursion_across_unrelated_shipments`` trap.
    Two edge excursions that already breach on their own (e.g. two whole-trip
    ``product_threshold_mismatch`` shipments that happen to follow each other on one device) are
    not the trap, since merging them changes no conclusion.

    Args:
        reports: Engine excursion reports.
        loggers: Logger assignments.

    Returns:
        ``device_id``, ``first_shipment_id``, ``second_shipment_id``, ``direction``.
    """
    ordered = loggers.sort_values(["device_id", "assigned_at"], kind="stable")
    pairs = pd.DataFrame(
        {
            "device_id": ordered["device_id"],
            "first_shipment_id": ordered["shipment_id"],
            "first_end": ordered["unassigned_at"],
            "second_shipment_id": ordered.groupby("device_id")["shipment_id"].shift(-1),
            "second_start": ordered.groupby("device_id")["assigned_at"].shift(-1),
        }
    ).dropna(subset=["second_shipment_id"])
    allowed = reports[reports["breach_type"] == BreachType.WITHIN_ALLOWANCE]
    ends = allowed[["shipment_id", "ended_at", "direction"]].rename(
        columns={"shipment_id": "first_shipment_id", "ended_at": "first_end"}
    )
    starts = allowed[["shipment_id", "started_at", "direction"]].rename(
        columns={"shipment_id": "second_shipment_id", "started_at": "second_start"}
    )
    matched = pairs.merge(ends, on=["first_shipment_id", "first_end"]).merge(
        starts, on=["second_shipment_id", "second_start", "direction"]
    )
    columns = ["device_id", "first_shipment_id", "second_shipment_id", "direction"]
    return matched[columns].drop_duplicates().reset_index(drop=True)


def predicted_issue_types(
    decisions: pd.DataFrame,
    reports: pd.DataFrame,
    shipments: pd.DataFrame,
    loggers: pd.DataFrame,
    policies: pd.DataFrame,
) -> pd.DataFrame:
    """Which issue type the engine's output (plus the raw tables) points to, per shipment.

    The signatures (docs/evaluation.md §2.1) use only the engine's decisions and reports and
    the raw shipments, loggers and policies, never the answer key:

    * ``logger_not_assigned``: LOGGER_NOT_ASSIGNED;
    * ``product_threshold_mismatch``: POLICY_MISMATCH_DECLARED **and** the declared policy is
      of another temperature class than the applied one (a version-only mismatch is a natural
      planning case, DECISIONS.md #26);
    * ``missing_data_silently_passed``: DATA_GAP **and** CARRIER_STATUS_CONTRADICTED, without
      LOGGER_NOT_ASSIGNED (whose untrusted stream also leaves a gap);
    * ``custody_gap_not_flagged``: CUSTODY_GAP **and** CARRIER_STATUS_CONTRADICTED;
    * ``excursion_across_unrelated_shipments``: both shipments of a
      :func:`back_to_back_edge_pairs` pair (two within-allowance excursions that meet at a
      device's hand-over).

    Args:
        decisions: Engine decisions.
        reports: Engine excursion reports.
        shipments: Raw shipments (``declared_policy_id``).
        loggers: Raw logger assignments.
        policies: Raw threshold policies (``policy_id`` → ``policy_code``).

    Returns:
        ``shipment_id`` and ``issue_type``, one row per (shipment, predicted type), sorted.
    """
    codes = code_sets(decisions["reason_codes"])
    code_of = policies.set_index("policy_id")["policy_code"]
    declared = decisions["shipment_id"].map(
        shipments.set_index("shipment_id")["declared_policy_id"]
    )
    other_class = declared.map(code_of) != decisions["applied_policy_id"].map(code_of)

    def has(code: ReasonCode) -> pd.Series:
        return codes.map(lambda items: code.value in items)

    contradicted = has(ReasonCode.CARRIER_STATUS_CONTRADICTED)
    rules = {
        IssueType.LOGGER_NOT_ASSIGNED: has(ReasonCode.LOGGER_NOT_ASSIGNED),
        IssueType.PRODUCT_THRESHOLD_MISMATCH: has(ReasonCode.POLICY_MISMATCH_DECLARED)
        & other_class,
        IssueType.MISSING_DATA_SILENTLY_PASSED: (
            has(ReasonCode.DATA_GAP) & contradicted & ~has(ReasonCode.LOGGER_NOT_ASSIGNED)
        ),
        IssueType.CUSTODY_GAP_NOT_FLAGGED: has(ReasonCode.CUSTODY_GAP) & contradicted,
    }
    frames = [
        pd.DataFrame({"shipment_id": decisions.loc[mask, "shipment_id"], "issue_type": issue.value})
        for issue, mask in rules.items()
    ]
    pairs = back_to_back_edge_pairs(reports, loggers)
    paired = pd.concat([pairs["first_shipment_id"], pairs["second_shipment_id"]])
    frames.append(
        pd.DataFrame(
            {
                "shipment_id": paired,
                "issue_type": IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value,
            }
        )
    )
    result = pd.concat(frames, ignore_index=True).astype(str).drop_duplicates()
    return result.sort_values(["issue_type", "shipment_id"], kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------------------------
# Validation findings vs injected anomalies
# --------------------------------------------------------------------------------------------


def explain_findings(findings: pd.DataFrame, anomalies: pd.DataFrame) -> pd.DataFrame:
    """Attach to each validation finding the anomaly that explains it, if any.

    A finding is explained when its shipment is a target of an anomaly **and** its check is in
    :data:`EXPECTED_FINDINGS` for that anomaly's issue type. INTEGRITY findings are never
    explained (no issue type lists an integrity check).

    Args:
        findings: Validation findings (``check_id``, ``shipment_id``, ...).
        anomalies: Answer-key anomaly rows (``anomaly_id``, ``shipment_id``, ``issue_type``).

    Returns:
        The findings with ``anomaly_id`` (null if none) and ``explained`` (bool) columns.
    """
    targets = anomalies[["shipment_id", "anomaly_id", "issue_type"]].drop_duplicates("shipment_id")
    frame = findings.merge(targets, on="shipment_id", how="left")
    frame.index = findings.index
    explained = [
        isinstance(issue, str) and check in EXPECTED_FINDINGS[IssueType(issue)]
        for check, issue in zip(frame["check_id"], frame["issue_type"], strict=True)
    ]
    frame["explained"] = pd.Series(explained, index=frame.index, dtype=bool)
    frame["anomaly_id"] = frame["anomaly_id"].astype(object).where(frame["explained"], None)
    return frame.drop(columns="issue_type")


def surfaced_anomalies(anomalies: pd.DataFrame, findings: pd.DataFrame) -> pd.DataFrame:
    """Whether each anomaly row produced every validation finding its issue type implies.

    Args:
        anomalies: Answer-key anomaly rows.
        findings: Validation findings.

    Returns:
        One row per anomaly row: ``anomaly_id``, ``shipment_id``, ``issue_type``,
        ``expected_checks`` and ``found_checks`` (sorted lists), ``surfaced`` (bool; rows whose
        type implies no check are ``None``).
    """
    found = findings.groupby("shipment_id")["check_id"].agg(lambda s: frozenset(s))
    rows = []
    for anomaly, shipment, issue in zip(
        anomalies["anomaly_id"], anomalies["shipment_id"], anomalies["issue_type"], strict=True
    ):
        expected = EXPECTED_FINDINGS[IssueType(issue)]
        present = found.get(shipment, frozenset())
        rows.append(
            {
                "anomaly_id": anomaly,
                "shipment_id": shipment,
                "issue_type": issue,
                "expected_checks": sorted(expected),
                "found_checks": sorted(present),
                "surfaced": (expected <= present) if expected else None,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "anomaly_id",
            "shipment_id",
            "issue_type",
            "expected_checks",
            "found_checks",
            "surfaced",
        ],
    )
