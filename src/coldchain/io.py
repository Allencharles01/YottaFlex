"""Read and write tables: the only module that touches table files on disk.

Every table is validated against its schema, sorted by its primary key and written with a fixed
text representation, so the same data always produces byte-identical files:

* timestamps: ``YYYY-MM-DDTHH:MM:SSZ`` (UTC); dates: ``YYYY-MM-DD``;
* nulls: an empty CSV field, ``null`` in JSON;
* floats: Python's shortest round-trip representation (values are already rounded).

Parquet and CSV are written for every table; JSON (an array of records) only where a phase asks
for it (``ground_truth/anomalies``).

The module also keeps the **baseline snapshot** used by the injector: before the first
``coldchain inject``, the generator's clean ``raw/`` tables and ``expected_outcomes`` are copied
byte for byte to ``<scale>/baseline/{raw,ground_truth}/``, so injection always starts from the
clean data and a diff against the baseline proves which records it changed (DECISIONS.md #67).

Reading converts every column back to the exact physical dtype its schema declares
(``datetime64[us, UTC]``, ``datetime.date``, ``Int64``, ``int64``, ``float64``, ``str``), so a
written table read back passes its schema and equals the original frame.
"""

import hashlib
import json
import logging
import shutil
from collections.abc import Iterable, Mapping
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import pandas as pd

from coldchain.enums import FileFormat, Layer, ReportKind, TableName
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

BASELINE_DIR = "baseline"
"""Sub-folder of ``<root>/<scale>/`` holding the clean pre-injection snapshot."""

BASELINE_TABLES: tuple[TableName, ...] = (
    *(t for t, layer in TABLE_LAYERS.items() if layer is Layer.RAW),
    TableName.EXPECTED_OUTCOMES,
)
"""Tables copied into the baseline snapshot: every raw table and the natural answer key."""


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


def report_dir(output_root: Path, scale: str, kind: ReportKind) -> Path:
    """Return a report folder, e.g. ``output/starter/validation``."""
    return Path(output_root) / scale / kind.value


def baseline_dir(output_root: Path, scale: str, layer: Layer) -> Path:
    """Return a layer directory of the baseline snapshot, e.g. ``output/starter/baseline/raw``."""
    return Path(output_root) / scale / BASELINE_DIR / layer.value


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
        elif fmt is FileFormat.CSV:
            _to_text(table, ordered).to_csv(
                path, index=False, na_rep="", lineterminator="\n", encoding="utf-8"
            )
        else:
            text = _to_text(table, ordered).to_json(orient="records", indent=2, force_ascii=False)
            path.write_text(text + "\n", encoding="utf-8")
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


def write_layers(
    tables: Mapping[TableName, pd.DataFrame],
    output_root: Path,
    scale: str,
    formats: Iterable[FileFormat] = DEFAULT_FORMATS,
) -> dict[Layer, Path]:
    """Write tables into their layer directories, e.g. ``raw/`` and ``ground_truth/``.

    Args:
        tables: Frames keyed by table (any mix of layers).
        output_root: Output root (``output/`` by default in the CLI).
        scale: Scale name (sub-directory of the root).
        formats: File formats to write.

    Returns:
        The directory written for every layer that had tables.
    """
    formats = tuple(formats)
    written: dict[Layer, Path] = {}
    for layer in Layer:
        members = {name: frame for name, frame in tables.items() if TABLE_LAYERS[name] is layer}
        if members:
            written[layer] = layer_dir(output_root, scale, layer)
            write_tables(members, written[layer], formats)
    return written


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
        elif fmt is FileFormat.CSV:
            raw = pd.read_csv(
                path, dtype=str, keep_default_na=False, na_values=[""], encoding="utf-8"
            )
        else:
            records = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(records, list):
                raise ValueError("expected a JSON array of records")
            raw = pd.DataFrame.from_records(records, columns=list(SCHEMAS[table].columns))
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


# --------------------------------------------------------------------------------------------
# Baseline snapshot (injection)
# --------------------------------------------------------------------------------------------


def ensure_baseline(output_root: Path, scale: str) -> bool:
    """Make sure the clean baseline snapshot exists, copying it from the generator output.

    On the first call after ``coldchain generate``, every :data:`BASELINE_TABLES` file (all
    formats present) is copied byte for byte from ``raw/`` and ``ground_truth/`` into
    ``baseline/raw/`` and ``baseline/ground_truth/``. Later calls leave the snapshot untouched,
    so a re-run of the injector starts again from the same clean data.

    Args:
        output_root: Output root.
        scale: Scale name.

    Returns:
        ``True`` if the snapshot was created now, ``False`` if it already existed.

    Raises:
        TableIOError: If the generator output (or an existing snapshot) is incomplete.
    """
    targets = {layer: baseline_dir(output_root, scale, layer) for layer in _snapshot_layers()}
    if baseline_root(output_root, scale).exists():
        for name in BASELINE_TABLES:
            _require_parquet(baseline_dir(output_root, scale, TABLE_LAYERS[name]), name)
        return False
    sources = {layer: layer_dir(output_root, scale, layer) for layer in targets}
    for name in BASELINE_TABLES:
        _require_parquet(sources[TABLE_LAYERS[name]], name)
    for name in BASELINE_TABLES:
        layer = TABLE_LAYERS[name]
        targets[layer].mkdir(parents=True, exist_ok=True)
        for fmt in FileFormat:
            source = table_path(sources[layer], name, fmt)
            if source.is_file():
                shutil.copyfile(source, table_path(targets[layer], name, fmt))
    logger.info("Saved the clean baseline snapshot to %s", baseline_root(output_root, scale))
    return True


def baseline_root(output_root: Path, scale: str) -> Path:
    """Return the baseline snapshot folder, e.g. ``output/starter/baseline``."""
    return Path(output_root) / scale / BASELINE_DIR


def clear_injection(output_root: Path, scale: str) -> list[Path]:
    """Remove the outputs of a previous injection (baseline snapshot and anomalies).

    Called by ``coldchain generate``: freshly generated data makes an older snapshot and answer
    key stale.

    Args:
        output_root: Output root.
        scale: Scale name.

    Returns:
        The paths that were removed.
    """
    removed: list[Path] = []
    root = baseline_root(output_root, scale)
    if root.exists():
        shutil.rmtree(root)
        removed.append(root)
    truth = layer_dir(output_root, scale, Layer.GROUND_TRUTH)
    for fmt in FileFormat:
        path = table_path(truth, TableName.ANOMALIES, fmt)
        if path.is_file():
            path.unlink()
            removed.append(path)
    return removed


def _snapshot_layers() -> tuple[Layer, ...]:
    """Layers that hold at least one :data:`BASELINE_TABLES` table."""
    return tuple(dict.fromkeys(TABLE_LAYERS[name] for name in BASELINE_TABLES))


def _require_parquet(directory: Path, table: TableName) -> None:
    """Fail with a clear message if a table's Parquet file is missing."""
    path = table_path(directory, table, FileFormat.PARQUET)
    if not path.is_file():
        raise TableIOError(f"Table file not found: {path} (run `coldchain generate` first)")


# --------------------------------------------------------------------------------------------
# Reports (validation summary, evaluation report)
# --------------------------------------------------------------------------------------------


def file_sha256(path: Path) -> str:
    """Return the hex sha256 of a file's bytes.

    Raises:
        TableIOError: If the file cannot be read.
    """
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError as exc:
        raise TableIOError(f"Could not read {path}: {exc}") from exc


def write_report(
    directory: Path, stem: str, markdown: str, payload: Mapping[str, Any]
) -> tuple[Path, Path]:
    """Write a report as ``<stem>.md`` and ``<stem>.json`` (UTF-8, LF line endings).

    Args:
        directory: Target directory (created if missing).
        stem: File stem, e.g. ``validation_summary``.
        markdown: The Markdown text.
        payload: JSON-serialisable content, written in its own key order.

    Returns:
        The Markdown and JSON paths.
    """
    md_path, json_path = report_paths(directory, stem)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(markdown, encoding="utf-8", newline="\n")
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    json_path.write_text(text + "\n", encoding="utf-8", newline="\n")
    logger.debug("Wrote report %s to %s", stem, md_path.parent)
    return md_path, json_path


def report_paths(directory: Path, stem: str) -> tuple[Path, Path]:
    """Return the Markdown and JSON paths of a report."""
    directory = Path(directory)
    return directory / f"{stem}.md", directory / f"{stem}.{FileFormat.JSON.value}"


def read_report(directory: Path, stem: str) -> dict[str, Any]:
    """Read a report's JSON payload.

    Raises:
        TableIOError: If the file is missing or is not a JSON object.
    """
    _, path = report_paths(directory, stem)
    if not path.is_file():
        raise TableIOError(f"Report not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TableIOError(f"Could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TableIOError(f"{path}: expected a JSON object")
    return payload
