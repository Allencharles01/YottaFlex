"""Build the export manifest and the bundle README (pure: values in, JSON/Markdown out).

``manifest.json`` makes every exported file traceable to the run that produced it:

* the scale, the seed, the ``export`` command line, the package version, and the git commit
  with its dirty flag;
* the pipeline config fingerprint (validated settings without ``export:`` and ``seed``, and
  every reference file), and the ``export:`` settings with their own hash;
* the ``pipeline``: each stage's command, seed, version, commit and config hash, taken from
  its provenance record after :func:`coldchain.provenance.verify_chain` has proved the chain;
* the sha256 of every input file (pipeline tables and reports);
* every output file with its row count, size and sha256;
* the SQL load proof.

Two hashes summarise it:

* ``dataset_sha256`` covers only the exported files. Two runs of the same code with the same
  seed and config give the same value, whatever the command line or output folder. The bundle
  also holds a copy of the data dictionary, so a docs-only commit changes it too.
* ``content_sha256`` covers the whole manifest except the ``run`` block. That block holds the
  only wall-clock value (``generated_at``), so a rerun's ``content_sha256`` stays comparable.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from coldchain import __version__
from coldchain.config import ExportConfig
from coldchain.export.exporters import SQL_BUNDLES, SQL_DIR, ExportedFile
from coldchain.provenance import combined_sha256

MANIFEST_NAME = "manifest.json"
"""File name of the manifest at the bundle root (not listed in its own ``files``)."""

README_NAME = "README.md"
"""File name of the bundle's README."""

MANIFEST_VERSION = 1
"""Version of the manifest layout."""

RUN_KEY = "run"
"""Manifest block left out of ``content_sha256`` (wall-clock time of the export)."""

_SELF_KEY = "content_sha256"


def file_entry(file: ExportedFile, sha256: str, size: int) -> dict[str, Any]:
    """Describe one bundle file for the manifest.

    Args:
        file: The file.
        sha256: Its hex sha256.
        size: Its size in bytes.

    Returns:
        ``{"path", "kind", "layer", "table", "rows", "bytes", "sha256"}``.
    """
    return {
        "path": file.path,
        "kind": file.kind,
        "layer": file.layer,
        "table": file.table,
        "rows": file.rows,
        "bytes": size,
        "sha256": sha256,
    }


def pipeline_entry(record: Mapping[str, Any]) -> dict[str, Any]:
    """Summarise one stage's provenance record for the manifest (without file digests)."""
    return {
        "stage": record["stage"],
        "command": record["command"],
        "seed": record.get("seed"),
        "package_version": record["package_version"],
        "git": record["git"],
        "config_sha256": record["config"]["sha256"],
    }


def content_sha256(manifest: Mapping[str, Any]) -> str:
    """Hash a manifest's content: canonical JSON without the ``run`` block and this hash.

    Args:
        manifest: The manifest (with or without ``content_sha256`` and ``run``).

    Returns:
        The hex sha256 of the sorted-key, compact JSON rendering.
    """
    core = {k: v for k, v in manifest.items() if k not in (RUN_KEY, _SELF_KEY)}
    text = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_manifest(
    *,
    scale: str,
    seed: int | None,
    command: str,
    git: Mapping[str, Any],
    config: Mapping[str, Any],
    export_settings: Mapping[str, Any],
    pipeline: Sequence[Mapping[str, Any]],
    inputs: Mapping[str, str],
    entries: Iterable[Mapping[str, Any]],
    sql: Mapping[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    """Assemble the manifest.

    Args:
        scale: Scale name.
        seed: The generator's seed (from its provenance record).
        command: The ``export`` command line.
        git: Repository state at export time.
        config: Pipeline config fingerprint (without the ``export:`` settings).
        export_settings: The ``export:`` settings and their own hash.
        pipeline: One entry per stage (:func:`pipeline_entry`), in pipeline order.
        inputs: Input path (relative to the output root) → sha256.
        entries: One entry per bundle file (:func:`file_entry`).
        sql: SQL settings and the load proof.
        generated_at: Wall-clock export time (UTC ISO-8601); kept in the ``run`` block only.

    Returns:
        The manifest, ending with ``content_sha256`` and the ``run`` block.
    """
    files = sorted((dict(e) for e in entries), key=lambda e: e["path"])
    rows: dict[str, int] = {}
    for entry in files:
        if entry["table"] is not None and entry["kind"] == "csv":
            rows[entry["table"]] = entry["rows"]
    manifest: dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "package": {"name": "coldchain", "version": __version__},
        "scale": scale,
        "seed": seed,
        "command": command,
        "git": dict(git),
        "config": dict(config),
        "export_settings": dict(export_settings),
        "pipeline": [dict(p) for p in pipeline],
        "inputs": dict(sorted(inputs.items())),
        "files": files,
        "totals": {
            "files": len(files),
            "bytes": sum(e["bytes"] for e in files),
            "rows": dict(sorted(rows.items())),
        },
        "dataset_sha256": combined_sha256({e["path"]: e["sha256"] for e in files}),
        "sql": dict(sql),
    }
    manifest[_SELF_KEY] = content_sha256(manifest)
    manifest[RUN_KEY] = {
        "generated_at": generated_at,
        "note": f"Not covered by {_SELF_KEY}; everything else is reproducible.",
    }
    return manifest


def render_readme(
    *, scale: str, seed: int | None, files: Sequence[ExportedFile], settings: ExportConfig
) -> str:
    """Render the bundle's README: what each folder holds and how to load the SQL.

    Args:
        scale: Scale name.
        seed: The generator's seed.
        files: The data files of the bundle.
        settings: Export settings.

    Returns:
        Markdown text (no wall-clock time, so it is byte-identical on a rerun).
    """
    rows = {f.table: (f.layer, f.rows) for f in files if f.kind == "csv"}
    table_lines = [f"| `{table}` | {layer} | {count:,} |" for table, (layer, count) in rows.items()]
    scripts = [f"{SQL_DIR}/{name}" for b in SQL_BUNDLES for name in (b.schema_name, b.seed_name)]
    gz = [f.path for f in files if f.kind == "sql.gz"]
    jsonl = ", ".join(f"`{t.value}`" for t in settings.jsonl_tables) or "none"
    lines = [
        f"# YottaFlex export: `{scale}` scale",
        "",
        f"Synthetic cold-chain data (100 % synthetic; no external data), seed `{seed}`, "
        f"written by coldchain {__version__}. `manifest.json` lists every file with its row "
        "count and sha256, and records the commands, seed, config and git commit that "
        "produced it.",
        "",
        "## Layout",
        "",
        "| Path | Content |",
        "|---|---|",
        "| `csv/<layer>/<table>.csv` | one CSV per table (UTF-8, header row, empty field = null) |",
        f"| `json/<layer>/<table>.json` | one JSON array of records per table; JSON Lines "
        f"(`.jsonl`, one record per line) for {jsonl} |",
        f"| `{scripts[0]}`, `{scripts[1]}` | DDL and rows of the raw and compliance tables |",
        f"| `{scripts[2]}`, `{scripts[3]}` | DDL and rows of the ground truth (answer key) |",
        "| `reports/` | the validation summary and the evaluation report of this data |",
        "| `docs/data_dictionary.md` | every table and field |",
        "",
        "Layers: `raw` is the pipeline input (no labels), `compliance` the engine's output, "
        "`ground_truth` the answer key. The answer key is kept in its own folders and its own "
        "SQL files, so loading `seed.sql` never loads the labels.",
    ]
    if gz:
        lines += ["", f"Large seed files also come gzip-compressed: {', '.join(gz)}."]
    lines += [
        "",
        "## Tables",
        "",
        "| Table | Layer | Rows |",
        "|---|---|---:|",
        *table_lines,
        "",
        "## Loading the SQL",
        "",
        "The scripts are portable (DuckDB and SQLite) and must be loaded in this order into a "
        "fresh database: " + ", ".join(f"`{s}`" for s in scripts) + ". The ground-truth pair "
        "is optional. Rows are batched multi-row INSERTs in one transaction, in foreign-key "
        "dependency order.",
        "",
        "```bash",
        "# DuckDB (foreign keys are always enforced)",
        "duckdb yottaflex.duckdb < sql/schema.sql && duckdb yottaflex.duckdb < sql/seed.sql",
        "# SQLite (enable foreign-key enforcement for the session first)",
        "(echo 'PRAGMA foreign_keys = ON;'; cat sql/schema.sql sql/seed.sql) | "
        "sqlite3 yottaflex.sqlite",
        "```",
        "",
    ]
    return "\n".join(lines)
