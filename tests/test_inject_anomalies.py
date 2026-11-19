"""Anomaly injection on the in-memory starter tables (PROJECT_PLAN.md §7.2, Phase 5 tests).

* only the target shipments' records change (row hashes of every other shipment are equal);
* every anomaly row references existing IDs, removed IDs are really gone, and surviving IDs are
  never renumbered;
* no label column leaks into the raw tables;
* injection is deterministic;
* each issue type has the shape its catalogue entry promises (see docs/anomaly_catalogue.md).
"""

import ast

import numpy as np
import pandas as pd
import pytest

from coldchain.compliance import check_tables
from coldchain.config import PROJECT_ROOT, parse_config
from coldchain.enums import IssueType, Layer, ReasonCode, TableName
from coldchain.errors import GenerationError, InjectionError
from coldchain.generate.expected import applied_policies
from coldchain.generate.readings import reshape_segments
from coldchain.inject import INJECT_OUTPUTS, inject_tables
from coldchain.inject.anomalies import (
    back_to_back_pairs,
    inject_logger_not_assigned,
    select_targets,
    target_counts,
)
from coldchain.io import raw_tables
from coldchain.schemas import PRIMARY_KEYS, SCHEMAS, TABLE_LAYERS, split_list

INJECT_DIR = PROJECT_ROOT / "src" / "coldchain" / "inject"


@pytest.fixture(scope="module")
def baseline(starter_tables) -> dict[TableName, pd.DataFrame]:
    return {name: frame for name, frame in starter_tables.items() if name in INJECT_OUTPUTS}


@pytest.fixture(scope="module")
def injected(app_config, baseline) -> dict[TableName, pd.DataFrame]:
    return inject_tables(app_config, baseline)


@pytest.fixture(scope="module")
def anomalies(injected) -> pd.DataFrame:
    return injected[TableName.ANOMALIES]


def _targets(anomalies: pd.DataFrame, issue: IssueType) -> list[str]:
    return anomalies.loc[anomalies["issue_type"] == issue.value, "shipment_id"].tolist()


def _shipment_of(table: TableName, frame: pd.DataFrame) -> pd.Series:
    """Shipment ID of every row of a shipment-scoped table (``None`` for master data)."""
    if "shipment_id" in frame.columns:
        return frame["shipment_id"]
    if "logger_id" in frame.columns:
        return frame["logger_id"].str.slice(4, 19)  # LOG-<shipment-id>-01
    return pd.Series(None, index=frame.index, dtype=object)


def _row_hashes(table: TableName, frame: pd.DataFrame) -> pd.Series:
    keys = list(PRIMARY_KEYS[table])
    return pd.Series(
        pd.util.hash_pandas_object(frame, index=False).to_numpy(),
        index=pd.MultiIndex.from_frame(frame[keys]) if len(keys) > 1 else frame[keys[0]],
    )


# --------------------------------------------------------------------------------------------
# Targets
# --------------------------------------------------------------------------------------------


def test_target_counts_follow_the_rates(app_config) -> None:
    starter = target_counts(app_config, 50)
    assert set(starter.values()) == {1}
    full = target_counts(app_config, 5000)
    assert full[IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS] == 25  # 50 shipments, 25 pairs
    assert {
        full[i] for i in IssueType if i is not IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS
    } == {50}


def test_zero_rate_gives_no_anomaly(raw_config) -> None:
    raw_config["injection_rates"]["custody_gap_not_flagged"] = 0.0
    assert target_counts(parse_config(raw_config), 50)[IssueType.CUSTODY_GAP_NOT_FLAGGED] == 0


def test_every_issue_type_is_injected_at_starter(anomalies) -> None:
    per_type = anomalies.groupby("issue_type")["anomaly_id"].nunique()
    assert per_type.to_dict() == {issue.value: 1 for issue in IssueType}
    assert len(anomalies) == 6  # the pair anomaly has two rows


def test_targets_are_disjoint_normal_and_compliant(baseline, anomalies) -> None:
    natural = baseline[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    ships = anomalies["shipment_id"]
    assert ships.is_unique
    assert (natural.loc[ships, "scenario"] == "NORMAL").all()
    assert (natural.loc[ships, "expected_status"] == "COMPLIANT").all()


def test_targets_change_with_the_seed(app_config, baseline, anomalies) -> None:
    other = inject_tables(app_config.model_copy(update={"seed": 7}), baseline)
    assert other[TableName.ANOMALIES]["shipment_id"].tolist() != anomalies["shipment_id"].tolist()


def test_too_few_eligible_shipments_raises(raw_config, baseline) -> None:
    raw_config["injection_rates"] = {issue.value: 0.0 for issue in IssueType}
    raw_config["injection_rates"]["logger_not_assigned"] = 0.9  # 45 targets > 38 eligible
    with pytest.raises(InjectionError, match="logger_not_assigned"):
        inject_tables(parse_config(raw_config), baseline)


def test_missing_input_table_raises(app_config, baseline) -> None:
    partial = {k: v for k, v in baseline.items() if k is not TableName.CUSTODY_EVENTS}
    with pytest.raises(InjectionError, match="custody_events"):
        inject_tables(app_config, partial)


# --------------------------------------------------------------------------------------------
# Only target records change; IDs are immutable and traceable
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("table", list(raw_tables()), ids=lambda t: t.value)
def test_rows_of_other_shipments_are_unchanged(baseline, injected, anomalies, table) -> None:
    before, after = baseline[table], injected[table]
    targets = set(anomalies["shipment_id"])
    keep_before = ~_shipment_of(table, before).isin(targets)
    keep_after = ~_shipment_of(table, after).isin(targets)
    a = _row_hashes(table, before[keep_before].reset_index(drop=True))
    b = _row_hashes(table, after[keep_after].reset_index(drop=True))
    assert a.sort_index().equals(b.sort_index())


def test_only_injection_tables_change(baseline, injected) -> None:
    changed = {t for t in raw_tables() if not baseline[t].equals(injected[t])}
    assert changed == {
        TableName.SHIPMENTS,
        TableName.TEMPERATURE_READINGS,
        TableName.CUSTODY_EVENTS,
    }
    assert set(injected) == set(INJECT_OUTPUTS)  # device_truth is neither read nor rewritten


def test_every_changed_record_is_listed_in_the_ground_truth(baseline, injected, anomalies) -> None:
    """A changed row's ID (or, for readings, its logger ID) is in its anomaly's ID lists."""
    listed = {
        ship: set(split_list(row.affected_record_ids)) | set(split_list(row.removed_record_ids))
        for ship, row in anomalies.set_index("shipment_id").iterrows()
    }
    for table in raw_tables():
        key = PRIMARY_KEYS[table][0]
        a, b = _row_hashes(table, baseline[table]), _row_hashes(table, injected[table])
        changed = set(a.index.symmetric_difference(b.index))
        common = a.index.intersection(b.index)
        changed |= set(common[a.loc[common].to_numpy() != b.loc[common].to_numpy()])
        if not changed:
            continue
        assert len(PRIMARY_KEYS[table]) == 1, table
        frame = pd.concat([baseline[table], injected[table]]).drop_duplicates(key)
        rows = frame.set_index(key).loc[sorted(changed)].reset_index()
        for _, row in rows.iterrows():
            ship = _shipment_of(table, rows.loc[[_]]).iloc[0]
            names = {row[key], row.get("logger_id")}
            assert names & listed[ship], f"{table.value} {row[key]} is not listed for {ship}"


def test_affected_ids_exist_and_removed_ids_are_gone(baseline, injected, anomalies) -> None:
    def all_ids(tables) -> set[str]:
        ids: set[str] = set()
        for table in raw_tables():
            frame = tables[table]
            for column in frame.columns:
                if column.endswith("_id"):
                    ids.update(frame[column].dropna())
        return ids

    before, after = all_ids(baseline), all_ids(injected)
    for row in anomalies.itertuples():
        assert set(split_list(row.affected_record_ids)) <= after, row.anomaly_id
        removed = set(split_list(row.removed_record_ids))
        assert removed <= before, row.anomaly_id
        assert not removed & after, row.anomaly_id


@pytest.mark.parametrize(
    "table",
    [TableName.TEMPERATURE_READINGS, TableName.CUSTODY_EVENTS],
    ids=lambda t: t.value,
)
def test_surviving_ids_are_never_renumbered(baseline, injected, anomalies, table) -> None:
    key = PRIMARY_KEYS[table][0]
    removed = {i for ids in anomalies["removed_record_ids"] for i in split_list(ids)}
    survivors = set(baseline[table][key]) - removed
    assert set(injected[table][key]) == survivors


def test_no_label_column_leaks_into_raw_tables(injected) -> None:
    labels = {"scenario", "issue_type", "anomaly_id", "expected_status", "systematic_offset_c"}
    for table in raw_tables():
        assert TABLE_LAYERS[table] is Layer.RAW
        assert list(injected[table].columns) == list(SCHEMAS[table].columns)
        assert not labels & set(injected[table].columns), table


def test_injection_is_deterministic(app_config, baseline, injected) -> None:
    again = inject_tables(app_config, baseline)
    assert set(again) == set(injected)
    for name, frame in injected.items():
        pd.testing.assert_frame_equal(again[name], frame, check_exact=True)


def test_expected_outcomes_and_anomalies_agree(injected, anomalies) -> None:
    outcomes = injected[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    labelled = outcomes[outcomes["issue_type"].notna()]
    assert sorted(labelled.index) == sorted(anomalies["shipment_id"])
    rows = anomalies.set_index("shipment_id").loc[labelled.index]
    assert (rows["issue_type"] == labelled["issue_type"]).all()
    for column in ("expected_status", "expected_reason_codes", "expected_excursion_durations_min"):
        assert rows[column].fillna("").equals(labelled[column].fillna("")), column
    assert (labelled["scenario"] == "NORMAL").all()


def test_anomaly_ids_are_numbered_in_issue_type_order(anomalies) -> None:
    order = anomalies.drop_duplicates("anomaly_id").sort_values("anomaly_id")
    assert order["anomaly_id"].tolist() == [f"ANOM-{k:06d}" for k in range(1, 6)]
    assert order["issue_type"].tolist() == [issue.value for issue in IssueType]


def test_injector_never_imports_the_engine() -> None:
    """Expected values come from the documented rules, never from the compliance engine."""
    for path in sorted(INJECT_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module]
        modules += [a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names]
        assert not [m for m in modules if m.startswith("coldchain.compliance")], path.name


# --------------------------------------------------------------------------------------------
# One test group per issue type
# --------------------------------------------------------------------------------------------


def _readings_of(tables, ship: str) -> pd.DataFrame:
    readings = tables[TableName.TEMPERATURE_READINGS]
    return readings[readings["logger_id"] == f"LOG-{ship}-01"].sort_values("recorded_at")


def _logger(tables, ship: str) -> pd.Series:
    return tables[TableName.LOGGERS].set_index("shipment_id").loc[ship]


def test_logger_not_assigned(baseline, injected, anomalies) -> None:
    (ship,) = _targets(anomalies, IssueType.LOGGER_NOT_ASSIGNED)
    log = _logger(injected, ship)
    devices = _readings_of(injected, ship)["device_id"].unique()
    assert len(devices) == 1 and devices[0] != log["device_id"]
    others = injected[TableName.LOGGERS]
    overlap = others[
        (others["device_id"] == devices[0])
        & (others["assigned_at"] <= log["unassigned_at"])
        & (others["unassigned_at"] >= log["assigned_at"])
    ]
    assert overlap.empty, "the replacement device has no assignment in that window"
    assert set(_readings_of(injected, ship)["reading_id"]) == set(
        _readings_of(baseline, ship)["reading_id"]
    )
    row = anomalies.set_index("shipment_id").loc[ship]
    assert row["expected_reason_codes"] == (
        "LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE|CARRIER_STATUS_CONTRADICTED"
    )
    assert row["expected_status"] == "INCONCLUSIVE"


def test_product_threshold_mismatch_passes_under_the_declared_policy(
    app_config, injected, anomalies
) -> None:
    (ship,) = _targets(anomalies, IssueType.PRODUCT_THRESHOLD_MISMATCH)
    policies = injected[TableName.THRESHOLD_POLICIES].set_index("policy_id")
    declared = policies.loc[
        injected[TableName.SHIPMENTS].set_index("shipment_id").loc[ship, "declared_policy_id"]
    ]
    outcome = injected[TableName.EXPECTED_OUTCOMES].set_index("shipment_id").loc[ship]
    applied = policies.loc[
        check_tables(app_config, injected)[TableName.COMPLIANCE_DECISIONS]
        .set_index("shipment_id")
        .loc[ship, "applied_policy_id"]
    ]
    assert declared["policy_code"] != applied["policy_code"]
    device = (
        injected[TableName.DEVICES].set_index("device_id").loc[_logger(injected, ship)["device_id"]]
    )
    t = _readings_of(injected, ship)["temperature_c"].to_numpy()
    u = device["uncertainty_c"]
    # Fully compliant under the declared policy, not even near one of its limits ...
    assert (t - u > declared["min_c"]).all() and (t + u < declared["max_c"]).all()
    # ... but every reading is out of range under the product's policy, never beyond abs limits.
    out = (t > applied["max_c"] + u).all() or (t < applied["min_c"] - u).all()
    assert out
    assert ((t <= applied["abs_max_c"]) & (t >= applied["abs_min_c"])).all()
    assert outcome["expected_status"] == "NON_COMPLIANT"
    assert split_list(outcome["expected_reason_codes"]) == [
        "SINGLE_DURATION_BREACH",
        "CUMULATIVE_DURATION_BREACH",
        "POLICY_MISMATCH_DECLARED",
        "CARRIER_STATUS_CONTRADICTED",
    ]


def test_product_threshold_mismatch_fools_a_checker_that_trusts_the_declaration(
    app_config, injected, anomalies
) -> None:
    """Relabel the products with the declared class: the real engine then releases the shipment."""
    (ship,) = _targets(anomalies, IssueType.PRODUCT_THRESHOLD_MISMATCH)
    tables = dict(injected)
    declared_id = (
        tables[TableName.SHIPMENTS].set_index("shipment_id").loc[ship, "declared_policy_id"]
    )
    declared_code = (
        tables[TableName.THRESHOLD_POLICIES].set_index("policy_id").loc[declared_id, "policy_code"]
    )
    lots = tables[TableName.SHIPMENT_LOTS]
    products = (
        tables[TableName.LOTS]
        .set_index("lot_id")
        .loc[lots.loc[lots["shipment_id"] == ship, "lot_id"], "product_id"]
    )
    relabelled = tables[TableName.PRODUCTS].copy()
    relabelled.loc[relabelled["product_id"].isin(products), "policy_code"] = declared_code
    tables[TableName.PRODUCTS] = relabelled
    naive = check_tables(app_config, tables)[TableName.COMPLIANCE_DECISIONS].set_index(
        "shipment_id"
    )
    assert naive.loc[ship, "status"] == "COMPLIANT"
    assert naive.loc[ship, "decided_action"] == "RELEASE"


def test_missing_data_silently_passed(app_config, baseline, injected, anomalies) -> None:
    (ship,) = _targets(anomalies, IssueType.MISSING_DATA_SILENTLY_PASSED)
    row = anomalies.set_index("shipment_id").loc[ship]
    removed = split_list(row["removed_record_ids"])
    before = _readings_of(baseline, ship).reset_index(drop=True)
    after = _readings_of(injected, ship)
    positions = before.index[before["reading_id"].isin(removed)]
    assert len(positions) == len(removed) >= 3
    assert (np.diff(positions) == 1).all(), "one contiguous block"
    assert positions.min() > 0 and positions.max() < len(before) - 1, "strictly inside the window"
    assert len(after) == len(before) - len(removed)
    gap = after["recorded_at"].diff().dt.total_seconds().max() / 60
    assert gap == (len(removed) + 1) * app_config.reading_interval_min > 30
    shipments = injected[TableName.SHIPMENTS].set_index("shipment_id")
    assert shipments.loc[ship, "carrier_reported_status"] == "PASS"
    assert set(split_list(row["affected_record_ids"])) == set(
        before.loc[[positions.min() - 1, positions.max() + 1], "reading_id"]
    )
    assert "DATA_GAP" in split_list(row["expected_reason_codes"])
    assert "CARRIER_STATUS_CONTRADICTED" in split_list(row["expected_reason_codes"])
    assert row["expected_status"] == "INCONCLUSIVE"


def test_custody_gap_not_flagged(baseline, injected, anomalies) -> None:
    (ship,) = _targets(anomalies, IssueType.CUSTODY_GAP_NOT_FLAGGED)
    row = anomalies.set_index("shipment_id").loc[ship]
    events = baseline[TableName.CUSTODY_EVENTS].set_index("custody_event_id")
    (deleted,) = split_list(row["removed_record_ids"])
    assert events.loc[deleted, "event_type"] == "HANDOFF"
    chain = injected[TableName.CUSTODY_EVENTS]
    chain = chain[chain["shipment_id"] == ship].sort_values("sequence")
    seq = events.loc[deleted, "sequence"]
    assert seq not in set(chain["sequence"])
    assert len(chain) == len(events[events["shipment_id"] == ship]) - 1
    previous = chain[chain["sequence"] == seq - 1].iloc[0]
    following = chain[chain["sequence"] == seq + 1].iloc[0]
    assert previous["to_party_id"] != following["from_party_id"]
    assert row["expected_reason_codes"] == "CUSTODY_GAP|CARRIER_STATUS_CONTRADICTED"


def test_excursion_across_unrelated_shipments(app_config, injected, anomalies) -> None:
    rows = anomalies[
        anomalies["issue_type"] == IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value
    ].set_index("shipment_id")
    assert rows["anomaly_id"].nunique() == 1
    a, b = sorted(rows.index)
    pairs = back_to_back_pairs(injected[TableName.LOGGERS])
    assert ((pairs["first_shipment_id"] == a) & (pairs["second_shipment_id"] == b)).any(), (
        "B is the device's very next assignment after A"
    )
    assert _logger(injected, a)["device_id"] == _logger(injected, b)["device_id"]
    idle = (
        _logger(injected, b)["assigned_at"] - _logger(injected, a)["unassigned_at"]
    ).total_seconds()
    assert (rows["idle_min"] == idle // 60).all()

    policies = injected[TableName.THRESHOLD_POLICIES].set_index("policy_id")
    decisions = check_tables(app_config, injected)[TableName.COMPLIANCE_DECISIONS]
    decisions = decisions.set_index("shipment_id")

    def high(ship: str) -> np.ndarray:
        limit = policies.loc[decisions.loc[ship, "applied_policy_id"], "max_c"]
        return _readings_of(injected, ship)["temperature_c"].to_numpy() > limit

    run_a = int(np.argmin(high(a)[::-1]))  # trailing out-of-range readings of A
    run_b = int(np.argmin(high(b)))  # leading out-of-range readings of B
    assert run_a >= 1 and run_b >= 1
    interval = app_config.reading_interval_min
    allowance = {
        s: policies.loc[decisions.loc[s, "applied_policy_id"], "max_single_excursion_min"]
        for s in (a, b)
    }
    assert int(rows.loc[a, "expected_excursion_durations_min"]) == run_a * interval <= allowance[a]
    assert int(rows.loc[b, "expected_excursion_durations_min"]) == run_b * interval <= allowance[b]
    # A naive engine grouping by device would see one run beyond the larger allowance.
    assert (run_a + run_b) * interval > max(allowance.values())
    for ship in (a, b):
        assert rows.loc[ship, "expected_reason_codes"] == "EXCURSION_WITHIN_ALLOWANCE"
        assert rows.loc[ship, "expected_status"] == "COMPLIANT_WITH_WARNINGS"


def test_engine_flags_every_injected_anomaly(app_config, injected, anomalies) -> None:
    """BACKLOG #2: the engine completes on injected data and reaches the expected verdict."""
    decisions = check_tables(app_config, injected)[TableName.COMPLIANCE_DECISIONS]
    decisions = decisions.set_index("shipment_id").loc[anomalies["shipment_id"]]
    assert decisions["status"].tolist() == anomalies["expected_status"].tolist()
    assert decisions["reason_codes"].fillna("").tolist() == (
        anomalies["expected_reason_codes"].fillna("").tolist()
    )
    contradicted = (
        decisions["reason_codes"]
        .fillna("")
        .str.contains(ReasonCode.CARRIER_STATUS_CONTRADICTED.value)
    )
    by_type = dict(zip(anomalies["issue_type"], contradicted, strict=True))
    assert by_type[IssueType.MISSING_DATA_SILENTLY_PASSED.value]


# --------------------------------------------------------------------------------------------
# Rates, disjoint pairs and failure paths
# --------------------------------------------------------------------------------------------


def test_a_zero_rate_issue_type_is_skipped(raw_config, baseline) -> None:
    raw_config["injection_rates"]["product_threshold_mismatch"] = 0.0
    result = inject_tables(parse_config(raw_config), baseline)
    types = set(result[TableName.ANOMALIES]["issue_type"])
    assert types == {i.value for i in IssueType} - {"product_threshold_mismatch"}


def test_several_pairs_never_share_a_shipment(raw_config, baseline) -> None:
    raw_config["injection_rates"] = {issue.value: 0.0 for issue in IssueType}
    raw_config["injection_rates"]["excursion_across_unrelated_shipments"] = 0.2  # 5 pairs
    config = parse_config(raw_config)
    result = inject_tables(config, baseline)
    rows = result[TableName.ANOMALIES]
    assert rows["anomaly_id"].nunique() == 5 and rows["shipment_id"].is_unique
    pairs = back_to_back_pairs(result[TableName.LOGGERS])
    consecutive = set(zip(pairs["first_shipment_id"], pairs["second_shipment_id"], strict=True))
    for _, pair in rows.groupby("anomaly_id"):
        assert tuple(sorted(pair["shipment_id"])) in consecutive
    decisions = check_tables(config, result)[TableName.COMPLIANCE_DECISIONS]
    decided = decisions.set_index("shipment_id").loc[rows["shipment_id"], "reason_codes"]
    assert decided.tolist() == rows["expected_reason_codes"].tolist()


def test_logger_injector_needs_a_free_device(app_config, baseline) -> None:
    applied = None  # not used by this injector
    targets = select_targets(
        app_config,
        baseline,
        baseline[TableName.EXPECTED_OUTCOMES],
        applied_policies(
            *(
                baseline[t]
                for t in (
                    TableName.SHIPMENTS,
                    TableName.SHIPMENT_LOTS,
                    TableName.LOTS,
                    TableName.PRODUCTS,
                    TableName.THRESHOLD_POLICIES,
                )
            )
        ),
        np.random.default_rng(0),
    )
    chosen = targets[targets["issue_type"] == IssueType.LOGGER_NOT_ASSIGNED.value]
    ship = chosen["shipment_id"].iloc[0]
    tables = dict(baseline)
    own = tables[TableName.LOGGERS].set_index("shipment_id").loc[ship, "device_id"]
    devices = tables[TableName.DEVICES]
    tables[TableName.DEVICES] = devices[devices["device_id"] == own]
    with pytest.raises(InjectionError, match="No device is free"):
        inject_logger_not_assigned(app_config, tables, chosen, applied, np.random.default_rng(0))


def test_reshape_needs_a_complete_stream(app_config, baseline) -> None:
    loggers = baseline[TableName.LOGGERS].iloc[:1]
    readings = baseline[TableName.TEMPERATURE_READINGS]
    mine = readings[readings["logger_id"] == loggers["logger_id"].iloc[0]]
    with pytest.raises(GenerationError, match="complete reading streams"):
        reshape_segments(
            app_config,
            loggers,
            mine.iloc[1:],
            pd.DataFrame(),
            baseline[TableName.DEVICES],
            pd.DataFrame(),
            np.random.default_rng(0),
        )


def test_pair_has_the_shortest_idle_time_of_all_eligible_pairs(baseline, anomalies) -> None:
    """DECISIONS.md #83: the device goes straight from one trip to the next."""
    natural = baseline[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    ok = (natural["scenario"] == "NORMAL") & (natural["expected_status"] == "COMPLIANT")
    pairs = back_to_back_pairs(baseline[TableName.LOGGERS])
    eligible = pairs[
        ok.loc[pairs["first_shipment_id"]].to_numpy()
        & ok.loc[pairs["second_shipment_id"]].to_numpy()
    ]
    rows = anomalies[
        anomalies["issue_type"] == IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value
    ]
    assert rows["idle_min"].nunique() == 1
    assert rows["idle_min"].iloc[0] == eligible["idle_min"].min()
    others = anomalies[
        anomalies["issue_type"] != IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value
    ]
    assert others["idle_min"].isna().all()


def test_several_pairs_are_taken_in_order_of_idle_time(raw_config, baseline) -> None:
    raw_config["injection_rates"] = {issue.value: 0.0 for issue in IssueType}
    raw_config["injection_rates"]["excursion_across_unrelated_shipments"] = 0.2  # 5 pairs
    rows = inject_tables(parse_config(raw_config), baseline)[TableName.ANOMALIES]
    idle = rows.drop_duplicates("anomaly_id")["idle_min"]
    natural = baseline[TableName.EXPECTED_OUTCOMES].set_index("shipment_id")
    ok = (natural["scenario"] == "NORMAL") & (natural["expected_status"] == "COMPLIANT")
    pairs = back_to_back_pairs(baseline[TableName.LOGGERS])
    eligible = pairs[
        ok.loc[pairs["first_shipment_id"]].to_numpy()
        & ok.loc[pairs["second_shipment_id"]].to_numpy()
    ]
    # No unchosen eligible pair is shorter than the longest chosen one unless it shares a shipment.
    chosen = set(rows["shipment_id"])
    shorter = eligible[eligible["idle_min"] < idle.max()]
    overlaps = shorter["first_shipment_id"].isin(chosen) | shorter["second_shipment_id"].isin(
        chosen
    )
    assert overlaps.all()
