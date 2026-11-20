"""Render tables as portable SQL: DDL with keys, and batched multi-row INSERT statements.

The SQL is written once and loads unchanged into DuckDB and SQLite (proved by
:mod:`coldchain.export.sql_load`). It uses only what both dialects accept:

* identifiers are double-quoted (``"shipments"``) after checking they are plain snake_case;
* column types: ``VARCHAR``, ``BIGINT``, ``DOUBLE PRECISION``, ``DATE`` and
  ``TIMESTAMP WITH TIME ZONE`` (SQLite stores the ISO-8601 text unchanged);
* every value is a literal, escaped here and only here: text is wrapped in single quotes with
  each ``'`` doubled (the SQL standard; neither dialect treats a backslash specially), nulls
  are ``NULL``, numbers are bare finite literals, and timestamps and dates use the same text
  as the CSV files. Text containing a NUL character is refused (SQLite cannot hold it in a
  statement);
* the seed script is one transaction, with tables in foreign-key dependency order and rows in
  primary-key order, so the same tables always give the same bytes.

Everything here is pure: frames in, strings out. Files are written by
:mod:`coldchain.export.exporters` through :mod:`coldchain.io`.
"""

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence

import numpy as np
import pandas as pd

from coldchain import __version__
from coldchain.config import SQLITE_MAX_SQL_LENGTH
from coldchain.enums import TableName
from coldchain.errors import ExportError
from coldchain.io import ColumnKind, column_kinds, sort_by_primary_key, to_text
from coldchain.schemas import FOREIGN_KEYS, PRIMARY_KEYS, SCHEMAS

SQL_TYPES: dict[ColumnKind, str] = {
    ColumnKind.STRING: "VARCHAR",
    ColumnKind.INTEGER: "BIGINT",
    ColumnKind.NULLABLE_INTEGER: "BIGINT",
    ColumnKind.FLOAT: "DOUBLE PRECISION",
    ColumnKind.TIMESTAMP: "TIMESTAMP WITH TIME ZONE",
    ColumnKind.DATE: "DATE",
}
"""SQL column type for every physical column kind (valid in DuckDB and SQLite)."""

NULL = "NULL"
"""The SQL null literal."""

_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")
_TEXT_KINDS = frozenset({ColumnKind.STRING, ColumnKind.TIMESTAMP, ColumnKind.DATE})


def quote_identifier(name: str) -> str:
    """Return a table or column name as a double-quoted SQL identifier.

    Args:
        name: A snake_case name (every table and column of the data model is one).

    Returns:
        The quoted identifier, e.g. ``"shipment_id"``.

    Raises:
        ExportError: If the name is not plain snake_case (defence in depth: names come from
            the schemas, never from data).
    """
    if not _IDENTIFIER.match(name):
        raise ExportError(f"Refusing to use {name!r} as an SQL identifier (not snake_case)")
    return f'"{name}"'


def text_literals(values: pd.Series) -> pd.Series:
    """Render a text column as SQL string literals (vectorised).

    Args:
        values: Text values; missing values become ``NULL``.

    Returns:
        Literals such as ``'O''Brien'``, aligned with ``values``.

    Raises:
        ExportError: If a value contains a NUL character.
    """
    present = values.notna()
    text = values[present].astype(str)
    if text.str.contains("\x00", regex=False).any():
        raise ExportError("A text value contains a NUL character, which SQL text cannot hold")
    quoted = "'" + text.str.replace("'", "''", regex=False) + "'"
    return _with_nulls(quoted, present)


def number_literals(values: pd.Series, kind: ColumnKind) -> pd.Series:
    """Render a numeric column as SQL number literals (vectorised).

    Integers are written in full; floats use the shortest text that reads back as the same
    value (e.g. ``5.12``, ``-24.0``, ``1e+16``).

    Args:
        values: Numeric values; missing values (``NA``, and ``NaN`` in a float column) become
            ``NULL``.
        kind: The column's physical kind (an integer or float kind).

    Returns:
        Literals aligned with ``values``.

    Raises:
        ExportError: If a float is infinite (SQL has no literal for it).
    """
    present = values.notna()
    numbers = values[present]
    if kind is ColumnKind.FLOAT:
        array = numbers.to_numpy(dtype="float64")
        if not np.isfinite(array).all():
            raise ExportError("A float value is infinite, which SQL cannot represent")
        rendered = pd.Series(array.astype(str), index=numbers.index)
    else:
        rendered = numbers.astype("int64").astype(str)
    return _with_nulls(rendered, present)


def _with_nulls(rendered: pd.Series, present: pd.Series) -> pd.Series:
    """Place rendered literals at the present positions and ``NULL`` everywhere else."""
    result = pd.Series(NULL, index=present.index, dtype=object)
    result[present] = rendered.to_numpy(dtype=object)
    return result


def row_literals(table: TableName, frame: pd.DataFrame) -> pd.Series:
    """Render every row of a table as an SQL value tuple, e.g. ``('PTY-0001', 'SUPPLIER', …)``.

    Args:
        table: The table.
        frame: Its rows, with the schema's dtypes.

    Returns:
        One string per row, in the frame's order.
    """
    if frame.empty:
        return pd.Series([], dtype=object)
    text = to_text(table, frame)
    columns = []
    for name, kind in column_kinds(table).items():
        if kind in _TEXT_KINDS:
            columns.append(text_literals(text[name]))
        else:
            columns.append(number_literals(frame[name], kind))
    joined = columns[0].str.cat(columns[1:], sep=", ")
    return "(" + joined + ")"


def dependency_order(tables: Iterable[TableName]) -> list[TableName]:
    """Order tables so that every foreign-key parent comes before its children.

    Only relationships between the given tables count (a parent outside the set is assumed to
    be loaded already). Ties keep :class:`TableName` declaration order, so the order is fixed.

    Args:
        tables: The tables to order.

    Returns:
        The tables in a valid load order.

    Raises:
        ExportError: If the foreign keys form a cycle.
    """
    wanted = set(tables)
    remaining = [t for t in TableName if t in wanted]
    parents = {
        t: {parent for child, _, parent, _ in FOREIGN_KEYS if child is t and parent in remaining}
        - {t}
        for t in remaining
    }
    ordered: list[TableName] = []
    while remaining:
        ready = [t for t in remaining if parents[t] <= set(ordered)]
        if not ready:
            raise ExportError(f"Foreign keys form a cycle among {[t.value for t in remaining]}")
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


def create_table(table: TableName) -> str:
    """Return the ``CREATE TABLE`` statement of one table.

    Columns keep schema order and types (:data:`SQL_TYPES`); required columns are
    ``NOT NULL``. The statement declares the primary key, a ``UNIQUE`` constraint per
    schema-unique column, and every foreign key of :data:`coldchain.schemas.FOREIGN_KEYS`.

    Args:
        table: The table.

    Returns:
        The statement, ending with ``;`` and a newline.
    """
    schema = SCHEMAS[table]
    lines = []
    for name, kind in column_kinds(table).items():
        null = "" if schema.columns[name].nullable else " NOT NULL"
        lines.append(f"{quote_identifier(name)} {SQL_TYPES[kind]}{null}")
    keys = ", ".join(quote_identifier(c) for c in PRIMARY_KEYS[table])
    lines.append(f"PRIMARY KEY ({keys})")
    for name, column in schema.columns.items():
        if column.unique and (name,) != PRIMARY_KEYS[table]:
            lines.append(f"UNIQUE ({quote_identifier(name)})")
    for child, column, parent, parent_column in FOREIGN_KEYS:
        if child is table:
            lines.append(
                f"FOREIGN KEY ({quote_identifier(column)}) REFERENCES "
                f"{quote_identifier(parent.value)} ({quote_identifier(parent_column)})"
            )
    body = ",\n".join(f"    {line}" for line in lines)
    return f"CREATE TABLE {quote_identifier(table.value)} (\n{body}\n);\n"


def schema_script(tables: Iterable[TableName], title: str) -> str:
    """Return a DDL script creating the given tables in dependency order.

    Args:
        tables: Tables to create.
        title: One-line description placed in the header comment.

    Returns:
        The script text.
    """
    ordered = dependency_order(tables)
    header = _header(title, ordered)
    return header + "\n".join(create_table(t) for t in ordered)


def insert_statements(table: TableName, frame: pd.DataFrame, batch_rows: int) -> Iterator[str]:
    """Yield batched multi-row ``INSERT`` statements for one table, in primary-key order.

    Args:
        table: The table.
        frame: Its rows (any order).
        batch_rows: Maximum rows per statement.

    Yields:
        One statement per batch, each ending with ``;`` and a newline.

    Raises:
        ExportError: If ``batch_rows`` < 1, or a statement exceeds SQLite's statement-length
            limit (lower ``export.sql_batch_rows``).
    """
    if batch_rows < 1:
        raise ExportError(f"batch_rows must be >= 1, got {batch_rows}")
    rows = row_literals(table, sort_by_primary_key(table, frame)).tolist()
    columns = ", ".join(quote_identifier(c) for c in SCHEMAS[table].columns)
    prefix = f"INSERT INTO {quote_identifier(table.value)} ({columns}) VALUES\n"
    for start in range(0, len(rows), batch_rows):
        statement = prefix + ",\n".join(rows[start : start + batch_rows]) + ";\n"
        size = len(statement.encode("utf-8"))
        if size > SQLITE_MAX_SQL_LENGTH:
            raise ExportError(
                f"An INSERT into {table.value} is {size} bytes, above SQLite's "
                f"{SQLITE_MAX_SQL_LENGTH}-byte limit: lower export.sql_batch_rows"
            )
        yield statement


def seed_script(
    frames: Mapping[TableName, pd.DataFrame], batch_rows: int, title: str
) -> Iterator[str]:
    """Yield a data script: one transaction inserting every table in dependency order.

    Args:
        frames: Rows per table.
        batch_rows: Maximum rows per ``INSERT``.
        title: One-line description placed in the header comment.

    Yields:
        Text chunks; concatenated, they form the script.
    """
    ordered = dependency_order(frames)
    yield _header(title, ordered)
    yield "BEGIN TRANSACTION;\n"
    for table in ordered:
        yield f"\n-- {table.value}: {len(frames[table])} rows\n"
        yield from insert_statements(table, frames[table], batch_rows)
    yield "\nCOMMIT;\n"


def _header(title: str, ordered: Sequence[TableName]) -> str:
    """Header comment of a script: what it is, who wrote it and the table order."""
    tables = ", ".join(t.value for t in ordered)
    return (
        f"-- {title}\n"
        f"-- Generated by coldchain {__version__}. Portable SQL for DuckDB and SQLite;\n"
        "-- SQLite enforces foreign keys only after `PRAGMA foreign_keys = ON;`.\n"
        f"-- Tables, in load order: {tables}\n\n"
    )
