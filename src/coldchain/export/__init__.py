"""Export (``coldchain export``): the audit bundle of one scale, with its manifest.

:func:`write_bundle` writes every table as CSV and JSON (JSON Lines for the large reading
table), the portable SQL scripts (:mod:`.sql`), copies of the reports and the data dictionary,
and a README. The CLI then proves that the SQL loads (:mod:`.sql_load`) and writes
``manifest.json`` (:mod:`.manifest`). Layout and rules: ``docs/data_dictionary.md``
("Export bundle") and DECISIONS.md #97-#104.
"""

from collections.abc import Mapping
from pathlib import Path

import pandas as pd

from coldchain.config import ExportConfig
from coldchain.enums import TableName
from coldchain.export.exporters import (
    ExportedFile,
    write_sql_exports,
    write_table_exports,
)
from coldchain.export.manifest import README_NAME, render_readme
from coldchain.io import copy_file, write_text

EXPORT_TABLES: tuple[TableName, ...] = tuple(TableName)
"""Every table of the data model is exported (each layer to its own folders)."""


def write_bundle(
    tables: Mapping[TableName, pd.DataFrame],
    root: Path,
    settings: ExportConfig,
    *,
    scale: str,
    seed: int | None,
    attachments: Mapping[str, Path],
) -> list[ExportedFile]:
    """Write the whole bundle (everything except ``manifest.json``) into ``root``.

    Args:
        tables: Every table of :data:`EXPORT_TABLES`.
        root: Bundle folder (created if missing).
        settings: Export settings.
        scale: Scale name (for the README).
        seed: The generator's seed (for the README).
        attachments: Bundle-relative path → source file to copy (reports, docs).

    Returns:
        Every file written, data files first.
    """
    root = Path(root)
    files, frames = write_table_exports(tables, root, settings)
    files += write_sql_exports(frames, root, settings)
    for relative, source in sorted(attachments.items()):
        copy_file(source, root / relative)
        kind = "md" if relative.endswith(".md") else "report"
        files.append(ExportedFile(relative, kind))
    readme = render_readme(scale=scale, seed=seed, files=files, settings=settings)
    write_text(root / README_NAME, [readme])
    files.append(ExportedFile(README_NAME, "md"))
    return files
