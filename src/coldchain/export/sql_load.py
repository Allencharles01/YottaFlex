"""Prove that the exported SQL loads into fresh DuckDB and SQLite databases (BACKLOG #4).

For each engine, :func:`load_scripts` creates a new in-memory database, turns foreign-key
enforcement on (SQLite needs ``PRAGMA foreign_keys = ON``; DuckDB always enforces it), and
runs the scripts in order, one statement at a time. Then it collects four kinds of evidence:

* the row count of every table;
* the number of rows whose foreign key has no parent, counted per relationship with an
  anti-join (it must be 0; SQLite also runs ``PRAGMA foreign_key_check``);
* a probe proving the session really rejects a violating row;
* the engine's version.

:func:`verify_loads` compares the counts with the CSV files and fails on any difference.
Statements are split with :func:`sqlite3.complete_statement`, a real tokenizer, so a ``;``
inside a quoted value never splits a statement. Scripts are streamed line by line, never
loaded whole.
"""

import logging
import sqlite3
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import duckdb

from coldchain.enums import SqlEngine, TableName
from coldchain.errors import ExportError
from coldchain.export.sql import quote_identifier
from coldchain.io import read_text_lines
from coldchain.schemas import FOREIGN_KEYS

logger = logging.getLogger(__name__)

_PROBE_PARENT = "fk_probe_parent"
_PROBE_CHILD = "fk_probe_child"


class _Connection(Protocol):
    """The part of a DB-API connection this module uses (DuckDB and sqlite3 both have it)."""

    def execute(self, sql: str) -> Any:  # pragma: no cover - typing only
        """Run one statement."""


@dataclass(frozen=True)
class LoadResult:
    """What loading the SQL into one engine showed.

    Attributes:
        engine: The database engine.
        version: Its version string.
        row_counts: Rows per loaded table.
        foreign_key_violations: Child rows without a parent, per ``child.column`` (all 0 when
            the data is consistent).
        enforcement_probe: ``True`` if the session rejected a deliberately violating row.
    """

    engine: SqlEngine
    version: str
    row_counts: dict[str, int]
    foreign_key_violations: dict[str, int]
    enforcement_probe: bool

    def as_dict(self) -> dict[str, Any]:
        """Return the result as a JSON-ready mapping."""
        return {
            "engine": self.engine.value,
            "version": self.version,
            "foreign_keys_enforced": self.enforcement_probe,
            "foreign_key_violations": sum(self.foreign_key_violations.values()),
            "row_counts": dict(sorted(self.row_counts.items())),
        }


def iter_statements(lines: Iterable[str]) -> Iterator[str]:
    """Split SQL text into complete statements.

    Lines are accumulated until they form a complete statement (checked only when a line
    ends with ``;``, so a long multi-row INSERT is tokenized once). Comment-only text between
    statements is dropped.

    Args:
        lines: The script's lines, each with its newline.

    Yields:
        One statement at a time.

    Raises:
        ExportError: If the script ends in the middle of a statement.
    """
    buffer: list[str] = []
    for line in lines:
        buffer.append(line)
        if line.rstrip().endswith(";"):
            text = "".join(buffer)
            if sqlite3.complete_statement(text):
                buffer.clear()
                yield text
    rest = "".join(buffer)
    if any(part.strip() and not part.lstrip().startswith("--") for part in rest.splitlines()):
        raise ExportError("The SQL script ends with an incomplete statement")


def _connect(engine: SqlEngine) -> tuple[_Connection, str]:
    """Open a fresh in-memory database with foreign keys enforced."""
    if engine is SqlEngine.DUCKDB:
        connection = duckdb.connect(database=":memory:")
        return connection, duckdb.__version__
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection, sqlite3.sqlite_version


def _scalar(connection: _Connection, sql: str) -> int:
    """Run a query that returns one integer."""
    return int(connection.execute(sql).fetchone()[0])


def _violations(connection: _Connection, tables: set[str]) -> dict[str, int]:
    """Count child rows whose foreign key has no parent, per relationship."""
    counts = {}
    for child, column, parent, parent_column in FOREIGN_KEYS:
        if child.value not in tables or parent.value not in tables:
            continue
        c, col = quote_identifier(child.value), quote_identifier(column)
        p, pcol = quote_identifier(parent.value), quote_identifier(parent_column)
        counts[f"{child.value}.{column}"] = _scalar(
            connection,
            f"SELECT COUNT(*) FROM {c} AS c LEFT JOIN {p} AS p ON c.{col} = p.{pcol} "
            f"WHERE c.{col} IS NOT NULL AND p.{pcol} IS NULL",
        )
    return counts


def _probe_enforcement(connection: _Connection) -> bool:
    """Return ``True`` if inserting a child row without a parent is rejected."""
    parent, child = quote_identifier(_PROBE_PARENT), quote_identifier(_PROBE_CHILD)
    connection.execute(f'CREATE TABLE {parent} ("id" VARCHAR NOT NULL, PRIMARY KEY ("id"))')
    connection.execute(
        f'CREATE TABLE {child} ("id" VARCHAR NOT NULL, "parent_id" VARCHAR NOT NULL, '
        f'PRIMARY KEY ("id"), FOREIGN KEY ("parent_id") REFERENCES {parent} ("id"))'
    )
    try:
        connection.execute(f"INSERT INTO {child} VALUES ('c', 'missing')")
    except (sqlite3.Error, duckdb.Error):
        return True
    return False


def load_scripts(engine: SqlEngine, scripts: Sequence[Path]) -> LoadResult:
    """Load SQL scripts, in order, into a fresh in-memory database of one engine.

    Args:
        engine: The database engine.
        scripts: Script files (``.sql``, or ``.sql.gz``), e.g. schema then seed.

    Returns:
        The evidence collected after loading.

    Raises:
        ExportError: If a statement fails, a foreign key is violated, or (SQLite)
            ``PRAGMA foreign_key_check`` reports a row.
    """
    connection, version = _connect(engine)
    try:
        statements = 0
        for script in scripts:
            for statement in iter_statements(read_text_lines(script)):
                try:
                    connection.execute(statement)
                except (sqlite3.Error, duckdb.Error) as exc:
                    raise ExportError(
                        f"{engine.value} {version} rejected a statement in {Path(script).name}: "
                        f"{str(exc)[:300]}"
                    ) from exc
                statements += 1
        loaded = {t.value for t in TableName} & set(_table_names(connection, engine))
        counts = {
            name: _scalar(connection, f"SELECT COUNT(*) FROM {quote_identifier(name)}")
            for name in sorted(loaded)
        }
        violations = _violations(connection, loaded)
        if engine is SqlEngine.SQLITE:
            unchecked = connection.execute("PRAGMA foreign_key_check").fetchall()
            violations["pragma_foreign_key_check"] = len(unchecked)
        probe = _probe_enforcement(connection)
    finally:
        connection.close()
    logger.info(
        "%s %s: %d statements, %d tables, %d rows, %d foreign-key violations, enforced: %s",
        engine.value,
        version,
        statements,
        len(counts),
        sum(counts.values()),
        sum(violations.values()),
        probe,
    )
    return LoadResult(engine, version, counts, violations, probe)


def _table_names(connection: _Connection, engine: SqlEngine) -> list[str]:
    """List the tables of the database."""
    if engine is SqlEngine.DUCKDB:
        query = "SELECT table_name FROM information_schema.tables"
    else:
        query = "SELECT name FROM sqlite_master WHERE type = 'table'"
    return [row[0] for row in connection.execute(query).fetchall()]


def verify_loads(
    scripts: Sequence[Path],
    expected_rows: Mapping[str, int],
    engines: Iterable[SqlEngine] = tuple(SqlEngine),
) -> dict[str, Any]:
    """Load the scripts into every engine and compare the row counts with the expected ones.

    Args:
        scripts: Script files in load order.
        expected_rows: Table → number of rows (counted from its CSV file).
        engines: Engines to prove.

    Returns:
        A JSON-ready summary per engine (``status`` is always ``PASS``; failures raise).

    Raises:
        ExportError: If a load fails, foreign keys are violated or not enforced, or any row
            count differs from ``expected_rows`` (including a missing or extra table).
    """
    summary: dict[str, Any] = {}
    for engine in engines:
        result = load_scripts(engine, scripts)
        problems = [
            f"{table}: {result.row_counts.get(table)} rows loaded, {rows} in the CSV"
            for table, rows in sorted(expected_rows.items())
            if result.row_counts.get(table) != rows
        ]
        problems += [
            f"{t}: loaded but has no CSV" for t in result.row_counts if t not in expected_rows
        ]
        problems += [
            f"{fk}: {n} orphan rows" for fk, n in result.foreign_key_violations.items() if n
        ]
        if not result.enforcement_probe:
            problems.append("foreign keys are not enforced by the session")
        if problems:
            raise ExportError(f"{engine.value} load check failed: " + "; ".join(problems[:5]))
        summary[engine.value] = {"status": "PASS", **result.as_dict()}
    return summary
