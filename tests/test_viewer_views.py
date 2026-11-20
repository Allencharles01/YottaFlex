"""Pure viewer code (Phase 9): table transforms, file discovery and the filtered reader."""

import pandas as pd
import pytest

from coldchain.enums import ComplianceStatus, FileFormat, ReasonCode, TableName
from coldchain.errors import TableIOError
from coldchain.io import read_table_rows, write_table
from coldchain.viewer import data, views
from conftest import LOG, SHP, WINDOW_TIMES, build_valid_tables, readings_frame

UTC = "UTC"


def _decisions(rows: list[tuple[str, str, str | None]]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "shipment_id": [r[0] for r in rows],
            "status": [r[1] for r in rows],
            "reason_codes": [r[2] for r in rows],
            "coverage_pct": [100.0] * len(rows),
            "max_gap_min": [10] * len(rows),
            "decided_action": ["RELEASE"] * len(rows),
        }
    )


def _ts(text: str) -> pd.Timestamp:
    return pd.Timestamp(text, tz=UTC)


# --------------------------------------------------------------------------------------------
# io.read_table_rows
# --------------------------------------------------------------------------------------------


@pytest.fixture
def readings_dir(tmp_path):
    frame = pd.concat(
        [
            readings_frame(LOG, "DEV-000001", WINDOW_TIMES),
            readings_frame("LOG-SHP-2026-000002-01", "DEV-000002", WINDOW_TIMES[:3]),
        ],
        ignore_index=True,
    )
    write_table(TableName.TEMPERATURE_READINGS, frame, tmp_path, (FileFormat.PARQUET,))
    return tmp_path


def test_read_table_rows_returns_only_the_requested_loggers(readings_dir) -> None:
    rows = read_table_rows(TableName.TEMPERATURE_READINGS, readings_dir, "logger_id", [LOG])
    assert len(rows) == len(WINDOW_TIMES)
    assert set(rows["logger_id"]) == {LOG}
    assert str(rows["recorded_at"].dtype) == "datetime64[us, UTC]"


def test_read_table_rows_with_no_values_gives_an_empty_frame_with_the_schema(readings_dir) -> None:
    rows = read_table_rows(TableName.TEMPERATURE_READINGS, readings_dir, "logger_id", [])
    assert rows.empty
    assert list(rows.columns) == [
        "reading_id",
        "logger_id",
        "device_id",
        "recorded_at",
        "temperature_c",
    ]


def test_read_table_rows_rejects_an_unknown_column_and_a_missing_file(readings_dir, tmp_path):
    with pytest.raises(TableIOError, match="no column"):
        read_table_rows(TableName.TEMPERATURE_READINGS, readings_dir, "nope", ["x"])
    with pytest.raises(TableIOError, match="not found"):
        read_table_rows(TableName.SHIPMENTS, tmp_path / "empty", "shipment_id", ["x"])


# --------------------------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------------------------


def test_missing_files_lists_every_required_file_of_an_empty_root(tmp_path) -> None:
    missing = data.missing_files(tmp_path, "starter")
    assert missing == data.required_files(tmp_path, "starter")
    assert len(missing) == len(data.VIEWER_TABLES) + 2
    assert all(str(path).startswith(str(tmp_path / "starter")) for path in missing)


def test_file_signature_and_optional_documents(tmp_path) -> None:
    assert data.file_signature(tmp_path / "missing.json") is None
    path = tmp_path / "doc.json"
    path.write_text('{"a": 1}\n', encoding="utf-8")
    assert data.file_signature(path)[1] == path.stat().st_size
    assert data.load_json_if_present(path) == {"a": 1}
    assert data.load_json_if_present(tmp_path / "absent.json") is None
    documents = data.optional_documents(tmp_path, "full")
    assert set(documents) == {"baseline_validation", "reproducibility", "run_summary", "manifest"}
    assert documents["baseline_validation"].parent.name == "baseline"


# --------------------------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------------------------


def test_kpis_count_compliant_with_warnings_as_compliant() -> None:
    decisions = _decisions(
        [
            ("S1", "COMPLIANT", None),
            ("S2", "COMPLIANT_WITH_WARNINGS", "LONG_DWELL"),
            ("S3", "NON_COMPLIANT", "SINGLE_DURATION_BREACH"),
            ("S4", "INCONCLUSIVE", "DATA_GAP|LOW_COVERAGE"),
        ]
    )
    tiles = views.kpis(decisions, pd.DataFrame({"report_id": ["E1", "E2"]}))
    assert tiles == {
        "shipments": 4,
        "compliant": 2,
        "compliant_pct": 50.0,
        "non_compliant": 1,
        "inconclusive": 1,
        "excursions": 2,
    }


def test_kpis_and_status_counts_handle_no_shipments() -> None:
    empty = _decisions([])
    assert views.kpis(empty, pd.DataFrame({"report_id": []}))["compliant_pct"] == 0.0
    counts = views.status_counts(empty)
    assert list(counts["status"]) == [s.value for s in ComplianceStatus]
    assert counts["shipments"].sum() == 0
    assert views.reason_code_counts(empty).empty


def test_status_counts_keep_severity_order_and_zero_statuses() -> None:
    counts = views.status_counts(_decisions([("S1", "NON_COMPLIANT", None)] * 3))
    assert list(counts["shipments"]) == [0, 0, 0, 3]
    assert list(counts["share_pct"]) == [0.0, 0.0, 0.0, 100.0]


def test_reason_code_counts_count_each_shipment_once_per_code() -> None:
    decisions = _decisions(
        [("S1", "INCONCLUSIVE", "DATA_GAP|LOW_COVERAGE"), ("S2", "INCONCLUSIVE", "DATA_GAP")]
    )
    counts = views.reason_code_counts(decisions)
    assert counts.to_dict("records") == [
        {"reason_code": "DATA_GAP", "shipments": 2},
        {"reason_code": "LOW_COVERAGE", "shipments": 1},
    ]


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, []), (float("nan"), []), (pd.NA, []), ("", []), ("A|B", ["A", "B"]), ("A", ["A"])],
)
def test_split_codes(value, expected) -> None:
    assert views.split_codes(value) == expected


def test_field_table_renders_every_value_as_text() -> None:
    table = views.field_table({"a": 1, "b": None, "c": 2.5})
    assert table.to_dict("list") == {"field": ["a", "b", "c"], "value": ["1", "", "2.5"]}
    assert views.field_table(None).empty


# --------------------------------------------------------------------------------------------
# Shipment details
# --------------------------------------------------------------------------------------------


def test_shipment_details_join_lots_products_parties_and_history() -> None:
    tables = build_valid_tables()
    lots = views.shipment_lots_detail(
        SHP, tables[TableName.SHIPMENT_LOTS], tables[TableName.LOTS], tables[TableName.PRODUCTS]
    )
    assert {"lot_id", "shipped_units", "product_name", "policy_code"} <= set(lots.columns)
    assert len(lots) == len(tables[TableName.SHIPMENT_LOTS])

    named = views.with_party_names(
        tables[TableName.SHIPMENTS], tables[TableName.PARTIES], ["supplier_party_id"]
    )
    position = list(named.columns).index("supplier_party_id")
    assert named.columns[position + 1] == "supplier_party_name"
    assert named["supplier_party_name"].iloc[0] == "Hyderabad Biologics Ltd"

    raw = tables[TableName.SHIPMENT_STATUS_EVENTS]
    engine = raw.head(1).assign(sequence=raw["sequence"].max() + 1, status="UNDER_REVIEW")
    history = views.status_history(raw, engine, SHP)
    assert list(history["source"]) == ["raw"] * len(raw) + ["compliance engine"]
    assert history["sequence"].is_monotonic_increasing


def test_assignment_window_uses_the_logger_and_device() -> None:
    tables = build_valid_tables()
    window = views.assignment_window(
        SHP, tables[TableName.SHIPMENTS], tables[TableName.LOGGERS], tables[TableName.DEVICES], 15
    )
    logger = tables[TableName.LOGGERS].iloc[0]
    assert window["has_logger"]
    assert window["logger_id"] == LOG
    assert window["window_start"] == logger["assigned_at"]
    assert window["interval_min"] == 10  # the device's interval, not the config default


def test_assignment_window_falls_back_to_the_trip_without_a_logger() -> None:
    tables = build_valid_tables()
    shipment = tables[TableName.SHIPMENTS].iloc[0]
    window = views.assignment_window(
        SHP,
        tables[TableName.SHIPMENTS],
        tables[TableName.LOGGERS].head(0),
        tables[TableName.DEVICES],
        15,
    )
    assert not window["has_logger"]
    assert window["device_id"] is None
    assert window["window_start"] == shipment["actual_departure_at"]
    assert window["window_end"] == shipment["actual_arrival_at"]
    assert window["interval_min"] == 15
    with pytest.raises(KeyError):
        views.assignment_window(
            "SHP-2026-999999",
            tables[TableName.SHIPMENTS],
            tables[TableName.LOGGERS],
            tables[TableName.DEVICES],
            10,
        )


def test_classify_readings() -> None:
    window = {
        "window_start": WINDOW_TIMES[1],
        "window_end": WINDOW_TIMES[-2],
        "device_id": "DEV-000001",
    }
    readings = readings_frame(LOG, "DEV-000001", WINDOW_TIMES)
    readings.loc[5, "device_id"] = "DEV-000009"
    classes = views.classify_readings(readings, window)["reading_class"]
    assert classes.iloc[0] == classes.iloc[-1] == views.ReadingClass.OUTSIDE_WINDOW
    assert classes.iloc[5] == views.ReadingClass.WRONG_DEVICE
    assert (classes == views.ReadingClass.TRUSTED).sum() == len(WINDOW_TIMES) - 3
    # the window ends are inclusive
    assert classes.iloc[1] == classes.iloc[-2] == views.ReadingClass.TRUSTED


# --------------------------------------------------------------------------------------------
# Gaps and excursions
# --------------------------------------------------------------------------------------------

START, END = WINDOW_TIMES[0], WINDOW_TIMES[-1]


def test_a_complete_stream_has_no_gap() -> None:
    assert views.gap_spans(pd.Series(WINDOW_TIMES), START, END, 10, 30).empty


def test_an_internal_gap_is_reported_from_last_to_next_reading() -> None:
    times = pd.Series(WINDOW_TIMES).drop(index=[5, 6, 7]).reset_index(drop=True)
    gaps = views.gap_spans(times, START, END, 10, 30)
    assert gaps.to_dict("records") == [
        {"gap_start": WINDOW_TIMES[4], "gap_end": WINDOW_TIMES[8], "gap_min": 40, "breach": True}
    ]


def test_a_gap_of_exactly_the_allowed_length_is_not_a_breach() -> None:
    times = pd.Series(WINDOW_TIMES).drop(index=[5, 6]).reset_index(drop=True)
    gaps = views.gap_spans(times, START, END, 10, 30)
    assert list(gaps["gap_min"]) == [30]
    assert not gaps["breach"].any()


def test_no_trusted_reading_makes_the_whole_window_one_gap() -> None:
    empty = pd.Series([], dtype="datetime64[us, UTC]")
    gaps = views.gap_spans(empty, START, END, 10, 30)
    window_min = int((END - START).total_seconds() // 60)
    assert list(gaps["gap_min"]) == [window_min + 20]  # padded by one interval each side
    assert gaps["breach"].all()


def test_a_late_start_shows_a_gap_at_the_window_start() -> None:
    times = pd.Series(WINDOW_TIMES[3:]).reset_index(drop=True)
    gaps = views.gap_spans(times, START, END, 10, 30)
    assert list(gaps["gap_min"]) == [40]
    assert gaps["gap_start"].iloc[0] == START - pd.Timedelta(minutes=10)


def test_excursion_spans_cover_the_duration() -> None:
    reports = pd.DataFrame(
        {
            "report_id": ["E2", "E1"],
            "started_at": [_ts("2026-03-10 10:00"), _ts("2026-03-10 08:00")],
            "ended_at": [_ts("2026-03-10 10:40"), _ts("2026-03-10 08:00")],
            "duration_min": [50, 10],
        }
    )
    spans = views.excursion_spans(reports, 10)
    assert list(spans["report_id"]) == ["E1", "E2"]
    minutes = (spans["span_end"] - spans["started_at"]).dt.total_seconds() // 60
    assert list(minutes) == list(spans["duration_min"])


def test_excursion_table_and_report_readings() -> None:
    reports = pd.DataFrame({"report_id": ["E1"], "shipment_id": ["S1"]})
    table = views.excursion_table(reports, _decisions([("S1", "NON_COMPLIANT", None)]))
    assert table["shipment_status"].tolist() == ["NON_COMPLIANT"]

    readings = readings_frame(LOG, "DEV-000001", WINDOW_TIMES).iloc[::-1]
    links = pd.DataFrame(
        {"report_id": ["E1", "E1", "E2"], "reading_id": readings["reading_id"].iloc[:3].tolist()}
    )
    linked = views.report_readings("E1", links, readings)
    assert len(linked) == 2
    assert linked["recorded_at"].is_monotonic_increasing


# --------------------------------------------------------------------------------------------
# Custody chain
# --------------------------------------------------------------------------------------------


def _chain_inputs():
    tables = build_valid_tables()
    return tables[TableName.CUSTODY_EVENTS], tables[TableName.SHIPMENTS].iloc[0]


def test_a_valid_custody_chain_has_no_problem() -> None:
    events, shipment = _chain_inputs()
    chain = views.custody_chain(events, shipment)
    assert chain["link_ok"].all()
    assert (chain["problem"] == "").all()


def test_a_deleted_handoff_breaks_the_chain() -> None:
    events, shipment = _chain_inputs()
    events = events[events["sequence"] != 2]
    chain = views.custody_chain(events, shipment)
    problems = chain.loc[~chain["link_ok"], "problem"].iloc[0]
    assert "sequence skips" in problems
    assert "previous event's to-party" in problems


def test_custody_chain_endpoints_are_checked() -> None:
    events, shipment = _chain_inputs()
    events = events.copy()
    events.loc[events["sequence"] == 1, "event_type"] = "HANDOFF"
    events.loc[events["sequence"] == events["sequence"].max(), "to_party_id"] = "PTY-0002"
    chain = views.custody_chain(events, shipment)
    assert "PICKUP" in chain["problem"].iloc[0]
    assert "DELIVERY" in chain["problem"].iloc[-1]


def test_custody_chain_of_a_shipment_without_events_is_empty() -> None:
    events, shipment = _chain_inputs()
    assert views.custody_chain(events.head(0), shipment).empty


# --------------------------------------------------------------------------------------------
# Gaps page and data quality
# --------------------------------------------------------------------------------------------


def test_flagged_shipments_keep_only_gap_codes() -> None:
    decisions = _decisions(
        [
            ("S1", "COMPLIANT", None),
            ("S2", "INCONCLUSIVE", "CUSTODY_GAP"),
            ("S3", "INCONCLUSIVE", "LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE"),
            ("S4", "COMPLIANT_WITH_WARNINGS", "LONG_DWELL"),
        ]
    )
    flagged = views.flagged_shipments(decisions)
    assert list(flagged["shipment_id"]) == ["S2", "S3"]
    assert flagged[ReasonCode.CUSTODY_GAP.value].tolist() == [True, False]
    assert flagged[ReasonCode.DATA_GAP.value].tolist() == [False, True]
    assert views.flagged_shipments(decisions.head(1)).empty


def test_answer_key_comparison_prefixes_ground_truth_and_ignores_code_order() -> None:
    expected = pd.DataFrame(
        {
            "shipment_id": ["S1", "S2"],
            "scenario": ["NORMAL", "NORMAL"],
            "issue_type": [None, "custody_gap_not_flagged"],
            "expected_status": ["COMPLIANT", "INCONCLUSIVE"],
            "expected_reason_codes": [None, "CUSTODY_GAP|CARRIER_STATUS_CONTRADICTED"],
        }
    )
    decisions = _decisions(
        [
            ("S1", "COMPLIANT", None),
            ("S2", "INCONCLUSIVE", "CARRIER_STATUS_CONTRADICTED|CUSTODY_GAP"),
        ]
    )
    frame = views.answer_key_comparison(expected, decisions)
    assert frame["status_match"].all()
    assert frame["reason_codes_match"].all()
    ground_truth = [c for c in frame.columns if c.startswith(views.ANSWER_KEY_PREFIX)]
    assert len(ground_truth) == 4
    assert not {"expected_status", "scenario", "issue_type"} & set(frame.columns)

    decisions.loc[1, "status"] = "COMPLIANT"
    assert not views.answer_key_comparison(expected, decisions)["status_match"].iloc[1]


def test_confusion_and_check_tables() -> None:
    block = {
        "confusion": {
            "COMPLIANT": {"COMPLIANT": 3, "NO_DECISION": 0},
            "INCONCLUSIVE": {"COMPLIANT": 1},
        }
    }
    confusion = views.confusion_frame(block)
    assert confusion.loc["COMPLIANT", "COMPLIANT"] == 3
    assert confusion.loc["INCONCLUSIVE", "NO_DECISION"] == 0

    summary = {"checks": [{"check_id": "a", "status": "PASS"}, {"check_id": "b", "status": "FAIL"}]}
    assert len(views.check_table(summary, only_problems=False)) == 2
    assert views.check_table(summary, only_problems=True)["check_id"].tolist() == ["b"]
    assert views.check_table({}, only_problems=True).empty


def test_reproducibility_overview_reads_the_report_and_tolerates_missing_parts() -> None:
    payload = {
        "verdict": "PASS",
        "seed": 1,
        "control_seed": 2,
        "totals": {"files": 3, "matching": 3, "mismatching": 0},
        "control": {"changed": 2, "passed": True},
        "runs": {"run1": {"dataset_sha256": "x"}, "run2": {"dataset_sha256": "x"}},
    }
    overview = views.reproducibility_overview(payload)
    assert overview["matching"] == overview["files"] == 3
    assert overview["dataset_sha256_run1"] == overview["dataset_sha256_run2"] == "x"
    assert views.reproducibility_overview({})["verdict"] is None
