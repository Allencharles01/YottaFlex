from datetime import date

import numpy as np
import pandas as pd
import pytest

from coldchain.enums import PolicyCode, Scenario, ScenarioVariant, TableName
from coldchain.errors import GenerationError
from coldchain.generate import scenarios
from coldchain.rng import stage_rng
from coldchain.schemas import effective_policy_ids

BOUNDARY_V2 = pd.Timestamp("2026-07-01", tz="UTC")


def _utc(value: str) -> pd.Timestamp:
    return pd.Timestamp(value, tz="UTC")


def _outcomes(tables) -> pd.DataFrame:
    return tables[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")


# --------------------------------------------------------------------------------------------
# Scenario counts
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (50, [39, 3, 2, 3, 2, 1]),  # EXCURSION_BREACH topped up to its 3 variants
        (5000, [4000, 300, 150, 250, 200, 100]),
        (9, [1, 1, 2, 3, 1, 1]),  # exactly one per variant
        (6, [1, 1, 1, 1, 1, 1]),  # too few for every variant: one per scenario
        (100, [80, 6, 3, 5, 4, 2]),
    ],
)
def test_scenario_counts_follow_weights_with_every_variant(
    app_config, count: int, expected: list[int]
) -> None:
    counts = scenarios.scenario_counts(app_config.scenario_weights, count)
    assert [counts[s] for s in Scenario] == expected
    assert sum(counts.values()) == count


def test_scenario_counts_skip_zero_weights_and_tiny_runs(app_config) -> None:
    weights = dict(app_config.scenario_weights)
    weights[Scenario.NORMAL] += weights[Scenario.BOUNDARY]
    weights[Scenario.BOUNDARY] = 0.0
    counts = scenarios.scenario_counts(weights, 50)
    assert counts[Scenario.BOUNDARY] == 0
    assert all(counts[s] >= 1 for s in Scenario if s is not Scenario.BOUNDARY)
    tiny = scenarios.scenario_counts(app_config.scenario_weights, 3)
    assert sum(tiny.values()) == 3


# --------------------------------------------------------------------------------------------
# Assignment (starter scale)
# --------------------------------------------------------------------------------------------


def test_every_scenario_appears_at_starter_scale(starter_tables, app_config) -> None:
    observed = _outcomes(starter_tables)["scenario"].value_counts()
    expected = scenarios.scenario_counts(app_config.scenario_weights, 50)
    assert {Scenario(s): int(n) for s, n in observed.items()} == expected
    assert all(expected[s] >= 1 for s in Scenario)


def test_every_breach_variant_appears_at_starter_scale(starter_tables) -> None:
    """DECISIONS.md #55: SINGLE, ABSOLUTE and CUMULATIVE breaches are all present."""
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES]
    breach = outcomes[outcomes["scenario"] == Scenario.EXCURSION_BREACH.value]
    codes = set("|".join(breach["expected_reason_codes"]).split("|"))
    assert {
        "SINGLE_DURATION_BREACH",
        "ABSOLUTE_LIMIT_BREACH",
        "CUMULATIVE_DURATION_BREACH",
    } <= codes
    assert (breach["expected_status"] == "NON_COMPLIANT").all()


def test_variants_cycle_in_shipment_order(starter_tables, app_config, reference) -> None:
    plan = starter_tables[TableName.SHIPMENTS]
    assignment = scenarios.assign_scenarios(
        app_config,
        plan,
        starter_tables[TableName.DEVICES],
        stage_rng(app_config.seed, scenarios.STAGES["assign"]),
    )
    assert assignment["scenario"].tolist() == _outcomes(starter_tables)["scenario"].tolist()
    for scenario, group in assignment.groupby("scenario"):
        cycle = scenarios.SCENARIO_VARIANTS[Scenario(scenario)]
        assert group["variant"].tolist() == [cycle[k % len(cycle)].value for k in range(len(group))]
    boundary = assignment[assignment["scenario"] == Scenario.BOUNDARY]
    assert set(boundary["variant"]) == {"AT_LIMIT", "EXACT_ALLOWANCE"}
    assert set(assignment["direction"]) == {"HIGH", "LOW"}


def test_calibration_expired_needs_an_expired_device(starter_tables, app_config) -> None:
    devices = starter_tables[TableName.DEVICES].assign(calibration_due_on=date(2030, 1, 1))
    with pytest.raises(GenerationError, match="can host CALIBRATION_EXPIRED"):
        scenarios.assign_scenarios(
            app_config, starter_tables[TableName.SHIPMENTS], devices, stage_rng(1, "x")
        )


def test_calibration_expired_eligibility_waits_for_the_kth_expiry(app_config) -> None:
    devices = pd.DataFrame({"calibration_due_on": [date(2026, 5, 1), date(2026, 6, 1)]})
    plan = pd.DataFrame(
        {"planned_departure_at": [_utc("2026-05-02"), _utc("2026-05-10"), _utc("2026-06-10")]}
    )
    assert scenarios.calibration_expired_eligible(app_config, plan, devices, 1).tolist() == [
        False,
        True,
        True,
    ]
    assert scenarios.calibration_expired_eligible(app_config, plan, devices, 2).tolist() == [
        False,
        False,
        True,
    ]
    assert not scenarios.calibration_expired_eligible(app_config, plan, devices, 3).any()
    assert not scenarios.calibration_expired_eligible(app_config, plan, devices, 0).any()


def test_flags_follow_scenarios(starter_tables, app_config) -> None:
    assignment = pd.DataFrame(
        {
            "scenario": ["NORMAL", "LONG_DWELL", "CALIBRATION_EXPIRED", "EXCURSION_BREACH"] * 2,
            "variant": ["NONE", "NONE", "NONE", "CUMULATIVE_DURATION"] * 2,
        }
    )
    plan = pd.DataFrame({"policy_code": ["AMBIENT_15_25"] * 4 + ["REFRIG_2_8"] * 4})
    policies = starter_tables[TableName.THRESHOLD_POLICIES]
    flags = scenarios.shipment_flags(
        app_config, assignment, plan, policies, np.array([-20, -20, 30, -20] * 2)
    )
    assert flags.long_dwell.tolist() == [False, True, False, False] * 2
    assert flags.expired_device.tolist() == [False, False, True, False] * 2
    assert flags.min_departure_delay_min.tolist() == [-20, -20, 30, -20] * 2
    # AMBIENT: 3 x 6 readings + 2 x 9 gaps + 2 x 4 ramps = 44 readings = 440 min;
    # REFRIG worst version (V2): 4 x 2 + 3 x 9 + 8 = 43 readings = 430 min.
    # Then / 0.95 drive factor + 3 stops x (60 - 20) min shorter dwell.
    assert flags.min_trip_min.tolist() == [240, 240, 240, 584, 240, 240, 240, 573]


def test_cumulative_trips_fit_their_design(starter_tables, app_config) -> None:
    """At starter every CUMULATIVE_DURATION shipment gets a trip long enough for its design."""
    outcomes = starter_tables[TableName.EXPECTED_OUTCOMES]
    cumulative = outcomes.loc[
        outcomes["expected_reason_codes"].fillna("").str.contains("CUMULATIVE"), "shipment_id"
    ]
    assert len(cumulative) >= 1
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id").loc[cumulative]
    hours = (ships["planned_arrival_at"] - ships["planned_departure_at"]).dt.total_seconds()
    assert (hours / 60 >= 573).all()


# --------------------------------------------------------------------------------------------
# Policy transitions (DECISIONS.md #26, #39)
# --------------------------------------------------------------------------------------------


def test_policy_transitions_inside_the_window(starter_tables, app_config) -> None:
    transitions = scenarios.policy_transitions(
        starter_tables[TableName.THRESHOLD_POLICIES], app_config
    )
    assert transitions["policy_id"].tolist() == ["THR-REFRIG_2_8-V2"]
    assert transitions["boundary"].tolist() == [BOUNDARY_V2]


def _plan(rows: list[tuple[str, str]], products: pd.DataFrame) -> pd.DataFrame:
    """A hand-made plan: (planned departure, policy code of the primary product) per shipment."""
    first = products.groupby("policy_code").first()
    return pd.DataFrame(
        {
            "shipment_id": [f"SHP-2026-{k:06d}" for k in range(1, len(rows) + 1)],
            "primary_product_id": [first.loc[code, "product_id"] for _, code in rows],
            "policy_code": [code for _, code in rows],
            "supplier_party_id": [first.loc[code, "supplier_party_id"] for _, code in rows],
            "planned_departure_at": [_utc(at) for at, _ in rows],
        }
    )


def test_natural_candidate_is_kept_and_delayed(starter_tables, app_config) -> None:
    products = starter_tables[TableName.PRODUCTS]
    plan = _plan([("2026-06-30T21:00", "REFRIG_2_8"), ("2026-06-30T22:30", "REFRIG_2_8")], products)
    forced, delay = scenarios.force_policy_transitions(
        app_config, plan, starter_tables[TableName.THRESHOLD_POLICIES], products, stage_rng(1, "x")
    )
    pd.testing.assert_frame_equal(forced, plan)
    assert delay.tolist() == [app_config.generation.departure_delay_min.min, 90]


def test_missing_transition_is_forced_on_latest_shipment(starter_tables, app_config) -> None:
    products, policies = (
        starter_tables[TableName.PRODUCTS],
        starter_tables[TableName.THRESHOLD_POLICIES],
    )
    plan = _plan(
        [
            ("2026-06-10T08:00", "FROZEN"),
            ("2026-06-20T08:00", "FROZEN"),
            ("2026-07-02T08:00", "COOL_8_15"),
        ],
        products,
    )
    for seed in range(5):
        forced, delay = scenarios.force_policy_transitions(
            app_config, plan, policies, products, stage_rng(seed, "x")
        )
        moved = forced.iloc[1]
        assert moved["policy_code"] == PolicyCode.REFRIG_2_8
        assert products.set_index("product_id").loc[moved["primary_product_id"], "policy_code"] == (
            PolicyCode.REFRIG_2_8
        )
        lead = BOUNDARY_V2 - moved["planned_departure_at"]
        assert pd.Timedelta(minutes=1) <= lead <= pd.Timedelta(minutes=120)
        assert delay[1] == lead.total_seconds() // 60
        assert forced["planned_departure_at"].is_monotonic_increasing
        pd.testing.assert_frame_equal(forced.drop(index=1), plan.drop(index=1))


def test_transition_needs_a_shipment_before_it_and_a_positive_delay(
    starter_tables, app_config
) -> None:
    products, policies = (
        starter_tables[TableName.PRODUCTS],
        starter_tables[TableName.THRESHOLD_POLICIES],
    )
    late = _plan([("2026-07-05T08:00", "REFRIG_2_8")], products)
    with pytest.raises(GenerationError, match="No shipment is planned before"):
        scenarios.force_policy_transitions(app_config, late, policies, products, stage_rng(1, "x"))
    no_delay = app_config.model_copy(
        update={
            "generation": app_config.generation.model_copy(
                update={
                    "departure_delay_min": app_config.generation.departure_delay_min.model_copy(
                        update={"max": 0}
                    )
                }
            )
        }
    )
    early = _plan([("2026-06-30T22:00", "REFRIG_2_8")], products)
    with pytest.raises(GenerationError, match=r"departure_delay_min\.max"):
        scenarios.force_policy_transitions(no_delay, early, policies, products, stage_rng(1, "x"))


def test_starter_has_a_declared_v1_applied_v2_shipment(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS]
    straddle = ships[
        (ships["planned_departure_at"] < BOUNDARY_V2)
        & (ships["actual_departure_at"] >= BOUNDARY_V2)
    ]
    straddle = straddle[straddle["declared_policy_id"] == "THR-REFRIG_2_8-V1"]
    assert len(straddle) >= 1
    reasons = _outcomes(starter_tables).loc[straddle["shipment_id"], "expected_reason_codes"]
    assert reasons.str.contains("POLICY_MISMATCH_DECLARED").all()


def test_policy_mismatch_expected_exactly_when_declared_differs(starter_tables) -> None:
    ships = starter_tables[TableName.SHIPMENTS].set_index("shipment_id")
    lots = starter_tables[TableName.LOTS].set_index("lot_id")
    products = starter_tables[TableName.PRODUCTS].set_index("product_id")
    pairs = starter_tables[TableName.SHIPMENT_LOTS]
    code = (
        pd.Series(
            products.loc[lots.loc[pairs["lot_id"], "product_id"], "policy_code"].to_numpy(),
            index=pairs["shipment_id"],
        )
        .groupby(level=0)
        .first()
    )
    applied = effective_policy_ids(
        starter_tables[TableName.THRESHOLD_POLICIES],
        code,
        ships.loc[code.index, "actual_departure_at"],
    )
    differs = ships.loc[code.index, "declared_policy_id"] != applied
    flagged = (
        _outcomes(starter_tables)
        .loc[code.index, "expected_reason_codes"]
        .fillna("")
        .str.contains("POLICY_MISMATCH_DECLARED")
    )
    assert (differs == flagged).all()


# --------------------------------------------------------------------------------------------
# Design (hand-made inputs, REFRIG_2_8 V1: single 30 min, cumulative 60 min)
# --------------------------------------------------------------------------------------------


def _design_inputs(variants: list[ScenarioVariant], trip_hours: float = 24.0):
    count = len(variants)
    sid = [f"SHP-2026-{k:06d}" for k in range(1, count + 1)]
    start = _utc("2026-03-01T08:00")
    assignment = pd.DataFrame(
        {
            "shipment_id": sid,
            "scenario": "BOUNDARY",
            "variant": [v.value for v in variants],
            "direction": "HIGH",
        }
    )
    departure = start + pd.Timedelta(minutes=30)
    shipments = pd.DataFrame(
        {
            "shipment_id": sid,
            "actual_departure_at": departure,
            "actual_arrival_at": departure + pd.Timedelta(hours=trip_hours),
        }
    )
    loggers = pd.DataFrame(
        {
            "shipment_id": sid,
            "logger_id": [f"LOG-{s}-01" for s in sid],
            "assigned_at": start,
            "unassigned_at": departure + pd.Timedelta(hours=trip_hours, minutes=30),
        }
    )
    applied = pd.DataFrame(
        {"shipment_id": sid, "max_single_excursion_min": 30, "max_cumulative_excursion_min": 60}
    )
    return assignment, shipments, loggers, applied


@pytest.mark.parametrize("seed", range(5))
def test_design_lengths_per_variant(app_config, seed: int) -> None:
    variants = [
        ScenarioVariant.SPIKE,
        ScenarioVariant.AT_LIMIT,
        ScenarioVariant.EXACT_ALLOWANCE,
        ScenarioVariant.SINGLE_DURATION,
        ScenarioVariant.ABSOLUTE_LIMIT,
        ScenarioVariant.CUMULATIVE_DURATION,
        ScenarioVariant.NONE,
    ]
    segments = scenarios.design_segments(
        app_config, *_design_inputs(variants), stage_rng(seed, "x")
    ).set_index("shipment_id")
    by = {v: segments.loc[[f"SHP-2026-{k:06d}"]] for k, v in enumerate(variants, 1) if k < 7}
    assert "SHP-2026-000007" not in segments.index  # NONE has no segment
    assert by[ScenarioVariant.SPIKE]["reading_count"].between(1, 2).all()
    assert not by[ScenarioVariant.AT_LIMIT]["is_excursion"].any()
    assert by[ScenarioVariant.AT_LIMIT]["reading_count"].between(1, 3).all()
    assert by[ScenarioVariant.EXACT_ALLOWANCE]["reading_count"].tolist() == [3]
    assert by[ScenarioVariant.SINGLE_DURATION]["reading_count"].between(4, 9).all()
    absolute = by[ScenarioVariant.ABSOLUTE_LIMIT].iloc[0]
    assert 1 <= absolute["reading_count"] <= 3
    assert 0 <= absolute["absolute_index"] < absolute["reading_count"]
    cumulative = by[ScenarioVariant.CUMULATIVE_DURATION]
    assert cumulative["reading_count"].tolist() == [3, 3, 3]  # 3 x 30 = 90 > 60
    assert np.diff(cumulative["first_index"]).tolist() == [12, 12]  # 3 readings + 9 separators
    assert (segments.loc[segments["absolute_index"] >= 0].index == ["SHP-2026-000005"]).all()
    assert segments["ramp_before"].between(2, 4).all()
    assert segments["ramp_after"].between(2, 4).all()
    # Placed in transit (departure is reading 3, arrival 147) with room for the longest ramp
    # (4 readings) inside the window (last reading 150).
    last = segments["first_index"] + segments["reading_count"] - 1
    assert (segments["first_index"] >= 4).all() and (last <= 146).all()
    duration = segments["ended_at"] - segments["started_at"]
    assert (duration == pd.to_timedelta((segments["reading_count"] - 1) * 10, unit="min")).all()


def test_design_falls_back_to_the_logger_window_then_fails(app_config) -> None:
    variants = [ScenarioVariant.CUMULATIVE_DURATION]
    assignment, shipments, loggers, applied = _design_inputs(variants, trip_hours=2.0)
    loggers = loggers.assign(unassigned_at=loggers["assigned_at"] + pd.Timedelta(hours=6))
    segments = scenarios.design_segments(
        app_config, assignment, shipments, loggers, applied, stage_rng(1, "x")
    )
    # 08:00 → 14:00 (last reading 36); transit (readings 3..15) is too short for the
    # 27-reading block (3 x 3 + 2 x 9), so it goes anywhere with 4 ramp readings free each side.
    window_last = 36
    assert segments["first_index"].min() >= 4
    assert (segments["first_index"] + segments["reading_count"] - 1).max() <= window_last - 4
    tiny = loggers.assign(unassigned_at=loggers["assigned_at"] + pd.Timedelta(minutes=60))
    with pytest.raises(GenerationError, match="do not fit"):
        scenarios.design_segments(
            app_config, assignment, shipments, tiny, applied, stage_rng(1, "x")
        )


def test_design_rejects_allowance_below_one_reading(app_config) -> None:
    assignment, shipments, loggers, applied = _design_inputs([ScenarioVariant.SPIKE])
    with pytest.raises(GenerationError, match="shorter than one reading"):
        scenarios.design_segments(
            app_config,
            assignment,
            shipments,
            loggers,
            applied.assign(max_single_excursion_min=5),
            stage_rng(1, "x"),
        )


def test_design_with_no_shaped_shipment_is_empty(app_config) -> None:
    segments = scenarios.design_segments(
        app_config, *_design_inputs([ScenarioVariant.NONE]), stage_rng(1, "x")
    )
    assert segments.empty
    assert tuple(segments.columns) == scenarios.SEGMENT_COLUMNS
