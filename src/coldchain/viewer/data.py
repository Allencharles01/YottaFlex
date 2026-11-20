"""Locate and read the pipeline output for the viewer (read-only; DECISIONS.md #122).

Every read goes through :mod:`coldchain.io`. Nothing here writes a file, and nothing imports
a pipeline stage. The viewer only shows what ``coldchain run-all`` (and optionally
``coldchain reproduce``) already wrote.
"""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from coldchain.enums import FileFormat, Layer, ReportKind, TableName
from coldchain.io import (
    layer_dir,
    read_json,
    read_table,
    read_table_rows,
    report_dir,
    report_paths,
    table_path,
)
from coldchain.schemas import TABLE_LAYERS

VALIDATION_STEM = "validation_summary"
EVALUATION_STEM = "evaluation_report"
REPRODUCIBILITY_STEM = "reproducibility_check"
BASELINE_SUBDIR = "baseline"
RUN_SUMMARY_FILE = "run_summary.json"
MANIFEST_FILE = Path("export") / "manifest.json"

VIEWER_TABLES: tuple[TableName, ...] = (
    TableName.PARTIES,
    TableName.THRESHOLD_POLICIES,
    TableName.PRODUCTS,
    TableName.PACKAGING,
    TableName.LOTS,
    TableName.DEVICES,
    TableName.SHIPMENTS,
    TableName.SHIPMENT_LOTS,
    TableName.LOGGERS,
    TableName.TEMPERATURE_READINGS,
    TableName.ROUTE_STOPS,
    TableName.CUSTODY_EVENTS,
    TableName.SHIPMENT_STATUS_EVENTS,
    TableName.COMPLIANCE_DECISIONS,
    TableName.EXCURSION_REPORTS,
    TableName.EXCURSION_REPORT_READINGS,
    TableName.COMPLIANCE_STATUS_EVENTS,
    TableName.EXPECTED_OUTCOMES,
    TableName.ANOMALIES,
)
"""Tables the viewer needs; ``temperature_readings`` is only ever read per shipment."""


def table_file(output_root: Path, scale: str, table: TableName) -> Path:
    """Return the Parquet file of a table, e.g. ``output/starter/raw/shipments.parquet``."""
    return table_path(layer_dir(output_root, scale, TABLE_LAYERS[table]), table, FileFormat.PARQUET)


def report_file(output_root: Path, scale: str, kind: ReportKind, stem: str, sub: str = "") -> Path:
    """Return a report's JSON file, e.g. ``output/starter/validation/validation_summary.json``.

    Args:
        output_root: The output root (``output/``).
        scale: The scale name.
        kind: Which report folder.
        stem: File stem of the report.
        sub: Optional sub-folder (``baseline`` for the clean-snapshot validation).
    """
    directory = report_dir(output_root, scale, kind)
    return report_paths(directory / sub if sub else directory, stem)[1]


def required_files(output_root: Path, scale: str) -> list[Path]:
    """Return every file the viewer cannot work without (written by ``coldchain run-all``)."""
    tables = [table_file(output_root, scale, table) for table in VIEWER_TABLES]
    reports = [
        report_file(output_root, scale, ReportKind.VALIDATION, VALIDATION_STEM),
        report_file(output_root, scale, ReportKind.EVALUATION, EVALUATION_STEM),
    ]
    return tables + reports


def missing_files(output_root: Path, scale: str) -> list[Path]:
    """Return the required files that do not exist (empty when the output is complete)."""
    return [path for path in required_files(output_root, scale) if not path.is_file()]


def file_signature(path: Path) -> tuple[int, int] | None:
    """Return ``(mtime_ns, size)`` of a file, or ``None`` if it does not exist.

    Used as part of the cache key, so a re-run of the pipeline is picked up without a restart.
    """
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def load_table(output_root: Path, scale: str, table: TableName) -> pd.DataFrame:
    """Read a whole table of the given scale (schema-checked, see :func:`coldchain.io.read_table`).

    Raises:
        TableIOError: If the file is missing or unreadable.
    """
    directory = layer_dir(output_root, scale, TABLE_LAYERS[table])
    return read_table(table, directory)


def load_readings(output_root: Path, scale: str, logger_ids: Iterable[str]) -> pd.DataFrame:
    """Read only the temperature readings of the given loggers, sorted by ``recorded_at``.

    Raises:
        TableIOError: If the readings file is missing or unreadable.
    """
    directory = layer_dir(output_root, scale, Layer.RAW)
    frame = read_table_rows(TableName.TEMPERATURE_READINGS, directory, "logger_id", logger_ids)
    return frame.sort_values(["recorded_at", "reading_id"], kind="stable").reset_index(drop=True)


def load_json_if_present(path: Path) -> dict[str, Any] | None:
    """Read a JSON object, or return ``None`` when the file does not exist.

    Raises:
        TableIOError: If the file exists but is not a readable JSON object.
    """
    return read_json(path) if Path(path).is_file() else None


def optional_documents(output_root: Path, scale: str) -> dict[str, Path]:
    """Return the optional JSON documents a page may show, by name.

    ``baseline_validation``, ``reproducibility``, ``run_summary`` and ``manifest``; each one may
    be missing (for example ``reproducibility`` until ``coldchain reproduce`` has run).
    """
    scale_root = Path(output_root) / scale
    return {
        "baseline_validation": report_file(
            output_root, scale, ReportKind.VALIDATION, VALIDATION_STEM, BASELINE_SUBDIR
        ),
        "reproducibility": report_file(
            output_root, scale, ReportKind.REPRODUCIBILITY, REPRODUCIBILITY_STEM
        ),
        "run_summary": scale_root / RUN_SUMMARY_FILE,
        "manifest": scale_root / MANIFEST_FILE,
    }
