"""The streamlit viewer end to end with ``AppTest`` (Phase 9).

* every page renders without an exception on real starter-scale output (``run-all``) and shows
  what the engine wrote;
* the pages link to each other (an excursion report opens its shipment's temperature chart);
* everything from ``ground_truth/`` is labelled "Answer key (ground truth)";
* with no or incomplete output, every page shows "Run `coldchain run-all --scale <scale>`
  first";
* the viewer never writes: every file under the output root is byte-identical, with the same
  modification time, after clicking through every page; a static scan finds no write call.
"""

import ast
import hashlib
import shutil
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

from coldchain.cli import app as cli
from coldchain.config import PROJECT_ROOT
from coldchain.enums import TableName
from coldchain.viewer import charts, data, session, views
from plotly_checks import (
    assert_date_chart,
    assert_labels_do_not_overlap,
    spec_of,
    window_labels,
)

APP = PROJECT_ROOT / "app" / "streamlit_app.py"
PAGES = {
    "views/overview.py": "Overview",
    "views/shipment_explorer.py": "Shipment Explorer",
    "views/temperature_chart.py": "Temperature Chart",
    "views/excursion_reports.py": "Excursion Reports",
    "views/custody_gaps.py": "Custody & Data Gaps",
    "views/data_quality.py": "Data Quality",
}
TIMEOUT_S = 60


def _app(root: Path, monkeypatch) -> AppTest:
    monkeypatch.setenv(session.OUTPUT_DIR_ENV, str(root))
    return AppTest.from_file(str(APP), default_timeout=TIMEOUT_S).run()


def _texts(at: AppTest) -> str:
    parts = [
        e.value
        for kind in (
            "markdown",
            "caption",
            "info",
            "warning",
            "error",
            "success",
            "subheader",
            "header",
            "title",
        )
        for e in getattr(at, kind)
    ]
    return "\n".join(str(p) for p in parts)


def _metric(at: AppTest, label: str) -> str:
    return next(m.value for m in at.metric if m.label == label)


def _no_exception(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]


def _snapshot(root: Path) -> dict[str, tuple[str, int]]:
    return {
        str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """``coldchain run-all --scale starter`` once (without the slow SQL proof)."""
    root = tmp_path_factory.mktemp("phase9")
    result = CliRunner().invoke(
        cli, ["run-all", "--scale", "starter", "--output-dir", str(root), "--no-verify-sql"]
    )
    assert result.exit_code == 0, result.output
    return root


@pytest.fixture(scope="module")
def tables(root) -> dict[TableName, pd.DataFrame]:
    names = (TableName.COMPLIANCE_DECISIONS, TableName.EXCURSION_REPORTS)
    return {name: data.load_table(root, "starter", name) for name in names}


# --------------------------------------------------------------------------------------------
# Missing output (fast: no pipeline run)
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("page", list(PAGES))
def test_every_page_asks_to_run_the_pipeline_when_output_is_missing(
    tmp_path, monkeypatch, page
) -> None:
    at = _app(tmp_path, monkeypatch)
    at.switch_page(page).run()
    _no_exception(at)
    assert [i.value for i in at.info] == ["Run `coldchain run-all --scale starter` first."]
    assert not at.title  # the page itself did not run


def test_the_message_names_the_selected_scale(tmp_path, monkeypatch) -> None:
    at = _app(tmp_path, monkeypatch)
    at.selectbox(key=session.SCALE_KEY).select("full").run()
    _no_exception(at)
    assert at.info[0].value == "Run `coldchain run-all --scale full` first."


def test_the_scale_selector_offers_the_config_scales(tmp_path, monkeypatch) -> None:
    at = _app(tmp_path, monkeypatch)
    assert list(at.selectbox(key=session.SCALE_KEY).options) == ["starter", "full"]


# --------------------------------------------------------------------------------------------
# Every page on real starter output
# --------------------------------------------------------------------------------------------

pytestmark_slow = pytest.mark.slow


@pytestmark_slow
@pytest.mark.parametrize(("page", "title"), list(PAGES.items()))
def test_every_page_renders(root, monkeypatch, page, title) -> None:
    at = _app(root, monkeypatch)
    at.switch_page(page).run()
    _no_exception(at)
    assert [t.value for t in at.title] == [title]
    assert not at.info or "run-all" not in at.info[0].value


@pytestmark_slow
def test_overview_tiles_match_the_engine_output(root, tables, monkeypatch) -> None:
    at = _app(root, monkeypatch)
    _no_exception(at)
    tiles = views.kpis(tables[TableName.COMPLIANCE_DECISIONS], tables[TableName.EXCURSION_REPORTS])
    assert _metric(at, "Shipments") == f"{tiles['shipments']:,}"
    assert _metric(at, "Compliant") == f"{tiles['compliant_pct']:g} %"
    assert _metric(at, "Non-compliant") == str(tiles["non_compliant"])
    assert _metric(at, "Inconclusive") == str(tiles["inconclusive"])
    assert _metric(at, "Excursion reports") == str(tiles["excursions"])


@pytestmark_slow
def test_temperature_chart_of_a_non_compliant_shipment(root, tables, monkeypatch) -> None:
    decisions = tables[TableName.COMPLIANCE_DECISIONS]
    reports = tables[TableName.EXCURSION_REPORTS]
    shipment_id = decisions.loc[decisions["status"] == "NON_COMPLIANT", "shipment_id"].iloc[0]
    at = _app(root, monkeypatch)
    at.switch_page("views/temperature_chart.py").run()
    at.selectbox(key="chart_shipment").select(shipment_id).run()
    _no_exception(at)
    assert _metric(at, "Engine status") == "NON_COMPLIANT"
    expected_reports = reports[reports["shipment_id"] == shipment_id]
    assert _metric(at, "Excursion reports") == str(len(expected_reports))
    shown = at.dataframe[0].value
    assert list(shown["report_id"]) == sorted(expected_reports["report_id"])
    assert list(shown["duration_min"]) == list(
        expected_reports.sort_values("started_at")["duration_min"]
    )


CHART_SHIPMENTS = ("SHP-2026-000001", "SHP-2026-000018", "SHP-2026-000049")
"""The shipments of the Phase 9 review (none, one and four excursion reports by default)."""


@pytestmark_slow
@pytest.mark.parametrize("shipment_id", CHART_SHIPMENTS)
def test_the_chart_streamlit_sends_has_dates_readings_and_excursions(
    root, tables, monkeypatch, shipment_id
) -> None:
    """Regression (Phase 9 review): no line, no red windows and an axis of -1 … 6."""
    window = views.assignment_window(
        shipment_id,
        data.load_table(root, "starter", TableName.SHIPMENTS),
        data.load_table(root, "starter", TableName.LOGGERS),
        data.load_table(root, "starter", TableName.DEVICES),
        10,
    )
    readings = views.classify_readings(
        data.load_readings(root, "starter", [window["logger_id"]]), window
    )
    trusted = int((readings["reading_class"] == views.ReadingClass.TRUSTED.value).sum())
    reports = tables[TableName.EXCURSION_REPORTS]
    spans = views.excursion_spans(
        reports[reports["shipment_id"] == shipment_id], window["interval_min"]
    )
    expected = [
        (charts.plotly_time(r.started_at), charts.plotly_time(r.span_end))
        for r in spans.itertuples()
    ]

    at = _app(root, monkeypatch)
    at.switch_page("views/temperature_chart.py").run()
    at.selectbox(key="chart_shipment").select(shipment_id).run()
    _no_exception(at)
    sent = at.get("plotly_chart")
    assert len(sent) == 1
    spec = spec_of(sent[0].proto.spec)
    assert trusted > 0
    assert_date_chart(spec, trusted, len(readings), expected)
    assert spec["layout"]["xaxis"]["title"]["text"] == "Recorded at (UTC)"
    labels = window_labels(spec, "EXCR-")
    assert len(labels) == len(expected)
    assert_labels_do_not_overlap(spec, "EXCR-")
    if shipment_id == "SHP-2026-000049":  # the four close windows of the approval review
        assert len({label["y"] for label in labels}) == len(labels) == 4


@pytestmark_slow
def test_the_review_shipments_cover_zero_one_and_several_excursions(root, tables) -> None:
    reports = tables[TableName.EXCURSION_REPORTS]
    counts = [int((reports["shipment_id"] == s).sum()) for s in CHART_SHIPMENTS]
    assert counts[0] == 0
    assert counts[1] == 1
    assert counts[2] > 1


@pytestmark_slow
def test_an_excursion_report_links_its_readings_policy_and_chart(root, monkeypatch) -> None:
    reports = data.load_table(root, "starter", TableName.EXCURSION_REPORTS)
    report = reports.sort_values("report_id").iloc[-1]
    at = _app(root, monkeypatch)
    at.switch_page("views/excursion_reports.py").run()
    at.selectbox(key="exc_report").select(report["report_id"]).run()
    _no_exception(at)
    policy_table, readings_table = at.dataframe[1].value, at.dataframe[2].value
    assert policy_table["policy_id"].tolist() == [report["policy_id"]]
    assert len(readings_table) == report["reading_count"]
    assert readings_table["reading_id"].iloc[0] == report["first_reading_id"]
    assert readings_table["reading_id"].iloc[-1] == report["last_reading_id"]

    at.button(key="exc_chart").click().run()
    _no_exception(at)
    assert [t.value for t in at.title] == ["Temperature Chart"]
    assert at.selectbox(key="chart_shipment").value == report["shipment_id"]


@pytestmark_slow
def test_excursion_filters_narrow_the_table(root, monkeypatch) -> None:
    at = _app(root, monkeypatch)
    at.switch_page("views/excursion_reports.py").run()
    total = len(at.dataframe[0].value)
    at.multiselect(key="exc_breach").select("WITHIN_ALLOWANCE").run()
    _no_exception(at)
    within = at.dataframe[0].value
    assert 0 < len(within) < total
    assert set(within["breach_type"]) == {"WITHIN_ALLOWANCE"}
    at.text_input(key="exc_search").input("no-such-id").run()
    _no_exception(at)
    assert at.dataframe[0].value.empty


@pytestmark_slow
def test_custody_and_gaps_page_lists_the_flagged_shipments(root, tables, monkeypatch) -> None:
    flagged = views.flagged_shipments(tables[TableName.COMPLIANCE_DECISIONS])
    at = _app(root, monkeypatch)
    at.switch_page("views/custody_gaps.py").run()
    _no_exception(at)
    assert list(at.dataframe[0].value["shipment_id"]) == list(flagged["shipment_id"])
    custody = flagged.loc[flagged["CUSTODY_GAP"], "shipment_id"].iloc[0]
    at.selectbox(key="gaps_shipment").select(custody).run()
    _no_exception(at)
    assert any("broken link" in e.value for e in at.error)


@pytestmark_slow
def test_explorer_shows_a_shipment_and_labels_the_answer_key(root, monkeypatch) -> None:
    at = _app(root, monkeypatch)
    at.switch_page("views/shipment_explorer.py").run()
    at.multiselect(key="explorer_status").select("INCONCLUSIVE").run()
    _no_exception(at)
    shipment = at.selectbox(key="explorer_shipment").value
    assert _metric(at, "Engine status") == "INCONCLUSIVE"
    assert "Answer key (ground truth)" in _texts(at)
    assert "never reads it" in _texts(at)
    at.button(key="explorer_chart").click().run()
    assert at.selectbox(key="chart_shipment").value == shipment


@pytestmark_slow
def test_a_status_filter_replaces_a_selection_it_excludes(root, tables, monkeypatch) -> None:
    decisions = tables[TableName.COMPLIANCE_DECISIONS]
    compliant = decisions.loc[decisions["status"] == "COMPLIANT", "shipment_id"].iloc[0]
    at = _app(root, monkeypatch)
    at.switch_page("views/shipment_explorer.py").run()
    at.selectbox(key="explorer_shipment").select(compliant).run()
    at.multiselect(key="explorer_status").select("NON_COMPLIANT").run()
    _no_exception(at)
    assert at.selectbox(key="explorer_shipment").value != compliant
    assert _metric(at, "Engine status") == "NON_COMPLIANT"


@pytestmark_slow
def test_data_quality_shows_reports_answer_key_and_reproducibility_hint(root, monkeypatch) -> None:
    at = _app(root, monkeypatch)
    at.switch_page("views/data_quality.py").run()
    _no_exception(at)
    assert _metric(at, "Status") == "PASS"
    assert _metric(at, "Verdict") == "PASS"
    assert _metric(at, "Shipments where the engine differs from the answer key") == "0"
    text = _texts(at)
    assert "Answer key (ground truth) vs engine" in text
    assert "Injected anomalies — Answer key (ground truth)" in text
    assert "Run `coldchain reproduce --scale starter`" in text


@pytestmark_slow
def test_data_quality_shows_the_reproducibility_result(root, tmp_path, monkeypatch) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    result = CliRunner().invoke(cli, ["reproduce", "--scale", "starter", "--output-dir", str(copy)])
    assert result.exit_code == 0, result.output
    at = _app(copy, monkeypatch)
    at.switch_page("views/data_quality.py").run()
    _no_exception(at)
    files = data.load_json_if_present(data.optional_documents(copy, "starter")["reproducibility"])[
        "totals"
    ]["files"]
    assert _metric(at, "Identical files (run1 vs run2)") == f"{files} / {files}"
    assert "coldchain reproduce" not in _texts(at)


@pytestmark_slow
def test_a_partial_output_is_reported_as_missing(root, tmp_path, monkeypatch) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(root, copy)
    data.table_file(copy, "starter", TableName.EXCURSION_REPORTS).unlink()
    at = _app(copy, monkeypatch)
    _no_exception(at)
    assert at.info[0].value == "Run `coldchain run-all --scale starter` first."
    assert "excursion_reports.parquet" in at.expander[0].children[0].value


@pytestmark_slow
def test_clicking_through_every_page_writes_nothing(root, tables, monkeypatch) -> None:
    before = _snapshot(root)
    at = _app(root, monkeypatch)
    shipment_ids = tables[TableName.COMPLIANCE_DECISIONS]["shipment_id"].tolist()
    for page in PAGES:
        at.switch_page(page).run()
        _no_exception(at)
    at.switch_page("views/temperature_chart.py").run()
    for shipment_id in shipment_ids[:: max(1, len(shipment_ids) // 10)]:
        at.selectbox(key="chart_shipment").select(shipment_id).run()
        _no_exception(at)
    assert _snapshot(root) == before


# --------------------------------------------------------------------------------------------
# Static read-only guarantee
# --------------------------------------------------------------------------------------------

FORBIDDEN_NAMES = {
    "write_table",
    "write_tables",
    "write_table_files",
    "write_layers",
    "write_report",
    "write_json",
    "write_text",
    "copy_file",
    "replace_directory",
    "remove_directory",
    "ensure_baseline",
    "clear_injection",
    "write_bytes",
    "unlink",
    "mkdir",
    "rmtree",
    "to_csv",
    "to_parquet",
    "subprocess",
}
FORBIDDEN_MODULES = (
    "coldchain.pipeline",
    "coldchain.cli",
    "coldchain.generate",
    "coldchain.inject",
    "coldchain.compliance",
    "coldchain.export",
    "subprocess",
)


def _viewer_files() -> list[Path]:
    return sorted(
        [
            *(PROJECT_ROOT / "app").rglob("*.py"),
            *(PROJECT_ROOT / "src" / "coldchain" / "viewer").rglob("*.py"),
        ]
    )


@pytest.mark.parametrize("path", _viewer_files(), ids=lambda p: p.name)
def test_the_viewer_never_writes_or_runs_the_pipeline(path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute | ast.Name):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            assert name not in FORBIDDEN_NAMES, f"{path}: {name}"
        if isinstance(node, ast.Import | ast.ImportFrom):
            modules = (
                [a.name for a in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            for module in modules:
                assert not module.startswith(FORBIDDEN_MODULES), f"{path}: imports {module}"
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open":
            pytest.fail(f"{path}: calls open()")
