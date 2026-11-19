"""Read and write tables: the only module that touches table files on disk.

Every table is validated against its schema, sorted by its primary key and written with a fixed
text representation, so the same data always produces byte-identical files:

* timestamps: ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); dates: ``YYYY-MM-DD``;
* nulls: an empty CSV field;
* floats: Python's shortest round-trip representation (values are already rounded).

Reading converts every column back to the exact physical dtype its schema declares
(``datetime64[us, UTC]``, ``datetime.date``, ``Int64``, ``int64``, ``float64``, ``str``), so a
written table read back passes its schema and equals the original frame.
"""

import logging
from collections.abc import Iterable, Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path

import pandas as pd

from coldchain.enums import FileFormat, Layer, TableName
from coldchain.errors import TableIOError
from coldchain.schemas import (
    ISO_TIMESTAMP_FORMAT,
    PRIMARY_KEYS,
    SCHEMAS,
    TABLE_LAYERS,
    TIMESTAMP_DTYPE,
    validate,
)

logger = logging.getLogger(__name__)

DATE_FORMAT = "%Y-%m-%d"
"""Text form of a calendar date in CSV files."""

DEFAULT_FORMATS: tuple[FileFormat, ...] = (FileFormat.PARQUET, FileFormat.CSV)
"""Formats written for every table unless the caller asks otherwise."""


class ColumnKind(StrEnum):
    """Physical kind of a column, derived from its schema dtype."""

    STRING = "string"
    INTEGER = "integer"
    NULLABLE_INTEGER = "nullable_integer"
    FLOAT = "float"
    TIMESTAMP = "timestamp"
    DATE = "date"


def column_kinds(table: TableName) -> dict[str, ColumnKind]:
    """Return the physical kind of every column of a table, in schema order.

    Args:
        table: The table.

    Returns:
        Mapping column name → :class:`ColumnKind`.

    Raises:
        TableIOError: If a schema column has a dtype this module cannot read back.
    """
    kinds: dict[str, ColumnKind] = {}
    for name, column in SCHEMAS[table].columns.items():
        dtype = str(column.dtype)
        if dtype == "date":
            kinds[name] = ColumnKind.DATE
        elif dtype == str(TIMESTAMP_DTYPE):
            kinds[name] = ColumnKind.TIMESTAMP
        elif dtype == "Int64":
            kinds[name] = ColumnKind.NULLABLE_INTEGER
        elif dtype == "int64":
            kinds[name] = ColumnKind.INTEGER
        elif dtype == "float64":
            kinds[name] = ColumnKind.FLOAT
        elif dtype.startswith("str"):
            kinds[name] = ColumnKind.STRING
        else:  # pragma: no cover - guards against a future schema dtype
            raise TableIOError(f"{table.value}.{name}: unsupported dtype {dtype}")
    return kinds


def table_path(directory: Path, table: TableName, fmt: FileFormat) -> Path:
    """Return the file path of a table in a directory, e.g. ``raw/shipments.csv``."""
    return Path(directory) / f"{table.value}.{fmt.value}"


def layer_dir(output_root: Path, scale: str, layer: Layer) -> Path:
    """Return the directory of one output layer, e.g. ``output/starter/raw``."""
    return Path(output_root) / scale / layer.value


def sort_by_primary_key(table: TableName, frame: pd.DataFrame) -> pd.DataFrame:
    """Return the frame sorted by the table's primary key, with a fresh 0..n-1 index."""
    keys = list(PRIMARY_KEYS[table])
    return frame.sort_values(keys, kind="stable").reset_index(drop=True)


# --------------------------------------------------------------------------------------------
# Dtype conversion
# --------------------------------------------------------------------------------------------


def _to_dates(values: pd.Series) -> pd.Series:
    """Convert text or date-like values to an object column of ``datetime.date`` / ``None``."""
    if values.dropna().map(lambda v: type(v) is date).all():
        converted = values.astype(object)
    else:
        converted = pd.to_datetime(values, format=DATE_FORMAT).dt.date.astype(object)
    return converted.where(values.notna(), None)


def _to_timestamps(values: pd.Series) -> pd.Series:
    """Convert text or datetime values to UTC timestamps (``datetime64[us, UTC]``)."""
    if pd.api.types.is_datetime64_any_dtype(values):
        converted = values if values.dt.tz is not None else values.dt.tz_localize("UTC")
    else:
        converted = pd.to_datetime(values, format=ISO_TIMESTAMP_FORMAT, utc=True)
    return converted.dt.tz_convert("UTC").astype(TIMESTAMP_DTYPE)


def coerce_to_schema(table: TableName, frame: pd.DataFrame) -> pd.DataFrame:
    """Convert every column of a frame to the physical dtype of its schema.

    Accepts the text form written by :func:`write_table` as well as already-typed columns
    (e.g. from Parquet), so it is safe to apply to either.

    Args:
        table: The table the frame belongs to.
        frame: Frame with exactly the table's columns (in any order).

    Returns:
        A new frame with the schema's column order and dtypes.

    Raises:
        TableIOError: If the columns differ from the schema, or a value cannot be converted.
    """
    kinds = column_kinds(table)
    if set(frame.columns) != set(kinds):
        missing = sorted(set(kinds) - set(frame.columns))
        extra = sorted(set(frame.columns) - set(kinds))
        raise TableIOError(f"{table.value}: column mismatch (missing {missing}, extra {extra})")
    converters = {
        ColumnKind.STRING: lambda s: s.astype("str").where(s.notna(), None),
        ColumnKind.INTEGER: lambda s: pd.to_numeric(s).astype("int64"),
        ColumnKind.NULLABLE_INTEGER: lambda s: pd.to_numeric(s).astype("Int64"),
        ColumnKind.FLOAT: lambda s: pd.to_numeric(s).astype("float64"),
        ColumnKind.TIMESTAMP: _to_timestamps,
        ColumnKind.DATE: _to_dates,
    }
    columns = {}
    for name, kind in kinds.items():
        try:
            columns[name] = converters[kind](frame[name])
        except (ValueError, TypeError) as exc:
            raise TableIOError(f"{table.value}.{name}: cannot convert to {kind}: {exc}") from exc
    result = pd.DataFrame(columns, index=frame.index)
    for name, kind in kinds.items():  # str columns: keep pandas' str dtype after None-masking
        if kind is ColumnKind.STRING:
            result[name] = result[name].astype("str")
    return result


def _to_text(table: TableName, frame: pd.DataFrame) -> pd.DataFrame:
    """Render timestamp and date columns in their fixed text form (other columns unchanged)."""
    text = frame.copy()
    for name, kind in column_kinds(table).items():
        if kind is ColumnKind.TIMESTAMP:
            text[name] = frame[name].dt.strftime(ISO_TIMESTAMP_FORMAT)
        elif kind is ColumnKind.DATE:
            text[name] = frame[name].map(lambda v: v.isoformat() if v is not None else None)
    return text


# --------------------------------------------------------------------------------------------
# Write / read
# --------------------------------------------------------------------------------------------


def write_table(
    table: TableName,
    frame: pd.DataFrame,
    directory: Path,
    formats: Iterable[FileFormat] = DEFAULT_FORMATS,
) -> dict[FileFormat, Path]:
    """Validate, sort and write one table.

    Args:
        table: The table to write.
        frame: Its rows (any order).
        directory: Target directory (created if missing).
        formats: File formats to write.

    Returns:
        Mapping format → written path.

    Raises:
        SchemaValidationError: If the frame fails its schema (nothing is written).
    """
    ordered = sort_by_primary_key(table, frame)
    validate(table, ordered)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[FileFormat, Path] = {}
    for fmt in formats:
        path = table_path(directory, table, fmt)
        if fmt is FileFormat.PARQUET:
            ordered.to_parquet(path, index=False, engine="pyarrow")
        else:
            _to_text(table, ordered).to_csv(
                path, index=False, na_rep="", lineterminator="\n", encoding="utf-8"
            )
        paths[fmt] = path
    logger.debug("Wrote %s (%d rows) to %s", table.value, len(ordered), directory)
    return paths


def write_tables(
    tables: Mapping[TableName, pd.DataFrame],
    directory: Path,
    formats: Iterable[FileFormat] = DEFAULT_FORMATS,
) -> dict[TableName, dict[FileFormat, Path]]:
    """Write several tables into one directory (see :func:`write_table`).

    Args:
        tables: Frames keyed by table.
        directory: Target directory.
        formats: File formats to write.

    Returns:
        The written paths, keyed by table then format.
    """
    formats = tuple(formats)
    return {name: write_table(name, frame, directory, formats) for name, frame in tables.items()}


def read_table(
    table: TableName,
    directory: Path,
    fmt: FileFormat = FileFormat.PARQUET,
    *,
    check_schema: bool = True,
) -> pd.DataFrame:
    """Read one table back with its exact schema dtypes.

    Args:
        table: The table to read.
        directory: Directory holding the file.
        fmt: Which file format to read.
        check_schema: Also validate the result against the schema.

    Returns:
        The table, in the schema's column order and dtypes.

    Raises:
        TableIOError: If the file is missing, unreadable or has the wrong columns.
        SchemaValidationError: If ``check_schema`` and the data fails the schema.
    """
    path = table_path(Path(directory), table, fmt)
    if not path.is_file():
        raise TableIOError(f"Table file not found: {path}")
    try:
        if fmt is FileFormat.PARQUET:
            raw = pd.read_parquet(path, engine="pyarrow")
        else:
            raw = pd.read_csv(
                path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8"
            )
    except (OSError, ValueError) as exc:
        raise TableIOError(f"Could not read {path}: {exc}") from exc
    frame = coerce_to_schema(table, raw)
    if check_schema:
        validate(table, frame)
    return frame


def read_tables(
    tables: Iterable[TableName], directory: Path, fmt: FileFormat = FileFormat.PARQUET
) -> dict[TableName, pd.DataFrame]:
    """Read several tables from one directory (see :func:`read_table`)."""
    return {name: read_table(name, directory, fmt) for name in tables}


def raw_tables() -> tuple[TableName, ...]:
    """Return every table of the ``raw`` layer, in declaration order."""
    return tuple(t for t, layer in TABLE_LAYERS.items() if layer is Layer.RAW)
