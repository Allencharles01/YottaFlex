"""Write the export bundle: CSV and JSON per table, and SQL scripts (``coldchain export``).

Layout under ``output/<scale>/export/`` (one sub-folder per layer, so the answer key never
mixes with the input data):

```
csv/<layer>/<table>.csv          every table, the same bytes as the pipeline's own CSV files
json/<layer>/<table>.json        a JSON array of records ...
json/<layer>/<table>.jsonl       ... or JSON Lines for the tables in export.jsonl_tables
sql/schema.sql                   DDL (PKs, UNIQUEs, FKs) of the raw and compliance tables
sql/seed.sql                     their rows: batched multi-row INSERTs, one transaction
sql/ground_truth_schema.sql      DDL of the ground-truth tables (FKs into the raw tables)
sql/ground_truth_seed.sql        their rows, kept out of seed.sql on purpose
sql/<seed>.sql.gz                gzip copy of a seed file above export.sql_gzip_threshold_mb
```

Tables are validated against their schemas once and written in primary-key order; files go
through :mod:`coldchain.io`, so the same tables always give the same bytes.
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from coldchain.config import ExportConfig
from coldchain.enums import FileFormat, Layer, TableName
from coldchain.export import sql
from coldchain.io import (
    file_size,
    read_text_lines,
    table_path,
    write_table_files,
    write_text,
)
from coldchain.schemas import TABLE_LAYERS

logger = logging.getLogger(__name__)

SQL_DIR = "sql"
"""Bundle sub-folder holding the SQL scripts."""

MIB = 1024 * 1024
"""Bytes per mebibyte (the unit of ``export.sql_gzip_threshold_mb``)."""


@dataclass(frozen=True)
class SqlBundle:
    """One pair of SQL scripts and the layers whose tables they hold.

    Attributes:
        prefix: File-name prefix (``""`` → ``schema.sql``/``seed.sql``).
        layers: Layers included.
        title: Description written in the scripts' header comment.
    """

    prefix: str
    layers: tuple[Layer, ...]
    title: str

    @property
    def schema_name(self) -> str:
        """File name of the DDL script."""
        return f"{self.prefix}schema.sql"

    @property
    def seed_name(self) -> str:
        """File name of the data script."""
        return f"{self.prefix}seed.sql"


SQL_BUNDLES: tuple[SqlBundle, ...] = (
    SqlBundle("", (Layer.RAW, Layer.COMPLIANCE), "YottaFlex input data and compliance output"),
    SqlBundle(
        "ground_truth_",
        (Layer.GROUND_TRUTH,),
        "YottaFlex ground truth (answer key); load after schema.sql and seed.sql",
    ),
)
"""The SQL scripts, in load order. Ground truth has its own pair (never in ``seed.sql``)."""


@dataclass(frozen=True)
class ExportedFile:
    """One file of the bundle.

    Attributes:
        path: Path relative to the bundle root (POSIX).
        kind: ``csv``, ``json``, ``jsonl``, ``sql``, ``sql.gz``, ``md`` or ``report``.
        layer: The layer of the table(s) it holds, if any.
        table: The table it holds, if exactly one.
        rows: Number of data rows it holds, if it holds table data.
    """

    path: str
    kind: str
    layer: str | None = None
    table: str | None = None
    rows: int | None = None


def table_files(table: TableName, settings: ExportConfig) -> dict[FileFormat, str]:
    """Return the bundle-relative CSV and JSON paths of one table.

    Args:
        table: The table.
        settings: Export settings (which tables are JSON Lines).

    Returns:
        Format → relative path, e.g. ``{CSV: "csv/raw/shipments.csv", JSON: ...}``.
    """
    layer = TABLE_LAYERS[table].value
    json_format = FileFormat.JSONL if table in settings.jsonl_tables else FileFormat.JSON
    folders = {FileFormat.CSV: Path("csv") / layer, json_format: Path("json") / layer}
    return {fmt: table_path(folder, table, fmt).as_posix() for fmt, folder in folders.items()}


def write_table_exports(
    tables: Mapping[TableName, pd.DataFrame], root: Path, settings: ExportConfig
) -> tuple[list[ExportedFile], dict[TableName, pd.DataFrame]]:
    """Write every table as CSV and JSON (or JSON Lines).

    Args:
        tables: Every table of the data model.
        root: Bundle root.
        settings: Export settings.

    Returns:
        The written files and the validated, PK-sorted frames.
    """
    files: list[ExportedFile] = []
    ordered: dict[TableName, pd.DataFrame] = {}
    for table in TableName:
        paths = table_files(table, settings)
        ordered[table] = write_table_files(
            table, tables[table], {fmt: root / rel for fmt, rel in paths.items()}
        )
        layer = TABLE_LAYERS[table].value
        for fmt, rel in paths.items():
            files.append(ExportedFile(rel, fmt.value, layer, table.value, len(ordered[table])))
    return files, ordered


def write_sql_exports(
    frames: Mapping[TableName, pd.DataFrame], root: Path, settings: ExportConfig
) -> list[ExportedFile]:
    """Write the SQL scripts of every :data:`SQL_BUNDLES` entry (plus gzip copies if large).

    Args:
        frames: Every table, validated.
        root: Bundle root.
        settings: Export settings (batch size, gzip threshold).

    Returns:
        The written files, in load order.
    """
    files: list[ExportedFile] = []
    for bundle in SQL_BUNDLES:
        members = {t: frames[t] for t in TableName if TABLE_LAYERS[t] in bundle.layers}
        layers = "+".join(layer.value for layer in bundle.layers)
        schema_path = root / SQL_DIR / bundle.schema_name
        write_text(schema_path, [sql.schema_script(members, bundle.title)])
        files.append(ExportedFile(f"{SQL_DIR}/{bundle.schema_name}", "sql", layers))
        seed_path = root / SQL_DIR / bundle.seed_name
        write_text(seed_path, sql.seed_script(members, settings.sql_batch_rows, bundle.title))
        rows = sum(len(frame) for frame in members.values())
        files.append(ExportedFile(f"{SQL_DIR}/{bundle.seed_name}", "sql", layers, rows=rows))
        size = file_size(seed_path)
        logger.info("Wrote %s: %d rows, %.1f MiB", seed_path.name, rows, size / MIB)
        if size > settings.sql_gzip_threshold_mb * MIB:
            gz_path = seed_path.with_name(seed_path.name + ".gz")
            write_text(gz_path, read_text_lines(seed_path), compress=True)
            files.append(ExportedFile(f"{SQL_DIR}/{gz_path.name}", "sql.gz", layers, rows=rows))
            logger.info(
                "%s is above %.0f MiB: also wrote %s (%.1f MiB)",
                seed_path.name,
                settings.sql_gzip_threshold_mb,
                gz_path.name,
                file_size(gz_path) / MIB,
            )
    return files


def sql_load_order(files: list[ExportedFile]) -> list[str]:
    """Return the plain ``.sql`` scripts in the order they must be loaded."""
    return [f.path for f in files if f.kind == "sql"]
