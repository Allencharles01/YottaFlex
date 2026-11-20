"""``coldchain export`` end to end at starter scale (Phase 7 UAT).

generate → inject → check → validate → evaluate → export, then: the bundle layout keeps the
ground truth separate; the manifest is complete and every checksum matches; the SQL loads into
fresh DuckDB and SQLite databases with foreign keys enforced and row counts equal to the CSVs;
a rerun is byte-identical; a stale or incomplete pipeline is refused.
"""

import gzip
import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml
from typer.testing import CliRunner

from coldchain import __version__
from coldchain.cli import app
from coldchain.config import DEFAULT_CONFIG_PATH
from coldchain.enums import FileFormat, Layer, PipelineStage, SqlEngine, TableName
from coldchain.export.sql_load import load_scripts
from coldchain.io import count_csv_rows, file_sha256, read_table
from coldchain.provenance import git_state
from coldchain.schemas import TABLE_LAYERS

pytestmark = pytest.mark.slow

runner = CliRunner()

PIPELINE = ("generate", "inject", "check", "validate", "evaluate", "export")
SQL_ORDER = ("schema.sql", "seed.sql", "ground_truth_schema.sql", "ground_truth_seed.sql")


def _invoke(root: Path, *args: str):
    return runner.invoke(app, [*args, "--scale", "starter", "--output-dir", str(root)])


def _ok(root: Path, *args: str) -> None:
    result = _invoke(root, *args)
    assert result.exit_code == 0, result.output


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def root(tmp_path_factory) -> Path:
    """The whole pipeline, once."""
    root = tmp_path_factory.mktemp("phase7")
    for command in PIPELINE:
        _ok(root, command)
    return root


@pytest.fixture(scope="module")
def bundle(root) -> Path:
    return root / "starter" / "export"


@pytest.fixture(scope="module")
def manifest(bundle) -> dict:
    return _json(bundle / "manifest.json")


def _copy(root: Path, tmp_path: Path) -> Path:
    target = tmp_path / "copy"
    shutil.copytree(root, target)
    return target


# --------------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------------


def test_every_table_is_exported_as_csv_and_json_in_its_layer_folder(bundle) -> None:
    for table in TableName:
        layer = TABLE_LAYERS[table].value
        assert (bundle / "csv" / layer / f"{table.value}.csv").is_file(), table
        suffix = "jsonl" if table is TableName.TEMPERATURE_READINGS else "json"
        assert (bundle / "json" / layer / f"{table.value}.{suffix}").is_file(), table
    for name in SQL_ORDER:
        assert (bundle / "sql" / name).is_file(), name
    for extra in ("README.md", "docs/data_dictionary.md", "reports/evaluation_report.json"):
        assert (bundle / extra).is_file(), extra
    assert not (bundle / "sql" / "seed.sql.gz").exists()  # far below the 200 MiB threshold


def test_ground_truth_is_never_in_the_input_data_sql(bundle) -> None:
    seed = (bundle / "sql" / "seed.sql").read_text(encoding="utf-8")
    schema = (bundle / "sql" / "schema.sql").read_text(encoding="utf-8")
    truth_seed = (bundle / "sql" / "ground_truth_seed.sql").read_text(encoding="utf-8")
    for table in TableName:
        statement = f'INSERT INTO "{table.value}"'
        if TABLE_LAYERS[table] is Layer.GROUND_TRUTH:
            assert statement not in seed and f'TABLE "{table.value}"' not in schema
            assert statement in truth_seed
        else:
            assert statement in seed and statement not in truth_seed


def test_csv_exports_are_the_pipeline_csv_files(root, bundle) -> None:
    for table in TableName:
        layer = TABLE_LAYERS[table].value
        source = root / "starter" / layer / f"{table.value}.csv"
        exported = bundle / "csv" / layer / f"{table.value}.csv"
        assert file_sha256(source) == file_sha256(exported), table


def test_json_and_jsonl_round_trip_to_the_parquet_tables(root, bundle) -> None:
    for table in TableName:
        layer = TABLE_LAYERS[table]
        fmt = "jsonl" if table is TableName.TEMPERATURE_READINGS else "json"
        directory = bundle / "json" / layer.value
        back = read_table(table, directory, FileFormat(fmt))
        original = read_table(table, root / "starter" / layer.value)
        pd.testing.assert_frame_equal(back, original)
    lines = (bundle / "json/raw/temperature_readings.jsonl").read_text().splitlines()
    assert len(lines) == count_csv_rows(bundle / "csv/raw/temperature_readings.csv")


# --------------------------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------------------------


def test_manifest_records_the_run(root, manifest) -> None:
    assert manifest["scale"] == "starter" and manifest["seed"] == 20261022
    assert manifest["package"] == {"name": "coldchain", "version": __version__}
    assert manifest["command"].startswith("coldchain export --scale starter ")
    assert "--verify-sql" in manifest["command"]
    assert manifest["git"] == git_state().as_dict()
    assert len(manifest["config"]["sha256"]) == 64
    assert [p["stage"] for p in manifest["pipeline"]] == [
        "generate",
        "inject",
        "check",
        "validate",
        "evaluate",
    ]
    generate = manifest["pipeline"][0]
    assert generate["seed"] == 20261022 and generate["command"].startswith("coldchain generate")
    assert {p["config_sha256"] for p in manifest["pipeline"]} == {manifest["config"]["sha256"]}
    assert set(manifest["run"]) == {"generated_at", "note"}


def test_manifest_input_checksums_match_the_files(root, manifest) -> None:
    assert len(manifest["inputs"]) == len(TableName) + 4  # 20 tables + 2 reports x (md, json)
    for relative, digest in manifest["inputs"].items():
        assert file_sha256(root / relative) == digest, relative


def test_manifest_lists_every_file_with_rows_size_and_checksum(bundle, manifest) -> None:
    on_disk = {p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file()} - {
        "manifest.json"
    }
    listed = {entry["path"]: entry for entry in manifest["files"]}
    assert set(listed) == on_disk
    for path, entry in listed.items():
        assert file_sha256(bundle / path) == entry["sha256"], path
        assert (bundle / path).stat().st_size == entry["bytes"], path
        if entry["kind"] == "csv":
            assert count_csv_rows(bundle / path) == entry["rows"], path
        if entry["kind"] == "jsonl":
            lines = (bundle / path).read_text(encoding="utf-8").splitlines()
            assert len(lines) == entry["rows"]
        if entry["kind"] == "json" and entry["table"]:
            assert len(_json_array(bundle / path)) == entry["rows"]
    assert manifest["totals"]["rows"][TableName.SHIPMENTS.value] == 50
    assert manifest["totals"]["rows"][TableName.DEVICES.value] == 20


def _json_array(path: Path) -> list:
    return json.loads(path.read_text(encoding="utf-8"))


def test_manifest_records_the_sql_proof(manifest) -> None:
    proof = manifest["sql"]["verification"]
    assert set(proof) == {engine.value for engine in SqlEngine}
    for result in proof.values():
        assert result["status"] == "PASS" and result["foreign_keys_enforced"] is True
        assert result["foreign_key_violations"] == 0
        assert result["row_counts"] == manifest["totals"]["rows"]
    assert manifest["sql"]["load_order"] == [f"sql/{name}" for name in SQL_ORDER]


# --------------------------------------------------------------------------------------------
# SQL loads independently of the export's own check
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("engine", list(SqlEngine))
def test_sql_loads_into_a_fresh_database_with_counts_matching_the_csvs(bundle, engine) -> None:
    result = load_scripts(engine, [bundle / "sql" / name for name in SQL_ORDER])
    assert result.enforcement_probe is True
    assert sum(result.foreign_key_violations.values()) == 0
    csv_counts = {
        table.value: count_csv_rows(bundle / "csv" / TABLE_LAYERS[table].value / f"{table}.csv")
        for table in TableName
    }
    assert result.row_counts == csv_counts


def test_the_input_data_sql_loads_without_the_ground_truth(bundle) -> None:
    result = load_scripts(SqlEngine.SQLITE, [bundle / "sql" / n for n in SQL_ORDER[:2]])
    assert not set(result.row_counts) & {
        t.value for t in TableName if TABLE_LAYERS[t] is Layer.GROUND_TRUTH
    }


# --------------------------------------------------------------------------------------------
# Reproducibility and refusal
# --------------------------------------------------------------------------------------------


def test_export_rerun_is_byte_identical(root, bundle, manifest, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    _ok(copy, "export")
    again = _json(copy / "starter" / "export" / "manifest.json")
    assert again["dataset_sha256"] == manifest["dataset_sha256"]
    assert [(e["path"], e["sha256"]) for e in again["files"]] == [
        (e["path"], e["sha256"]) for e in manifest["files"]
    ]
    # Only the output folder in the command differs between the two roots.
    assert again["command"] != manifest["command"]
    assert not (copy / "starter" / "export.partial").exists()


def test_export_writes_its_own_provenance(root, manifest) -> None:
    record = _json(root / "starter" / "provenance" / "export.json")
    assert record["stage"] == PipelineStage.EXPORT.value
    assert record["outputs"]["starter/export/manifest.json"]
    assert len(record["outputs"]) == manifest["totals"]["files"] + 1


def test_export_refuses_a_stale_pipeline_and_keeps_the_old_bundle(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    before = file_sha256(copy / "starter" / "export" / "manifest.json")
    _ok(copy, "inject", "--seed", "7")  # rewrites raw/: check, validate, evaluate are now stale
    result = _invoke(copy, "export")
    assert result.exit_code == 1
    assert "Stale or incomplete pipeline" in result.output and "re-run" in result.output
    assert file_sha256(copy / "starter" / "export" / "manifest.json") == before


def test_export_refuses_a_tampered_report(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    report = copy / "starter" / "evaluation" / "evaluation_report.md"
    report.write_text(report.read_text(encoding="utf-8") + "edited\n", encoding="utf-8")
    result = _invoke(copy, "export")
    assert result.exit_code == 1 and "was modified or removed since" in result.output


def _config_copy(tmp_path: Path, edit) -> Path:
    data = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    edit(data)
    path = tmp_path / "edited.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_changing_only_export_settings_keeps_the_pipeline_fresh(root, manifest, tmp_path) -> None:
    """DECISIONS.md #108: export settings have their own hash and never make a stage stale."""
    copy = _copy(root, tmp_path)

    def edit(data: dict) -> None:
        data["export"].update(sql_batch_rows=37, sql_gzip_threshold_mb=0)

    _ok(copy, "export", "--config", str(_config_copy(tmp_path, edit)))
    again = _json(copy / "starter" / "export" / "manifest.json")
    assert again["config"]["sha256"] == manifest["config"]["sha256"]
    assert again["export_settings"]["sha256"] != manifest["export_settings"]["sha256"]
    assert again["export_settings"]["values"]["sql_batch_rows"] == 37
    assert (copy / "starter" / "export" / "sql" / "seed.sql.gz").is_file()
    for engine in again["sql"]["verification"].values():
        assert engine["status"] == "PASS"


@pytest.mark.parametrize(
    "edit",
    [
        lambda data: data.update(long_dwell_threshold_min=data["long_dwell_threshold_min"] + 1),
        lambda data: data["evaluation"].update(strict_scales=["starter"]),
        lambda data: data["generation"]["readings"].update(noise_sd_c=0.2),
    ],
    ids=["top_level", "evaluation", "generation"],
)
def test_changing_any_other_config_section_makes_the_pipeline_stale(root, tmp_path, edit) -> None:
    copy = _copy(root, tmp_path)
    result = _invoke(copy, "export", "--config", str(_config_copy(tmp_path, edit)))
    assert result.exit_code == 1
    assert "ran with a different config" in result.output


def test_manifest_records_the_export_settings_separately(manifest, app_config) -> None:
    assert manifest["export_settings"]["values"] == app_config.export.model_dump(mode="json")
    assert len(manifest["export_settings"]["sha256"]) == 64
    assert manifest["export_settings"]["sha256"] not in manifest["config"]["files"].values()


def test_export_needs_every_stage(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    (copy / "starter" / "provenance" / "check.json").unlink()
    result = _invoke(copy, "export")
    assert result.exit_code == 1 and "run `coldchain check --scale starter`" in result.output


def test_regenerating_drops_the_inject_provenance(root, tmp_path) -> None:
    copy = _copy(root, tmp_path)
    _ok(copy, "generate")
    assert not (copy / "starter" / "provenance" / "inject.json").exists()
    result = _invoke(copy, "export")  # inject's record is gone: the chain is stale, not missing
    assert result.exit_code == 1
    assert "Stale or incomplete pipeline" in result.output
    assert "No provenance record" not in result.output


def test_a_pipeline_without_injection_exports_an_empty_anomalies_table(tmp_path) -> None:
    root = tmp_path / "clean"
    for command in ("generate", "check", "validate", "evaluate"):
        _ok(root, command)
    _ok(root, "export", "--no-verify-sql")
    manifest = _json(root / "starter" / "export" / "manifest.json")
    assert manifest["totals"]["rows"][TableName.ANOMALIES.value] == 0
    assert manifest["sql"]["verification"] is None
    assert [p["stage"] for p in manifest["pipeline"]] == [
        "generate",
        "check",
        "validate",
        "evaluate",
    ]
    result = load_scripts(
        SqlEngine.DUCKDB, [root / "starter" / "export" / "sql" / n for n in SQL_ORDER]
    )
    assert result.row_counts[TableName.ANOMALIES.value] == 0


def test_gzip_copy_when_seed_is_above_the_threshold(tmp_path, valid_tables, app_config) -> None:
    from coldchain.export.exporters import write_sql_exports

    settings = app_config.export.model_copy(update={"sql_gzip_threshold_mb": 0})
    first = write_sql_exports(valid_tables, tmp_path / "a", settings)
    write_sql_exports(valid_tables, tmp_path / "b", settings)
    gz = [f.path for f in first if f.kind == "sql.gz"]
    assert gz == ["sql/seed.sql.gz", "sql/ground_truth_seed.sql.gz"]
    for path in gz:
        a, b = tmp_path / "a" / path, tmp_path / "b" / path
        assert a.read_bytes() == b.read_bytes()  # fixed gzip header: deterministic
        plain = (tmp_path / "a" / path.removesuffix(".gz")).read_bytes()
        assert gzip.decompress(a.read_bytes()) == plain
    result = load_scripts(
        SqlEngine.SQLITE,
        [tmp_path / "a" / "sql" / n for n in ("schema.sql", "seed.sql.gz")],
    )
    assert result.row_counts[TableName.SHIPMENTS.value] == len(valid_tables[TableName.SHIPMENTS])
