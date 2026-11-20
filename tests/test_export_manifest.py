"""The export manifest's hashes and the bundle README."""

import copy

from coldchain.enums import FileFormat, TableName
from coldchain.export.exporters import ExportedFile, table_files
from coldchain.export.manifest import (
    RUN_KEY,
    build_manifest,
    content_sha256,
    file_entry,
    pipeline_entry,
    render_readme,
)
from coldchain.provenance import combined_sha256


def _manifest(generated_at: str = "2026-11-20T00:00:00Z", **overrides) -> dict:
    files = [
        ExportedFile("csv/raw/parties.csv", "csv", "raw", "parties", 3),
        ExportedFile("README.md", "md"),
    ]
    entries = [file_entry(f, f"{i}" * 64, 100 + i) for i, f in enumerate(files)]
    arguments = {
        "scale": "starter",
        "seed": 20261022,
        "command": "coldchain export --scale starter",
        "git": {"commit": "abc", "dirty": False},
        "config": {"sha256": "cfg"},
        "export_settings": {"sha256": "exp", "values": {}},
        "pipeline": [],
        "inputs": {"starter/raw/parties.parquet": "p" * 64},
        "entries": entries,
        "sql": {"verification": None},
        "generated_at": generated_at,
    }
    arguments.update(overrides)
    return build_manifest(**arguments)


def test_content_hash_ignores_only_the_run_block() -> None:
    first, second = _manifest("2026-11-20T00:00:00Z"), _manifest("2027-01-01T12:34:00Z")
    assert first[RUN_KEY] != second[RUN_KEY]
    assert first["content_sha256"] == second["content_sha256"]
    assert content_sha256(first) == first["content_sha256"]
    assert _manifest(seed=1)["content_sha256"] != first["content_sha256"]


def test_content_hash_detects_any_edit() -> None:
    manifest = _manifest()
    edited = copy.deepcopy(manifest)
    edited["files"][0]["rows"] = 4
    assert content_sha256(edited) != manifest["content_sha256"]


def test_dataset_hash_covers_only_the_files() -> None:
    manifest = _manifest()
    expected = combined_sha256({e["path"]: e["sha256"] for e in manifest["files"]})
    assert manifest["dataset_sha256"] == expected
    other = _manifest(command="coldchain export --scale starter --output-dir /x", git={})
    assert other["dataset_sha256"] == manifest["dataset_sha256"]


def test_files_are_sorted_and_totals_add_up() -> None:
    manifest = _manifest()
    assert [e["path"] for e in manifest["files"]] == ["README.md", "csv/raw/parties.csv"]
    assert manifest["totals"] == {"files": 2, "bytes": 201, "rows": {"parties": 3}}
    assert list(manifest)[-2:] == ["content_sha256", RUN_KEY]


def test_pipeline_entry_drops_file_digests() -> None:
    record = {
        "stage": "generate",
        "command": "coldchain generate",
        "seed": 7,
        "package_version": "0.1.0",
        "git": {"commit": None, "dirty": None},
        "config": {"sha256": "cfg", "files": {}},
        "inputs": {"a": "1"},
        "outputs": {"b": "2"},
    }
    assert pipeline_entry(record) == {
        "stage": "generate",
        "command": "coldchain generate",
        "seed": 7,
        "package_version": "0.1.0",
        "git": {"commit": None, "dirty": None},
        "config_sha256": "cfg",
    }


def test_table_files_use_one_folder_per_format_and_layer(app_config) -> None:
    settings = app_config.export
    assert table_files(TableName.SHIPMENTS, settings) == {
        FileFormat.CSV: "csv/raw/shipments.csv",
        FileFormat.JSON: "json/raw/shipments.json",
    }
    assert table_files(TableName.TEMPERATURE_READINGS, settings)[FileFormat.JSONL] == (
        "json/raw/temperature_readings.jsonl"
    )
    assert table_files(TableName.ANOMALIES, settings)[FileFormat.CSV] == (
        "csv/ground_truth/anomalies.csv"
    )


def test_readme_lists_tables_and_has_no_wall_clock_time(app_config) -> None:
    files = [ExportedFile("csv/raw/parties.csv", "csv", "raw", "parties", 1234)]
    text = render_readme(scale="full", seed=5, files=files, settings=app_config.export)
    assert "| `parties` | raw | 1,234 |" in text
    assert "seed `5`" in text and "PRAGMA foreign_keys = ON;" in text
    assert "2026-" not in text
