"""Reproducibility check (``coldchain reproduce``): compare every file of two pipeline runs.

``coldchain reproduce`` runs the whole pipeline three times (run1 and run2 with the same seed,
then a control run with another seed) and describes every file each run wrote
(:func:`coldchain.io.describe_files`). This module is pure: it compares those descriptions and
renders ``reproducibility_check.md`` and ``.json``.

* **run1 vs run2.** Every file must exist in both runs with the same row count and the same
  sha256 of its bytes. The only exceptions are the three run records that hold a wall-clock
  value or a hash of one (:data:`CONTENT_HASHED`): ``export/manifest.json`` is compared on its
  content without the ``run`` block (``generated_at``), ``run_summary.json`` without its
  ``timing`` blocks, and ``provenance/export.json`` without the sha256 it records for the
  manifest (a hash that covers ``generated_at``). Content hashes are recomputed from the file,
  never read from it.
* **Control.** A run with a different seed must change the data: every file of
  :data:`CONTROL_MUST_CHANGE` differs from run1. This proves the comparison above is not
  trivially true (for example because the seed is ignored).

Decisions: DECISIONS.md #116-#118.
"""

from collections.abc import Callable, Mapping
from typing import Any

from coldchain.enums import PipelineStage
from coldchain.export.manifest import MANIFEST_NAME, content_sha256
from coldchain.io import EXPORT_DIR, PROVENANCE_DIR
from coldchain.observability import RUN_SUMMARY_NAME, run_summary_content_sha256
from coldchain.provenance import settings_sha256

REPORT_STEM = "reproducibility_check"
"""File stem of the report in ``<scale>/reproducibility/``."""

REPORT_VERSION = 1
"""Version of the report layout."""

RUN_LABELS: tuple[str, ...] = ("run1", "run2", "control")
"""The three runs, in the order they are executed."""

MANIFEST_PATH = f"{EXPORT_DIR}/{MANIFEST_NAME}"
"""The export manifest, relative to a run's scale folder."""

EXPORT_RECORD_PATH = f"{PROVENANCE_DIR}/{PipelineStage.EXPORT.value}.json"
"""The export stage's provenance record, relative to a run's scale folder."""


def export_record_sha256(record: Mapping[str, Any]) -> str:
    """Hash the export stage's provenance record without the manifest's file digest.

    That digest is the sha256 of ``manifest.json``'s bytes, which include ``generated_at``;
    the manifest itself is compared on its content instead. Every other field, including the
    digest of every other exported file, is hashed.

    Args:
        record: The parsed ``provenance/export.json``.

    Returns:
        The hex sha256 of its sorted-key, compact JSON rendering without that one digest.
    """
    outputs = {
        path: digest
        for path, digest in record.get("outputs", {}).items()
        if not path.endswith(f"/{MANIFEST_PATH}")
    }
    return settings_sha256({**record, "outputs": outputs})


CONTENT_HASHED: dict[str, tuple[Callable[[Mapping[str, Any]], str], str]] = {
    MANIFEST_PATH: (content_sha256, "the `run` block (`generated_at`, the export's clock time)"),
    RUN_SUMMARY_NAME: (
        run_summary_content_sha256,
        "the `timing` blocks (stage durations and peak memory)",
    ),
    EXPORT_RECORD_PATH: (
        export_record_sha256,
        "the sha256 it records for `export/manifest.json` (a hash over `generated_at`)",
    ),
}
"""Files compared on their content: path → (hash function, what it leaves out)."""

CONTROL_MUST_CHANGE: tuple[str, ...] = (
    "raw/shipments.parquet",
    "raw/temperature_readings.parquet",
    "ground_truth/expected_outcomes.parquet",
    "ground_truth/anomalies.parquet",
    "compliance/compliance_decisions.parquet",
    MANIFEST_PATH,
)
"""Files a different seed must change (the generated data, its answer key and the verdicts)."""

_BYTES = "bytes"
_CONTENT = "content"


def run_facts(
    files: Mapping[str, Mapping[str, Any]], documents: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Turn one run's file descriptions into the facts that are compared.

    Args:
        files: Path → ``{"sha256", "bytes", "rows"}`` (:func:`coldchain.io.describe_files`).
        documents: Path → parsed JSON of the files in :data:`CONTENT_HASHED` that exist.

    Returns:
        Path → ``{"sha256", "bytes", "rows", "compared"}``. For a content-hashed file,
        ``sha256`` is its content hash and ``file_sha256`` keeps the hash of its bytes. Export
        files whose rows cannot be counted from the file (the SQL seed scripts) take the row
        count the manifest records for them.
    """
    facts = {path: {**dict(fact), "compared": _BYTES} for path, fact in files.items()}
    for path, payload in documents.items():
        hasher, _ = CONTENT_HASHED[path]
        facts[path] = {
            **facts[path],
            "file_sha256": facts[path]["sha256"],
            "sha256": hasher(payload),
            "compared": _CONTENT,
        }
    manifest = documents.get(MANIFEST_PATH, {})
    for entry in manifest.get("files", []):
        path = f"{EXPORT_DIR}/{entry['path']}"
        if path in facts and facts[path]["rows"] is None and entry.get("rows") is not None:
            facts[path]["rows"] = entry["rows"]
    return facts


def compare_runs(
    first: Mapping[str, Mapping[str, Any]], second: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Compare two runs file by file.

    Args:
        first: run1's facts (:func:`run_facts`).
        second: run2's facts.

    Returns:
        One row per path in either run, sorted by path: ``path``, ``rows_run1``,
        ``rows_run2``, ``sha256_run1``, ``sha256_run2``, ``compared`` and ``match`` (the file
        is in both runs with equal rows and equal sha256).
    """
    rows = []
    for path in sorted(set(first) | set(second)):
        one, two = first.get(path), second.get(path)
        rows.append(
            {
                "path": path,
                "rows_run1": None if one is None else one["rows"],
                "rows_run2": None if two is None else two["rows"],
                "sha256_run1": None if one is None else one["sha256"],
                "sha256_run2": None if two is None else two["sha256"],
                "compared": (one or two or {}).get("compared", _BYTES),
                "match": one is not None
                and two is not None
                and one["sha256"] == two["sha256"]
                and one["rows"] == two["rows"],
            }
        )
    return rows


def control_comparison(
    run: Mapping[str, Mapping[str, Any]], control: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """Check that the control run (another seed) changed the data.

    Args:
        run: run1's facts.
        control: The control run's facts.

    Returns:
        ``files`` (paths in either run), ``changed`` (count), ``unchanged`` (the paths with the
        same sha256 in both), ``must_change`` (each :data:`CONTROL_MUST_CHANGE` path → whether
        it changed) and ``passed`` (all of them changed).
    """
    paths = set(run) | set(control)
    changed = {
        path
        for path in paths
        if (run.get(path) or {}).get("sha256") != (control.get(path) or {}).get("sha256")
    }
    must_change = {path: path in changed and path in run for path in CONTROL_MUST_CHANGE}
    return {
        "files": len(paths),
        "changed": len(changed),
        "unchanged": sorted(paths - changed),
        "must_change": must_change,
        "passed": all(must_change.values()),
    }


def _stage_details(summary: Mapping[str, Any], stage: PipelineStage) -> Mapping[str, Any]:
    """The ``details`` of one stage in a run summary (empty if it did not run)."""
    for entry in summary.get("stages", []):
        if entry["stage"] == stage.value:
            return entry.get("details") or {}
    return {}


def _run_block(summary: Mapping[str, Any]) -> dict[str, Any]:
    """Summarise one run for the report: command, status, dataset hash and timings."""
    return {
        "command": summary["command"],
        "seed": summary["seed"],
        "status": summary["status"],
        "failed_stage": summary["failed_stage"],
        "error": summary["error"],
        "dataset_sha256": _stage_details(summary, PipelineStage.EXPORT).get("dataset_sha256"),
        "timing": {
            "total_duration_s": summary["timing"]["total_duration_s"],
            "peak_rss_mib": summary["timing"]["peak_rss_mib"],
            "stages": {
                stage["stage"]: stage["timing"] for stage in summary["stages"] if stage["timing"]
            },
        },
    }


def build_report(
    *,
    scale: str,
    seed: int,
    control_seed: int,
    verify_sql: bool,
    summaries: Mapping[str, Mapping[str, Any]],
    facts: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Assemble the reproducibility report.

    Args:
        scale: Scale name.
        seed: Seed of run1 and run2.
        control_seed: Seed of the control run.
        verify_sql: Whether each run's export proved the SQL load.
        summaries: Run label → its run summary (only the runs that were started).
        facts: Run label → its file facts (only the runs that completed).

    Returns:
        The JSON-ready report. ``verdict`` is PASS when all three runs completed, every file of
        run1 matches run2, and the control run changed every :data:`CONTROL_MUST_CHANGE` file.
        ``failures`` lists every reason otherwise.
    """
    failures = []
    for label in RUN_LABELS:
        summary = summaries.get(label)
        if summary is None:
            failures.append(f"{label} was not run (an earlier run failed)")
        elif summary["exit_code"] != 0:
            failures.append(
                f"{label} failed at stage {summary['failed_stage']}: {summary['error']}"
            )
    files = compare_runs(facts["run1"], facts["run2"]) if {"run1", "run2"} <= set(facts) else []
    mismatched = [row["path"] for row in files if not row["match"]]
    if mismatched:
        shown = ", ".join(mismatched[:5]) + (" …" if len(mismatched) > 5 else "")
        failures.append(f"{len(mismatched)} file(s) differ between run1 and run2: {shown}")
    control: dict[str, Any] = {}
    if {"run1", "control"} <= set(facts):
        control = control_comparison(facts["run1"], facts["control"])
        if not control["passed"]:
            same = [path for path, moved in control["must_change"].items() if not moved]
            failures.append(f"the control seed did not change: {', '.join(same)}")
    return {
        "reproducibility_version": REPORT_VERSION,
        "scale": scale,
        "seed": seed,
        "control_seed": control_seed,
        "verify_sql": verify_sql,
        "verdict": "FAIL" if failures else "PASS",
        "failures": failures,
        "comparison": {
            "rows": "records per table file (Parquet metadata, CSV data rows, JSON array "
            "length, JSON Lines); for SQL seed scripts the rows the manifest records",
            "sha256": "of the file bytes, except the content-hashed run records",
            "content_hashed": [
                {"path": path, "excluded": excluded}
                for path, (_, excluded) in CONTENT_HASHED.items()
            ],
        },
        "totals": {
            "files": len(files),
            "matching": len(files) - len(mismatched),
            "mismatching": len(mismatched),
            "bytes_run1": sum(fact["bytes"] for fact in facts.get("run1", {}).values()),
        },
        "files": files,
        "control": control,
        "runs": {label: _run_block(summary) for label, summary in summaries.items()},
    }


def _cell(value: Any) -> str:
    """A table cell: ``—`` for ``None``, thousands separators for integers."""
    if value is None:
        return "—"
    return f"{value:,}" if isinstance(value, int) else str(value)


def _short(digest: str | None) -> str:
    """The first 12 hex digits of a hash, as code."""
    return "—" if not digest else f"`{digest[:12]}`"


def report_markdown(payload: Mapping[str, Any]) -> str:
    """Render the reproducibility report as Markdown.

    Args:
        payload: The report (:func:`build_report`).

    Returns:
        Markdown text: verdict, method, runs and timings, the per-file table, the control run.
    """
    totals, control, runs = payload["totals"], payload["control"], payload["runs"]
    lines = [f"# Reproducibility check: `{payload['scale']}` scale", ""]
    if payload["verdict"] == "PASS":
        lines.append(
            f"**Verdict: PASS.** run1 and run2 (seed `{payload['seed']}`) wrote "
            f"{totals['files']:,} files, and every file has the same row count and sha256 "
            f"({totals['matching']:,} of {totals['files']:,} match). The control run (seed "
            f"`{payload['control_seed']}`) changed {control['changed']:,} of "
            f"{control['files']:,} files, so the seed really drives the data."
        )
    else:
        lines.append("**Verdict: FAIL.**")
        lines += ["", *(f"- {failure}" for failure in payload["failures"])]
    excluded = "; ".join(
        f"`{item['path']}` without {item['excluded']}"
        for item in payload["comparison"]["content_hashed"]
    )
    sql = (
        "on (`--verify-sql`)"
        if payload["verify_sql"]
        else "off (`--no-verify-sql`): byte-identical SQL scripts load identically, and "
        "`run-all` / `export` prove the load by default"
    )
    lines += [
        "",
        "## Method",
        "",
        "- Each run is the whole pipeline (`coldchain run-all`: generate → inject → check → "
        "validate → validate baseline → evaluate → export), written into an empty folder at "
        "the same path, then moved aside. The same path keeps even the provenance records "
        "(which name `--output-dir`) comparable byte for byte.",
        f"- Rows: {payload['comparison']['rows']}. `—` marks a file that is not a table.",
        f"- sha256: {payload['comparison']['sha256']}. Content-hashed (†): {excluded}. These "
        "hold the only wall-clock value the pipeline writes, or a hash of it.",
        f"- SQL load proof inside the runs: {sql}.",
        "",
        "## Runs",
        "",
        "| Run | Seed | Status | dataset_sha256 | Duration (s) | Peak RSS (MiB) |",
        "|---|---:|---|---|---:|---:|",
    ]
    for label, run in runs.items():
        timing = run["timing"]
        lines.append(
            f"| {label} | {run['seed']} | {run['status']} | {_short(run['dataset_sha256'])} | "
            f"{timing['total_duration_s']:.2f} | {_cell(timing['peak_rss_mib'])} |"
        )
    stages = list(next(iter(runs.values()))["timing"]["stages"]) if runs else []
    if stages:
        header = " | ".join(f"{label} s / MiB" for label in runs)
        lines += [
            "",
            "Stage timings (duration in seconds / peak RSS in MiB during the stage):",
            "",
            f"| Stage | {header} |",
            "|---|" + "---:|" * len(runs),
        ]
        for stage in stages:
            cells = []
            for run in runs.values():
                timing = run["timing"]["stages"].get(stage)
                cells.append(
                    "—"
                    if not timing
                    else f"{timing['duration_s']:.2f} / {_cell(timing['peak_rss_mib'])}"
                )
            lines.append(f"| {stage} | " + " | ".join(cells) + " |")
    if payload["files"]:
        lines += [
            "",
            "## Files: run1 vs run2",
            "",
            "| File | Rows run1 | Rows run2 | sha256 match |",
            "|---|---:|---:|---|",
        ]
        for row in payload["files"]:
            mark = "yes" if row["match"] else "**NO**"
            dagger = " †" if row["compared"] == _CONTENT else ""
            lines.append(
                f"| `{row['path']}` | {_cell(row['rows_run1'])} | {_cell(row['rows_run2'])} | "
                f"{mark}{dagger} |"
            )
    if control:
        lines += [
            "",
            f"## Control run (seed `{payload['control_seed']}`)",
            "",
            f"{control['changed']:,} of {control['files']:,} files differ from run1.",
            "",
            "| Must change | Changed |",
            "|---|---|",
            *(
                f"| `{path}` | {'yes' if moved else '**NO**'} |"
                for path, moved in control["must_change"].items()
            ),
            "",
            "Files that are the same under both seeds (they do not depend on the seed):",
            "",
            *(f"- `{path}`" for path in control["unchanged"]),
        ]
    lines.append("")
    return "\n".join(lines)
