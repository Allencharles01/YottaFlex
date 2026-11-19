from pathlib import Path

import pandas as pd
import pytest

from coldchain import io
from coldchain.enums import FileFormat, Layer, TableName
from coldchain.errors import SchemaValidationError, TableIOError
from coldchain.schemas import SCHEMAS, TABLE_LAYERS


@pytest.mark.slow
@pytest.mark.parametrize("fmt", list(FileFormat))
@pytest.mark.parametrize("table", list(TableName))
def test_round_trip_preserves_exact_dtypes_and_passes_schema(
    tmp_path: Path, valid_tables, table: TableName, fmt: FileFormat
) -> None:
    frame = valid_tables[table]
    io.write_table(table, frame, tmp_path, formats=[fmt])
    back = io.read_table(table, tmp_path, fmt)
    pd.testing.assert_frame_equal(back, io.sort_by_primary_key(table, frame))
    SCHEMAS[table].validate(back)


@pytest.mark.slow
@pytest.mark.parametrize("fmt", list(FileFormat))
@pytest.mark.parametrize("table", list(TableName))
def test_empty_table_round_trips(
    tmp_path: Path, valid_tables, table: TableName, fmt: FileFormat
) -> None:
    empty = valid_tables[table].iloc[0:0]
    io.write_table(table, empty, tmp_path, formats=[fmt])
    back = io.read_table(table, tmp_path, fmt)
    assert back.empty
    assert list(back.columns) == list(SCHEMAS[table].columns)


def test_csv_text_representation(tmp_path: Path, valid_tables) -> None:
    io.write_table(TableName.ROUTE_STOPS, valid_tables[TableName.ROUTE_STOPS], tmp_path)
    lines = (tmp_path / "route_stops.csv").read_text(encoding="utf-8").splitlines()
    assert lines[1] == (
        "RST-SHP-2026-000001-01,SHP-2026-000001,1,ORIGIN,PTY-0001,,2026-03-10T08:00:00Z,"
    )
    assert lines[2].endswith(",2026-03-10T10:00:00Z,2026-03-10T10:30:00Z,30")
    io.write_table(TableName.LOTS, valid_tables[TableName.LOTS], tmp_path)
    lot_line = (tmp_path / "lots.csv").read_text(encoding="utf-8").splitlines()[1]
    assert ",2026-01-15,2028-01-15," in lot_line


def test_write_sorts_by_primary_key(tmp_path: Path, valid_tables) -> None:
    stops = valid_tables[TableName.ROUTE_STOPS].iloc[::-1]
    io.write_table(TableName.ROUTE_STOPS, stops, tmp_path)
    back = io.read_table(TableName.ROUTE_STOPS, tmp_path, FileFormat.CSV)
    assert back["stop_id"].is_monotonic_increasing


def test_write_is_byte_deterministic(tmp_path: Path, valid_tables) -> None:
    table = TableName.SHIPMENTS
    first = io.write_table(table, valid_tables[table], tmp_path / "a", list(FileFormat))
    second = io.write_table(table, valid_tables[table], tmp_path / "b", list(FileFormat))
    for fmt in FileFormat:
        assert first[fmt].read_bytes() == second[fmt].read_bytes()


def test_invalid_frame_is_rejected_and_not_written(tmp_path: Path, valid_tables) -> None:
    bad = valid_tables[TableName.SHIPMENTS].assign(carrier_reported_status="MAYBE")
    with pytest.raises(SchemaValidationError):
        io.write_table(TableName.SHIPMENTS, bad, tmp_path)
    assert not list(tmp_path.iterdir())


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(TableIOError, match="not found"):
        io.read_table(TableName.PARTIES, tmp_path, FileFormat.CSV)


def test_wrong_columns_raise(tmp_path: Path) -> None:
    (tmp_path / "parties.csv").write_text("party_id,colour\nPTY-0001,blue\n", encoding="utf-8")
    with pytest.raises(TableIOError, match="column mismatch"):
        io.read_table(TableName.PARTIES, tmp_path, FileFormat.CSV)


def test_unparseable_value_raises(tmp_path: Path, valid_tables) -> None:
    io.write_table(TableName.LOTS, valid_tables[TableName.LOTS], tmp_path, [FileFormat.CSV])
    path = tmp_path / "lots.csv"
    path.write_text(path.read_text().replace("2026-01-15", "15/01/2026"), encoding="utf-8")
    with pytest.raises(TableIOError, match="manufactured_on"):
        io.read_table(TableName.LOTS, tmp_path, FileFormat.CSV)


def test_text_that_looks_like_na_stays_text(tmp_path: Path, valid_tables) -> None:
    parties = valid_tables[TableName.PARTIES].copy()
    parties.loc[0, "name"] = "NA"
    io.write_table(TableName.PARTIES, parties, tmp_path, [FileFormat.CSV])
    back = io.read_table(TableName.PARTIES, tmp_path, FileFormat.CSV)
    assert back.loc[0, "name"] == "NA"


def test_schema_check_can_be_skipped(tmp_path: Path, valid_tables) -> None:
    io.write_table(TableName.LOTS, valid_tables[TableName.LOTS], tmp_path, [FileFormat.CSV])
    path = tmp_path / "lots.csv"
    path.write_text(path.read_text().replace("RELEASED", "LOST"), encoding="utf-8")
    with pytest.raises(SchemaValidationError):
        io.read_table(TableName.LOTS, tmp_path, FileFormat.CSV)
    back = io.read_table(TableName.LOTS, tmp_path, FileFormat.CSV, check_schema=False)
    assert back.loc[0, "lot_status"] == "LOST"


def test_coerce_is_idempotent(valid_tables) -> None:
    for table, frame in valid_tables.items():
        once = io.coerce_to_schema(table, frame)
        pd.testing.assert_frame_equal(io.coerce_to_schema(table, once), once)


def test_write_and_read_several_tables(tmp_path: Path, valid_tables) -> None:
    subset = {t: valid_tables[t] for t in (TableName.PARTIES, TableName.DEVICES)}
    paths = io.write_tables(subset, tmp_path)
    assert set(paths) == set(subset)
    back = io.read_tables(subset, tmp_path)
    assert set(back) == set(subset)


def test_layer_dir_and_raw_tables() -> None:
    assert io.layer_dir(Path("out"), "starter", Layer.RAW) == Path("out/starter/raw")
    raw = io.raw_tables()
    assert TableName.SHIPMENTS in raw
    assert all(TABLE_LAYERS[t] is Layer.RAW for t in raw)
    assert TableName.EXPECTED_OUTCOMES not in raw


# --------------------------------------------------------------------------------------------
# JSON and the baseline snapshot (Phase 5)
# --------------------------------------------------------------------------------------------


def test_default_formats_do_not_include_json(tmp_path: Path, valid_tables) -> None:
    written = io.write_table(TableName.PARTIES, valid_tables[TableName.PARTIES], tmp_path)
    assert set(written) == {FileFormat.PARQUET, FileFormat.CSV}


def test_json_is_an_array_of_records_with_nulls(tmp_path: Path, valid_tables) -> None:
    import json

    table = TableName.ROUTE_STOPS
    path = io.write_table(table, valid_tables[table], tmp_path, [FileFormat.JSON])[FileFormat.JSON]
    records = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(records, list) and len(records) == len(valid_tables[table])
    assert list(records[0]) == list(valid_tables[table].columns)
    assert records[0]["arrived_at"] is None  # the ORIGIN stop has no arrival
    assert path.read_text(encoding="utf-8").endswith("]\n")


def test_json_that_is_not_an_array_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "parties.json").write_text('{"party_id": "PTY-0001"}', encoding="utf-8")
    with pytest.raises(TableIOError, match="array of records"):
        io.read_table(TableName.PARTIES, tmp_path, FileFormat.JSON)


def _fake_generation(root: Path, valid_tables) -> None:
    for name in io.BASELINE_TABLES:
        target = io.layer_dir(root, "starter", io.TABLE_LAYERS[name])
        io.write_table(name, valid_tables[name], target)


def test_ensure_baseline_copies_generator_output_byte_for_byte(
    tmp_path: Path, valid_tables
) -> None:
    _fake_generation(tmp_path, valid_tables)
    assert io.ensure_baseline(tmp_path, "starter") is True
    for name in io.BASELINE_TABLES:
        layer = io.TABLE_LAYERS[name]
        for fmt in (FileFormat.PARQUET, FileFormat.CSV):
            source = io.table_path(io.layer_dir(tmp_path, "starter", layer), name, fmt)
            copy = io.table_path(io.baseline_dir(tmp_path, "starter", layer), name, fmt)
            assert copy.read_bytes() == source.read_bytes()
    snapshot = sorted(
        p.name for p in io.baseline_dir(tmp_path, "starter", Layer.GROUND_TRUTH).iterdir()
    )
    assert snapshot == ["expected_outcomes.csv", "expected_outcomes.parquet"]


def test_ensure_baseline_never_overwrites_an_existing_snapshot(
    tmp_path: Path, valid_tables
) -> None:
    _fake_generation(tmp_path, valid_tables)
    io.ensure_baseline(tmp_path, "starter")
    raw = io.layer_dir(tmp_path, "starter", Layer.RAW)
    io.write_table(TableName.PARTIES, valid_tables[TableName.PARTIES].iloc[:1], raw)
    assert io.ensure_baseline(tmp_path, "starter") is False
    kept = io.read_table(TableName.PARTIES, io.baseline_dir(tmp_path, "starter", Layer.RAW))
    assert len(kept) == len(valid_tables[TableName.PARTIES])


def test_ensure_baseline_needs_generated_data(tmp_path: Path) -> None:
    with pytest.raises(TableIOError, match="coldchain generate"):
        io.ensure_baseline(tmp_path, "starter")


def test_ensure_baseline_rejects_an_incomplete_snapshot(tmp_path: Path, valid_tables) -> None:
    _fake_generation(tmp_path, valid_tables)
    io.ensure_baseline(tmp_path, "starter")
    io.table_path(
        io.baseline_dir(tmp_path, "starter", Layer.RAW), TableName.LOTS, FileFormat.PARQUET
    ).unlink()
    with pytest.raises(TableIOError, match=r"lots\.parquet"):
        io.ensure_baseline(tmp_path, "starter")


def test_clear_injection_removes_snapshot_and_anomalies_only(tmp_path: Path, valid_tables) -> None:
    _fake_generation(tmp_path, valid_tables)
    io.ensure_baseline(tmp_path, "starter")
    truth = io.layer_dir(tmp_path, "starter", Layer.GROUND_TRUTH)
    io.write_table(TableName.ANOMALIES, valid_tables[TableName.ANOMALIES], truth, list(FileFormat))
    removed = io.clear_injection(tmp_path, "starter")
    assert len(removed) == 4  # the snapshot folder + anomalies.{parquet,csv,json}
    assert not io.baseline_root(tmp_path, "starter").exists()
    assert sorted(p.name for p in truth.iterdir()) == [
        "expected_outcomes.csv",
        "expected_outcomes.parquet",
    ]
    assert io.clear_injection(tmp_path, "starter") == []
