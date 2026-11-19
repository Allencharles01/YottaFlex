"""Expected outcomes: rule derivation on hand-made facts, and a cross-check against the readings."""

from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd
import pytest

from coldchain.enums import ComplianceStatus, Scenario, TableName
from coldchain.errors import GenerationError
from coldchain.generate import expected
from coldchain.io import coerce_to_schema
from coldchain.schemas import split_list, validate

START = pd.Timestamp("2026-03-01T08:00", tz="UTC")
END = START + pd.Timedelta(hours=24)
V1 = {"applied_policy_id": "THR-REFRIG_2_8-V1", "max_single_excursion_min": 30}
V2_ID = "THR-REFRIG_2_8-V2"
V2 = {"applied_policy_id": V2_ID, "max_single_excursion_min": 20}


@dataclass
class Case:
    """One hand-made shipment: its designed segments and the facts around them."""

    segments: list[tuple[int, int, bool, str]] = field(default_factory=list)
    """(reading_count, absolute_index, is_excursion, direction) per segment."""
    policy: dict = field(default_factory=lambda: dict(V1))
    declared: str = "THR-REFRIG_2_8-V1"
    due: date = date(2027, 1, 1)
    calibrated: date = date(2026, 1, 1)
    dwell: int | None = 60
    uncertainty: float = 0.3


def _derive(app_config, cases: list[Case]) -> pd.DataFrame:
    sid = [f"SHP-2026-{k:06d}" for k in range(1, len(cases) + 1)]
    assignment = pd.DataFrame({"shipment_id": sid, "scenario": "NORMAL"})
    applied = pd.DataFrame(
        {
            "shipment_id": sid,
            "declared_policy_id": [c.declared for c in cases],
            "policy_code": "REFRIG_2_8",
            "applied_policy_id": [c.policy["applied_policy_id"] for c in cases],
            "min_c": 2.0,
            "max_c": 8.0,
            "abs_min_c": 0.0,
            "abs_max_c": 12.0,
            "max_single_excursion_min": [c.policy["max_single_excursion_min"] for c in cases],
            "max_cumulative_excursion_min": 60,
        }
    )
    loggers = pd.DataFrame(
        {
            "shipment_id": sid,
            "device_id": [f"DEV-{k:06d}" for k in range(1, len(cases) + 1)],
            "assigned_at": START,
            "unassigned_at": END,
        }
    )
    devices = pd.DataFrame(
        {
            "device_id": loggers["device_id"],
            "calibrated_on": [c.calibrated for c in cases],
            "calibration_due_on": [c.due for c in cases],
            "uncertainty_c": [c.uncertainty for c in cases],
        }
    )
    stops = pd.DataFrame(
        {"shipment_id": sid, "dwell_min": pd.array([c.dwell for c in cases], dtype="Int64")}
    )
    rows = []
    for ship, case in zip(sid, cases, strict=True):
        first = 5
        for number, (count, absolute, is_excursion, direction) in enumerate(case.segments, 1):
            started = START + pd.Timedelta(minutes=10 * first)
            rows.append(
                {
                    "shipment_id": ship,
                    "logger_id": f"LOG-{ship}-01",
                    "segment": number,
                    "is_excursion": is_excursion,
                    "direction": direction,
                    "first_index": first,
                    "reading_count": count,
                    "absolute_index": absolute,
                    "started_at": started,
                    "ended_at": started + pd.Timedelta(minutes=10 * (count - 1)),
                }
            )
            first += count + 3
    segments = pd.DataFrame(rows, columns=list(expected_columns()))
    segments["started_at"] = pd.to_datetime(segments["started_at"], utc=True)
    segments["ended_at"] = pd.to_datetime(segments["ended_at"], utc=True)
    outcomes = expected.derive_expected_outcomes(
        app_config, assignment, applied, loggers, devices, stops, segments
    )
    validate(TableName.EXPECTED_OUTCOMES, coerce_to_schema(TableName.EXPECTED_OUTCOMES, outcomes))
    return outcomes.set_index("shipment_id")


def expected_columns() -> tuple[str, ...]:
    from coldchain.generate.scenarios import SEGMENT_COLUMNS

    return SEGMENT_COLUMNS


EXC = (True, "HIGH")


@pytest.mark.parametrize(
    ("case", "reasons", "durations"),
    [
        (Case(), None, None),
        (Case(segments=[(3, -1, *EXC)]), "EXCURSION_WITHIN_ALLOWANCE", "30"),
        (Case(segments=[(4, -1, *EXC)]), "SINGLE_DURATION_BREACH", "40"),
        (
            Case(segments=[(3, -1, *EXC)], policy=dict(V2), declared=V2_ID),
            "SINGLE_DURATION_BREACH",
            "30",
        ),
        (
            Case(segments=[(2, -1, *EXC)], policy=dict(V2), declared=V2_ID),
            "EXCURSION_WITHIN_ALLOWANCE",
            "20",
        ),
        (Case(segments=[(1, 0, *EXC)]), "ABSOLUTE_LIMIT_BREACH", "10"),
        (Case(segments=[(4, 2, *EXC)]), "ABSOLUTE_LIMIT_BREACH", "40"),
        (Case(segments=[(3, -1, *EXC), (3, -1, *EXC)]), "EXCURSION_WITHIN_ALLOWANCE", "30|30"),
        (
            Case(segments=[(3, -1, *EXC), (3, -1, *EXC), (1, -1, True, "LOW")]),
            "CUMULATIVE_DURATION_BREACH|EXCURSION_WITHIN_ALLOWANCE",
            "30|30|10",
        ),
        (
            Case(segments=[(7, -1, *EXC)]),
            "SINGLE_DURATION_BREACH|CUMULATIVE_DURATION_BREACH",
            "70",
        ),
        (Case(segments=[(2, -1, False, "HIGH")]), "NEAR_LIMIT_UNCERTAIN", None),
        (Case(segments=[(1, -1, False, "LOW")]), "NEAR_LIMIT_UNCERTAIN", None),
        (Case(due=date(2026, 3, 2)), None, None),  # due on the last day is still valid
        (Case(due=date(2026, 3, 1)), "CALIBRATION_EXPIRED", None),
        (Case(calibrated=date(2026, 3, 2)), "CALIBRATION_EXPIRED", None),
        (Case(dwell=120), None, None),
        (Case(dwell=121), "LONG_DWELL", None),
        (Case(dwell=None), None, None),
        (Case(policy=dict(V2)), "POLICY_MISMATCH_DECLARED", None),
        (
            Case(segments=[(1, 0, *EXC)], policy=dict(V2), dwell=200),
            "ABSOLUTE_LIMIT_BREACH|POLICY_MISMATCH_DECLARED|LONG_DWELL",
            "10",
        ),
    ],
    ids=lambda v: str(v) if not isinstance(v, Case) else None,
)
def test_rules_applied_to_designed_facts(app_config, case: Case, reasons, durations) -> None:
    row = _derive(app_config, [case]).iloc[0]
    for column, value in (
        ("expected_reason_codes", reasons),
        ("expected_excursion_durations_min", durations),
    ):
        assert (pd.isna(row[column]) and value is None) or row[column] == value, column
    total = sum(int(d) for d in split_list(durations))
    assert row["expected_total_excursion_min"] == total
    assert row["expected_excursion_count"] == len(split_list(durations))


def test_status_and_action_follow_the_decision_table(app_config) -> None:
    outcomes = _derive(
        app_config,
        [Case(), Case(dwell=121), Case(due=date(2026, 1, 1)), Case(segments=[(4, -1, *EXC)])],
    )
    assert outcomes["expected_status"].tolist() == [
        "COMPLIANT",
        "COMPLIANT_WITH_WARNINGS",
        "INCONCLUSIVE",
        "NON_COMPLIANT",
    ]
    assert outcomes["expected_action"].tolist() == ["RELEASE", "RELEASE", "REVIEW", "QUARANTINE"]


def test_windows_are_first_and_last_reading_times(app_config) -> None:
    row = _derive(app_config, [Case(segments=[(3, -1, *EXC), (1, -1, True, "LOW")])]).iloc[0]
    assert row["expected_excursion_windows"] == (
        "2026-03-01T08:50:00Z/2026-03-01T09:10:00Z|2026-03-01T09:50:00Z/2026-03-01T09:50:00Z"
    )


def test_applied_policy_rejects_mixed_codes_and_missing_lots(starter_tables) -> None:
    tables = starter_tables
    products = tables[TableName.PRODUCTS]
    pairs = tables[TableName.SHIPMENT_LOTS]
    lots = tables[TableName.LOTS]
    ship = pairs["shipment_id"].iloc[0]
    other_code = products.loc[products["policy_code"] != "REFRIG_2_8", "product_id"].iloc[0]
    other_code_lot = lots.loc[lots["product_id"] == other_code, "lot_id"].iloc[0]
    ours = products.set_index("product_id").loc[
        lots.set_index("lot_id").loc[pairs["lot_id"].iloc[0], "product_id"], "policy_code"
    ]
    if ours == products.set_index("product_id").loc[other_code, "policy_code"]:
        other_code = products.loc[products["policy_code"] != ours, "product_id"].iloc[0]
        other_code_lot = lots.loc[lots["product_id"] == other_code, "lot_id"].iloc[0]
    mixed = pd.concat(
        [
            pairs,
            pd.DataFrame(
                {"shipment_id": [ship], "lot_id": [other_code_lot], "quantity_units": [1]}
            ),
        ]
    )
    args = (
        tables[TableName.SHIPMENTS],
        mixed,
        lots,
        products,
        tables[TableName.THRESHOLD_POLICIES],
    )
    with pytest.raises(GenerationError, match="mix policy codes"):
        expected.applied_policies(*args)
    without = pairs[pairs["shipment_id"] != ship]
    with pytest.raises(GenerationError, match="without lots"):
        expected.applied_policies(
            tables[TableName.SHIPMENTS],
            without,
            lots,
            products,
            tables[TableName.THRESHOLD_POLICIES],
        )


# --------------------------------------------------------------------------------------------
# Cross-check: an independent scan of the generated readings reproduces the ground truth
# --------------------------------------------------------------------------------------------


def _limits(tables) -> pd.DataFrame:
    """Applied policy limits and device uncertainty per logger, computed independently."""
    ships = tables[TableName.SHIPMENTS]
    lots = tables[TableName.LOTS].set_index("lot_id")
    products = tables[TableName.PRODUCTS].set_index("product_id")
    pairs = tables[TableName.SHIPMENT_LOTS]
    code = (
        pd.Series(
            products.loc[lots.loc[pairs["lot_id"], "product_id"], "policy_code"].to_numpy(),
            index=pairs["shipment_id"].to_numpy(),
        )
        .groupby(level=0)
        .first()
    )
    policies = tables[TableName.THRESHOLD_POLICIES]
    frame = ships[["shipment_id", "actual_departure_at"]].assign(
        policy_code=code.loc[ships["shipment_id"]].to_numpy()
    )
    start = pd.to_datetime(policies["effective_from"]).dt.tz_localize("UTC")
    end = pd.to_datetime(policies["effective_to"].fillna(date(2100, 1, 1))).dt.tz_localize("UTC")
    windows = policies.assign(start=start, end=end)
    frame = frame.merge(windows, on="policy_code")
    frame = frame[
        (frame["start"] <= frame["actual_departure_at"])
        & (frame["actual_departure_at"] < frame["end"])
    ]
    loggers = tables[TableName.LOGGERS].merge(
        tables[TableName.DEVICES][["device_id", "uncertainty_c"]], on="device_id"
    )
    return loggers.merge(frame, on="shipment_id")


def _scan(tables) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Out-of-range runs per logger, and the near-limit flag per shipment."""
    limits = _limits(tables)
    readings = tables[TableName.TEMPERATURE_READINGS].merge(
        limits[
            [
                "logger_id",
                "shipment_id",
                "min_c",
                "max_c",
                "abs_min_c",
                "abs_max_c",
                "uncertainty_c",
            ]
        ],
        on="logger_id",
    )
    readings = readings.sort_values(["logger_id", "recorded_at"]).reset_index(drop=True)
    t = readings["temperature_c"]
    direction = np.select([t > readings["max_c"], t < readings["min_c"]], ["HIGH", "LOW"], "")
    readings["direction"] = direction
    readings["beyond"] = (t > readings["abs_max_c"]) | (t < readings["abs_min_c"])
    change = (readings["direction"] != readings["direction"].shift()) | (
        readings["logger_id"] != readings["logger_id"].shift()
    )
    readings["run"] = change.cumsum()
    out = readings[readings["direction"] != ""]
    runs = out.groupby("run").agg(
        shipment_id=("shipment_id", "first"),
        started_at=("recorded_at", "min"),
        ended_at=("recorded_at", "max"),
        beyond=("beyond", "any"),
    )
    gap = np.minimum((t - readings["min_c"]).abs(), (t - readings["max_c"]).abs()).round(2)
    near = (gap <= readings["uncertainty_c"]).groupby(readings["shipment_id"]).any()
    return runs.sort_values(["shipment_id", "started_at"]), near


def test_readings_reproduce_expected_excursion_windows(starter_tables) -> None:
    runs, _ = _scan(starter_tables)
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    windows = (
        runs["started_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        + "/"
        + runs["ended_at"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    observed = windows.groupby(runs["shipment_id"]).agg("|".join)
    durations = (
        ((runs["ended_at"] - runs["started_at"]).dt.total_seconds() // 60 + 10)
        .astype(int)
        .astype(str)
        .groupby(runs["shipment_id"])
        .agg("|".join)
    )
    with_excursions = outcomes[outcomes["expected_excursion_count"] > 0]
    assert sorted(observed.index) == sorted(with_excursions.index)
    assert (
        observed.loc[with_excursions.index] == with_excursions["expected_excursion_windows"]
    ).all()
    assert (
        durations.loc[with_excursions.index] == with_excursions["expected_excursion_durations_min"]
    ).all()
    absolute = runs.groupby("shipment_id")["beyond"].any()
    flagged = outcomes.loc[absolute.index, "expected_reason_codes"].str.contains(
        "ABSOLUTE_LIMIT_BREACH"
    )
    assert (absolute == flagged).all()


def test_readings_reproduce_expected_near_limit_flags(starter_tables) -> None:
    _, near = _scan(starter_tables)
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    flagged = outcomes["expected_reason_codes"].fillna("").str.contains("NEAR_LIMIT_UNCERTAIN")
    assert (near.loc[outcomes.index] == flagged).all()
    assert flagged.any()


def test_normal_shipments_never_leave_their_range(starter_tables) -> None:
    runs, near = _scan(starter_tables)
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    normal = outcomes.index[outcomes["scenario"] == Scenario.NORMAL]
    assert len(normal) >= 30
    assert not runs["shipment_id"].isin(normal).any()
    assert not near.loc[normal].any()


@pytest.mark.parametrize(
    ("scenario", "statuses", "required"),
    [
        (Scenario.NORMAL, {"COMPLIANT", "COMPLIANT_WITH_WARNINGS"}, None),
        (Scenario.MINOR_SPIKE_WITHIN_ALLOWANCE, {"COMPLIANT_WITH_WARNINGS"}, "EXCURSION_WITHIN"),
        (Scenario.BOUNDARY, {"COMPLIANT_WITH_WARNINGS"}, None),
        (Scenario.EXCURSION_BREACH, {"NON_COMPLIANT"}, None),
        (Scenario.LONG_DWELL, {"COMPLIANT_WITH_WARNINGS"}, "LONG_DWELL"),
        (Scenario.CALIBRATION_EXPIRED, {"INCONCLUSIVE"}, "CALIBRATION_EXPIRED"),
    ],
)
def test_each_scenario_has_its_expected_status(
    starter_tables, scenario: Scenario, statuses: set[str], required: str | None
) -> None:
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES]
    rows = outcomes[outcomes["scenario"] == scenario]
    assert not rows.empty
    assert set(rows["expected_status"]) <= statuses
    if required:
        assert rows["expected_reason_codes"].str.contains(required).all()
    assert rows["issue_type"].isna().all()


def test_normal_shipments_are_compliant_unless_the_policy_version_changed(starter_tables) -> None:
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES]
    normal = outcomes[outcomes["scenario"] == Scenario.NORMAL]
    warned = normal[normal["expected_status"] != ComplianceStatus.COMPLIANT]
    assert (warned["expected_reason_codes"] == "POLICY_MISMATCH_DECLARED").all()
