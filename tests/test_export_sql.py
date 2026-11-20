"""SQL rendering and loading (BACKLOG #4): escaping, DDL, dependency order, batching, and
round trips through fresh DuckDB and SQLite databases with foreign keys enforced."""

import sqlite3
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest

from coldchain.config import SQLITE_MAX_SQL_LENGTH
from coldchain.enums import Layer, SqlEngine, TableName
from coldchain.errors import ExportError
from coldchain.export import sql
from coldchain.export.sql_load import iter_statements, load_scripts, verify_loads
from coldchain.io import ColumnKind, write_text
from coldchain.schemas import FOREIGN_KEYS, SCHEMAS, TABLE_LAYERS

TRICKY_NAMES = [
    "O'Brien; DROP TABLE parties;--",
    "Zürich \u2013 東京 🚚 Café",  # en dash, CJK, emoji, accents
    'Back\\slash "double" quotes',
    "Line one\nline two;\n",
    "'';'",
]


# --------------------------------------------------------------------------------------------
# Literals
# --------------------------------------------------------------------------------------------


def test_text_literals_double_single_quotes_and_render_nulls() -> None:
    values = pd.Series(["O'Brien", None, "plain", "''", "東京"], dtype=object)
    assert sql.text_literals(values).tolist() == [
        "'O''Brien'",
        "NULL",
        "'plain'",
        "''''''",
        "'東京'",
    ]


def test_text_literals_keep_backslashes_and_semicolons_verbatim() -> None:
    assert sql.text_literals(pd.Series(["a\\b;c"])).tolist() == ["'a\\b;c'"]


def test_text_with_a_nul_character_is_refused() -> None:
    with pytest.raises(ExportError, match="NUL"):
        sql.text_literals(pd.Series(["bad\x00value"]))


def test_number_literals_integers_floats_and_nulls() -> None:
    ints = pd.Series([1, None, -3], dtype="Int64")
    assert sql.number_literals(ints, ColumnKind.NULLABLE_INTEGER).tolist() == ["1", "NULL", "-3"]
    floats = pd.Series([5.12, -24.0, 1e16, 0.1])
    assert sql.number_literals(floats, ColumnKind.FLOAT).tolist() == [
        "5.12",
        "-24.0",
        "1e+16",
        "0.1",
    ]


@pytest.mark.parametrize("bad", [np.inf, -np.inf])
def test_infinite_floats_are_refused(bad: float) -> None:
    with pytest.raises(ExportError, match="infinite"):
        sql.number_literals(pd.Series([1.0, bad]), ColumnKind.FLOAT)


def test_nan_is_a_missing_float_and_becomes_null() -> None:
    rendered = sql.number_literals(pd.Series([1.5, np.nan]), ColumnKind.FLOAT)
    assert rendered.tolist() == ["1.5", "NULL"]


def test_float_literals_read_back_exactly() -> None:
    values = pd.Series([5.12, -24.0, 0.1, 2.675, 12.34])
    rendered = sql.number_literals(values, ColumnKind.FLOAT)
    assert [float(v) for v in rendered] == values.tolist()


@pytest.mark.parametrize("name", ['a"b', "Upper", "1abc", "a b", "a;b", ""])
def test_identifiers_must_be_snake_case(name: str) -> None:
    with pytest.raises(ExportError, match="identifier"):
        sql.quote_identifier(name)


def test_identifiers_are_double_quoted() -> None:
    assert sql.quote_identifier("shipment_id") == '"shipment_id"'


# --------------------------------------------------------------------------------------------
# DDL and order
# --------------------------------------------------------------------------------------------


def test_dependency_order_puts_every_parent_first() -> None:
    order = sql.dependency_order(TableName)
    position = {t: i for i, t in enumerate(order)}
    assert set(order) == set(TableName)
    for child, _, parent, _ in FOREIGN_KEYS:
        if child is not parent:
            assert position[parent] < position[child], (parent, child)


def test_dependency_order_ignores_parents_outside_the_set() -> None:
    truth = [t for t in TableName if TABLE_LAYERS[t] is Layer.GROUND_TRUTH]
    assert sql.dependency_order(truth) == truth


def test_dependency_order_is_fixed() -> None:
    assert sql.dependency_order(reversed(list(TableName))) == sql.dependency_order(TableName)


@pytest.mark.parametrize("table", list(TableName))
def test_create_table_declares_keys_and_nullability(table: TableName) -> None:
    ddl = sql.create_table(table)
    assert ddl.startswith(f'CREATE TABLE "{table.value}" (')
    assert "PRIMARY KEY (" in ddl
    for name, column in SCHEMAS[table].columns.items():
        line = next(line for line in ddl.splitlines() if line.strip().startswith(f'"{name}" '))
        assert line.rstrip(",").endswith("NOT NULL") is (not column.nullable), line
    for child, column, parent, parent_column in FOREIGN_KEYS:
        if child is table:
            assert (
                f'FOREIGN KEY ("{column}") REFERENCES "{parent.value}" ("{parent_column}")' in ddl
            )


def test_composite_primary_keys_and_unique_columns() -> None:
    assert 'PRIMARY KEY ("shipment_id", "lot_id")' in sql.create_table(TableName.SHIPMENT_LOTS)
    loggers = sql.create_table(TableName.LOGGERS)
    assert 'UNIQUE ("shipment_id")' in loggers


# --------------------------------------------------------------------------------------------
# INSERT batching
# --------------------------------------------------------------------------------------------


def _many_parties(valid_tables, n: int) -> pd.DataFrame:
    base = valid_tables[TableName.PARTIES].iloc[[0]]
    frame = pd.concat([base] * n, ignore_index=True)
    frame["party_id"] = [f"PTY-{i:04d}" for i in range(1, n + 1)]
    frame["name"] = [f"Party {i}" for i in range(1, n + 1)]
    return frame


def test_inserts_are_batched_in_primary_key_order(valid_tables) -> None:
    frame = _many_parties(valid_tables, 7).iloc[::-1]
    statements = list(sql.insert_statements(TableName.PARTIES, frame, batch_rows=3))
    assert len(statements) == 3
    assert [s.count("\n(") for s in statements] == [3, 3, 1]
    assert statements[0].startswith('INSERT INTO "parties" ("party_id", "party_type", ')
    assert "('PTY-0001'" in statements[0] and statements[-1].rstrip().endswith(");")


def test_an_empty_table_yields_no_insert(valid_tables) -> None:
    empty = valid_tables[TableName.PARTIES].iloc[0:0]
    assert list(sql.insert_statements(TableName.PARTIES, empty, 10)) == []


def test_batch_rows_must_be_positive(valid_tables) -> None:
    with pytest.raises(ExportError, match="batch_rows"):
        list(sql.insert_statements(TableName.PARTIES, valid_tables[TableName.PARTIES], 0))


def test_a_statement_above_the_sqlite_limit_is_refused(valid_tables) -> None:
    frame = _many_parties(valid_tables, 2)
    frame["name"] = ["x" * (SQLITE_MAX_SQL_LENGTH // 2 + 10), "y" * (SQLITE_MAX_SQL_LENGTH // 2)]
    with pytest.raises(ExportError, match="sql_batch_rows"):
        list(sql.insert_statements(TableName.PARTIES, frame, batch_rows=2))


def test_seed_script_is_one_transaction_in_dependency_order(valid_tables) -> None:
    text = "".join(sql.seed_script(valid_tables, 100, "title"))
    assert text.startswith("-- title\n")
    assert text.count("BEGIN TRANSACTION;") == 1 and text.rstrip().endswith("COMMIT;")
    first_insert = [text.index(f'INSERT INTO "{t.value}"') for t in sql.dependency_order(TableName)]
    assert first_insert == sorted(first_insert)


def test_statement_splitter_respects_quotes() -> None:
    lines = [
        "-- header\n",
        "INSERT INTO t VALUES\n",
        "('a;\n",
        "b'),\n",
        "('c');\n",
        "COMMIT;\n",
        "-- trailing comment\n",
    ]
    statements = list(iter_statements(lines))
    assert len(statements) == 2
    assert "('a;\nb')" in statements[0]


def test_statement_splitter_rejects_an_unfinished_script() -> None:
    with pytest.raises(ExportError, match="incomplete"):
        list(iter_statements(["INSERT INTO t VALUES\n", "('a')\n"]))


# --------------------------------------------------------------------------------------------
# Round trip through both engines
# --------------------------------------------------------------------------------------------


@pytest.fixture
def tricky_tables(valid_tables) -> dict[TableName, pd.DataFrame]:
    """The valid sample with quotes, SQL syntax, unicode and newlines in text, and NULLs."""
    tables = dict(valid_tables)
    parties = tables[TableName.PARTIES].copy()
    parties.loc[: len(TRICKY_NAMES) - 1, "name"] = TRICKY_NAMES
    tables[TableName.PARTIES] = parties
    return tables


def _scripts(tmp_path: Path, tables: dict[TableName, pd.DataFrame]) -> list[Path]:
    data = {t: f for t, f in tables.items() if TABLE_LAYERS[t] is not Layer.GROUND_TRUTH}
    truth = {t: f for t, f in tables.items() if TABLE_LAYERS[t] is Layer.GROUND_TRUTH}
    paths = []
    for stem, members in (("", data), ("ground_truth_", truth)):
        schema = write_text(tmp_path / f"{stem}schema.sql", [sql.schema_script(members, "t")])
        seed = write_text(tmp_path / f"{stem}seed.sql", sql.seed_script(members, 2, "t"))
        paths += [schema, seed]
    return paths


@pytest.mark.parametrize("engine", list(SqlEngine))
def test_tricky_values_round_trip_exactly(tmp_path: Path, tricky_tables, engine) -> None:
    paths = _scripts(tmp_path, tricky_tables)
    expected = {t.value: len(f) for t, f in tricky_tables.items()}
    proof = verify_loads(paths, expected, engines=[engine])
    assert proof[engine.value]["status"] == "PASS"
    assert proof[engine.value]["foreign_keys_enforced"] is True
    assert proof[engine.value]["foreign_key_violations"] == 0

    # Load again by hand and read the values back.
    if engine is SqlEngine.SQLITE:
        con = sqlite3.connect(":memory:", isolation_level=None)
        con.execute("PRAGMA foreign_keys = ON")
    else:
        con = duckdb.connect(":memory:")
    for path in paths:
        for statement in iter_statements(path.read_text(encoding="utf-8").splitlines(True)):
            con.execute(statement)
    names = [
        r[0] for r in con.execute('SELECT "name" FROM "parties" ORDER BY "party_id"').fetchall()
    ]
    assert names == tricky_tables[TableName.PARTIES].sort_values("party_id")["name"].tolist()
    nulls = con.execute(
        'SELECT COUNT(*) FROM "threshold_policies" WHERE "effective_to" IS NULL'
    ).fetchone()[0]
    assert nulls == tricky_tables[TableName.THRESHOLD_POLICIES]["effective_to"].isna().sum()
    stops = tricky_tables[TableName.ROUTE_STOPS]
    null_dwell = con.execute('SELECT COUNT(*) FROM "route_stops" WHERE "dwell_min" IS NULL')
    assert null_dwell.fetchone()[0] == stops["dwell_min"].isna().sum()
    temps = con.execute(
        'SELECT "temperature_c" FROM "temperature_readings" ORDER BY "reading_id"'
    ).fetchall()
    expected_temps = tricky_tables[TableName.TEMPERATURE_READINGS].sort_values("reading_id")
    assert [r[0] for r in temps] == expected_temps["temperature_c"].tolist()
    con.close()


@pytest.mark.parametrize("engine", list(SqlEngine))
def test_a_dangling_foreign_key_fails_the_load(tmp_path: Path, valid_tables, engine) -> None:
    tables = dict(valid_tables)
    loggers = tables[TableName.LOGGERS].copy()
    loggers["device_id"] = "DEV-999999"
    tables[TableName.LOGGERS] = loggers
    with pytest.raises(ExportError, match=r"rejected a statement|orphan"):
        load_scripts(engine, _scripts(tmp_path, tables))


@pytest.mark.parametrize("engine", list(SqlEngine))
def test_a_row_count_mismatch_fails_verification(tmp_path: Path, valid_tables, engine) -> None:
    paths = _scripts(tmp_path, valid_tables)
    expected = {t.value: len(f) for t, f in valid_tables.items()}
    expected[TableName.SHIPMENTS.value] += 1
    with pytest.raises(ExportError, match="shipments: 1 rows loaded, 2 in the CSV"):
        verify_loads(paths, expected, engines=[engine])


def test_ground_truth_scripts_need_the_data_scripts_first(tmp_path: Path, valid_tables) -> None:
    paths = _scripts(tmp_path, valid_tables)
    with pytest.raises(ExportError, match="rejected a statement"):
        load_scripts(SqlEngine.SQLITE, paths[2:])
