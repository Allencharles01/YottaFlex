"""Pipeline stages: what each ``coldchain`` command does, callable without the CLI.

Each ``run_<stage>`` function reads its inputs, runs the pure library code, writes its outputs
and its provenance record (``<output-dir>/<scale>/provenance/<stage>.json``), and returns a
:class:`StageResult`: the rows it wrote, stage-specific details (verdicts, totals) and, when its
verdict is FAIL, the failure message. Errors that stop a stage raise
:class:`~coldchain.errors.ColdchainError`.

* The CLI wraps one function per command (:func:`execute_stage` adds timing and peak memory).
* :func:`run_pipeline` (``coldchain run-all``) runs every stage in order, stops at the first
  failure, and writes ``run_summary.json``.
* :func:`reproduce` (``coldchain reproduce``) runs the pipeline twice with one seed and once
  with a control seed, and compares every file (:mod:`coldchain.export.reproducibility`).

Every function receives the exact command line of the stage it runs and records it in the
provenance record. ``run-all`` passes the command line that the single-stage command would
have recorded, so running the stages one by one or with ``run-all`` writes the same bytes.

Together with :mod:`coldchain.cli` and :mod:`coldchain.io`, this module is the I/O edge of the
package: it reads and writes files, while everything it calls is pure.
"""

import functools
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from coldchain import provenance
from coldchain.compliance import ENGINE_INPUTS, check_tables
from coldchain.config import PROJECT_ROOT, AppConfig, load_config, load_reference
from coldchain.enums import (
    DataSource,
    FileFormat,
    Layer,
    PipelineStage,
    ReportKind,
    Scale,
    StageStatus,
    TableName,
)
from coldchain.errors import (
    ColdchainError,
    EvaluationError,
    ExportError,
    ReproducibilityError,
)
from coldchain.evaluation import EVALUATION_INPUTS, REPORT_STEM, evaluate_tables
from coldchain.evaluation.report import report_markdown
from coldchain.export import EXPORT_TABLES, reproducibility, write_bundle
from coldchain.export.exporters import MIB, sql_load_order
from coldchain.export.manifest import MANIFEST_NAME, build_manifest, file_entry, pipeline_entry
from coldchain.export.sql_load import verify_loads
from coldchain.generate import generate_tables
from coldchain.inject import INJECT_INPUTS, inject_tables
from coldchain.io import (
    DEFAULT_FORMATS,
    baseline_dir,
    clear_injection,
    coerce_to_schema,
    count_csv_rows,
    describe_files,
    ensure_baseline,
    export_root,
    file_sha256,
    file_size,
    layer_dir,
    provenance_path,
    read_json,
    read_report,
    read_table,
    read_tables,
    remove_directory,
    replace_directory,
    report_dir,
    report_paths,
    table_path,
    write_json,
    write_layers,
    write_report,
    write_table,
)
from coldchain.observability import (
    RUN_SUMMARY_NAME,
    Measurement,
    build_run_summary,
    measure,
    stage_entry,
)
from coldchain.schemas import SCHEMAS, TABLE_LAYERS
from coldchain.validation import COMPLIANCE_INPUTS, RAW_INPUTS, validate_tables
from coldchain.validation.summary import SUMMARY_STEM, summary_markdown, summary_payload

logger = logging.getLogger(__name__)

PARQUET = FileFormat.PARQUET

CHAIN_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.GENERATE,
    PipelineStage.INJECT,
    PipelineStage.CHECK,
    PipelineStage.VALIDATE,
    PipelineStage.EVALUATE,
)
"""Stages whose provenance ``export`` checks; all but ``inject`` are required."""

RUN_ALL_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.GENERATE,
    PipelineStage.INJECT,
    PipelineStage.CHECK,
    PipelineStage.VALIDATE,
    PipelineStage.VALIDATE_BASELINE,
    PipelineStage.EVALUATE,
    PipelineStage.EXPORT,
)
"""Stages ``run-all`` runs, in order (DECISIONS.md #114)."""

EXPORT_DOCS: dict[str, Path] = {
    "docs/data_dictionary.md": PROJECT_ROOT / "docs" / "data_dictionary.md",
}
"""Project documents copied into the export bundle (bundle path → source)."""

RUNS_DIR = "runs"
"""Sub-folder of ``<scale>/reproducibility/`` holding the reproducibility runs."""

CURRENT_RUN = "current"
"""The one path every reproducibility run is written to before it is moved aside."""


@dataclass(frozen=True)
class RunOptions:
    """Options shared by every stage.

    Attributes:
        scale: Scale preset.
        config_path: Pipeline config YAML (``--config``).
        output_dir: Output root (``--output-dir``); files go to ``<output_dir>/<scale>/``.
    """

    scale: Scale
    config_path: Path
    output_dir: Path

    @property
    def scale_dir(self) -> Path:
        """The folder of this scale, ``<output_dir>/<scale>``."""
        return Path(self.output_dir) / self.scale.value


@dataclass
class StageResult:
    """What a stage did.

    Attributes:
        rows: ``<layer>/<table>`` → rows written.
        details: Stage-specific results (seed, verdict, check totals, dataset hash, ...).
        failure: Why the stage's verdict is FAIL (its outputs are written), or ``None``.
    """

    rows: dict[str, int] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)
    failure: str | None = None


@dataclass(frozen=True)
class StageRun:
    """One executed stage: its result or error, and its measurement.

    Attributes:
        stage: The stage.
        command: Its command line.
        result: What it did (``None`` when it raised an error).
        error: The error message, when it raised one.
        timing: Duration and peak memory.
    """

    stage: PipelineStage
    command: str
    result: StageResult | None
    error: str | None
    timing: Measurement

    @property
    def message(self) -> str | None:
        """The failure message (error or FAIL verdict), or ``None`` when the stage passed."""
        if self.error is not None:
            return self.error
        return None if self.result is None else self.result.failure

    @property
    def passed(self) -> bool:
        """Whether the stage completed with a passing verdict."""
        return self.result is not None and self.message is None

    def entry(self) -> dict[str, Any]:
        """Return the stage's entry for ``run_summary.json``."""
        result = self.result or StageResult()
        return stage_entry(
            self.stage.value,
            status=StageStatus.PASS if self.passed else StageStatus.FAIL,
            command=self.command,
            rows=result.rows,
            details=result.details,
            error=self.message,
            timing=self.timing,
        )


def execute_stage(
    stage: PipelineStage, command: str, action: Callable[[], StageResult]
) -> StageRun:
    """Run one stage, measuring its duration and peak memory and logging them.

    Args:
        stage: The stage (for the log line).
        command: Its command line.
        action: Runs the stage.

    Returns:
        The executed stage. A :class:`ColdchainError` becomes its ``error``; any other
        exception (a bug) propagates.
    """
    result, error = None, None
    with measure() as timing:
        try:
            result = action()
        except ColdchainError as exc:
            error = str(exc)
    run = StageRun(stage, command, result, error, timing)
    logger.info(
        "Stage %s %s in %s", stage.value, "done" if run.passed else "FAILED", timing.describe()
    )
    return run


# --------------------------------------------------------------------------------------------
# Helpers shared by the stages
# --------------------------------------------------------------------------------------------


def _relative(path: Path, output_root: Path) -> str:
    """Path of a pipeline file relative to the output root (the key used in provenance)."""
    return Path(path).relative_to(output_root).as_posix()


def _digests(paths: Iterable[Path], output_root: Path) -> dict[str, str]:
    """Sha256 of every existing file, keyed by its path relative to the output root."""
    return {
        _relative(path, output_root): file_sha256(path) for path in paths if Path(path).is_file()
    }


def _table_files(
    output_root: Path, scale: str, tables: Iterable[TableName], formats: Iterable[FileFormat]
) -> list[Path]:
    """The files of some tables in their layer folders, one per format."""
    formats = tuple(formats)
    return [
        table_path(layer_dir(output_root, scale, TABLE_LAYERS[name]), name, fmt)
        for name in tables
        for fmt in formats
    ]


def _row_counts(tables: Mapping[TableName, pd.DataFrame]) -> dict[str, int]:
    """``<layer>/<table>`` → row count of every table written by a stage."""
    return {f"{TABLE_LAYERS[name].value}/{name.value}": len(df) for name, df in tables.items()}


def _record(
    stage: PipelineStage,
    options: RunOptions,
    *,
    command: str,
    config: AppConfig,
    seed: int | None,
    inputs: Iterable[Path],
    outputs: Iterable[Path],
) -> Path:
    """Write a stage's provenance record (see :mod:`coldchain.provenance`)."""
    record = provenance.stage_record(
        stage,
        command=command,
        scale=options.scale.value,
        seed=seed,
        config=provenance.config_fingerprint(config, options.config_path, file_sha256),
        git=provenance.git_state(),
        inputs=_digests(inputs, options.output_dir),
        outputs=_digests(outputs, options.output_dir),
    )
    path = write_json(provenance_path(options.output_dir, options.scale.value, stage), record)
    logger.debug("Wrote provenance record %s", path)
    return path


def _load_config(options: RunOptions, seed: int | None = None) -> AppConfig:
    """Load the config, with ``seed`` overriding the configured seed when given."""
    config = load_config(options.config_path)
    return config if seed is None else config.model_copy(update={"seed": seed})


def validation_dir(output_root: Path, scale: str, source: DataSource) -> Path:
    """Folder of a validation summary: ``validation/`` (raw) or ``validation/baseline/``."""
    folder = report_dir(output_root, scale, ReportKind.VALIDATION)
    return folder if source is DataSource.RAW else folder / source.value


# --------------------------------------------------------------------------------------------
# Stages
# --------------------------------------------------------------------------------------------


def run_generate(options: RunOptions, *, seed: int | None, command: str) -> StageResult:
    """Generate master data, events and natural scenarios, plus the ground truth.

    Raw tables go to ``raw/``, the answer-key tables to ``ground_truth/``. A previous
    injection's baseline snapshot and anomaly list are removed, since they belong to the data
    being replaced.

    Args:
        options: Shared options.
        seed: Overrides the config's seed when given.
        command: The command line to record.

    Returns:
        Rows per table and the seed used.
    """
    scale, output_dir = options.scale, options.output_dir
    config = _load_config(options, seed)
    logger.info(
        "Generating scale=%s seed=%d (config %s)", scale.value, config.seed, options.config_path
    )
    tables = generate_tables(config, load_reference(), scale)
    for path in clear_injection(output_dir, scale.value):
        logger.info("Removed stale injection output %s", path)
    written = write_layers(tables, output_dir, scale.value)
    for name, frame in tables.items():
        logger.info("  %-13s %-24s %8d rows", TABLE_LAYERS[name].value, name.value, len(frame))
    for layer, target in written.items():
        count = sum(TABLE_LAYERS[name] is layer for name in tables)
        logger.info("Wrote %d %s tables (Parquet + CSV) to %s", count, layer.value, target)
    _record(
        PipelineStage.GENERATE,
        options,
        command=command,
        config=config,
        seed=config.seed,
        inputs=(),
        outputs=_table_files(output_dir, scale.value, tables, DEFAULT_FORMATS),
    )
    return StageResult(rows=_row_counts(tables), details={"seed": config.seed})


def run_inject(options: RunOptions, *, seed: int | None, command: str) -> StageResult:
    """Inject the five labelled edge cases into the generated data.

    The first run saves the clean generator output to ``baseline/``. Every run starts from that
    snapshot, so re-running gives the same result. It rewrites ``raw/`` (only the target
    shipments' records change), updates ``ground_truth/expected_outcomes`` and writes
    ``ground_truth/anomalies`` (Parquet, CSV and JSON).

    Args:
        options: Shared options.
        seed: Overrides the config's seed when given.
        command: The command line to record.

    Returns:
        Rows per table, the seed and the number of anomalies.
    """
    scale, output_dir = options.scale, options.output_dir
    config = _load_config(options, seed)
    if ensure_baseline(output_dir, scale.value):
        logger.info("Snapshot of the clean data taken before the first injection")
    logger.info(
        "Injecting scale=%s seed=%d (config %s)", scale.value, config.seed, options.config_path
    )
    baseline = {
        name: read_table(name, baseline_dir(output_dir, scale.value, TABLE_LAYERS[name]))
        for name in INJECT_INPUTS
    }
    tables = inject_tables(config, baseline)
    anomalies = tables.pop(TableName.ANOMALIES)
    written = write_layers(tables, output_dir, scale.value)
    write_table(
        TableName.ANOMALIES,
        anomalies,
        written[Layer.GROUND_TRUTH],
        (*DEFAULT_FORMATS, FileFormat.JSON),
    )
    everything = {**tables, TableName.ANOMALIES: anomalies}
    for name, frame in everything.items():
        logger.info("  %-13s %-24s %8d rows", TABLE_LAYERS[name].value, name.value, len(frame))
    logger.info(
        "Wrote %d anomaly rows (%d anomalies) to %s",
        len(anomalies),
        anomalies["anomaly_id"].nunique(),
        written[Layer.GROUND_TRUTH],
    )
    _record(
        PipelineStage.INJECT,
        options,
        command=command,
        config=config,
        seed=config.seed,
        inputs=[
            table_path(baseline_dir(output_dir, scale.value, TABLE_LAYERS[n]), n, PARQUET)
            for n in INJECT_INPUTS
        ],
        outputs=[
            *_table_files(output_dir, scale.value, tables, DEFAULT_FORMATS),
            *_table_files(output_dir, scale.value, [TableName.ANOMALIES], FileFormat),
        ],
    )
    return StageResult(
        rows=_row_counts(everything),
        details={"seed": config.seed, "anomalies": int(anomalies["anomaly_id"].nunique())},
    )


def run_check(options: RunOptions, *, command: str) -> StageResult:
    """Run the compliance engine on the raw tables.

    Reads only ``raw/`` (never the answer key) and writes the excursion reports, their reading
    links, the decisions and the appended status events to ``compliance/``. Raw files are never
    modified.

    Args:
        options: Shared options.
        command: The command line to record.

    Returns:
        Rows per table and the number of decisions per status.
    """
    scale, output_dir = options.scale, options.output_dir
    config = load_config(options.config_path)
    raw_dir = layer_dir(output_dir, scale.value, Layer.RAW)
    logger.info("Checking scale=%s from %s", scale.value, raw_dir)
    tables = check_tables(config, read_tables(ENGINE_INPUTS, raw_dir))
    written = write_layers(tables, output_dir, scale.value)
    for name, frame in tables.items():
        logger.info("  %-13s %-26s %8d rows", TABLE_LAYERS[name].value, name.value, len(frame))
    logger.info(
        "Wrote %d compliance tables (Parquet + CSV) to %s", len(tables), written[Layer.COMPLIANCE]
    )
    _record(
        PipelineStage.CHECK,
        options,
        command=command,
        config=config,
        seed=None,
        inputs=_table_files(output_dir, scale.value, ENGINE_INPUTS, [FileFormat.PARQUET]),
        outputs=_table_files(output_dir, scale.value, tables, DEFAULT_FORMATS),
    )
    statuses = tables[TableName.COMPLIANCE_DECISIONS]["status"].value_counts()
    return StageResult(
        rows=_row_counts(tables),
        details={"statuses": {str(k): int(v) for k, v in sorted(statuses.items())}},
    )


def run_validate(options: RunOptions, *, source: DataSource, command: str) -> StageResult:
    """Validate data integrity: keys, enums, dates, lifecycle, duplicates, reconciliation.

    Judges the raw tables (and the engine output in ``compliance/`` when present) on their own,
    without the answer key, and writes ``validation_summary.md`` and ``.json`` to
    ``validation/`` (``validation/baseline/`` for the baseline source). Its verdict is FAIL if
    any INTEGRITY check fails; BUSINESS_RULE findings are reported for ``evaluate`` to explain.

    Args:
        options: Shared options.
        source: ``raw`` (with the engine output) or the clean ``baseline`` snapshot.
        command: The command line to record.

    Returns:
        The check totals and verdict; ``failure`` names the failed integrity checks.
    """
    scale, output_dir = options.scale, options.output_dir
    config = load_config(options.config_path)
    if source is DataSource.RAW:
        raw_dir = layer_dir(output_dir, scale.value, Layer.RAW)
    else:
        raw_dir = baseline_dir(output_dir, scale.value, Layer.RAW)
    compliance_dir = layer_dir(output_dir, scale.value, Layer.COMPLIANCE)
    directories = {name: raw_dir for name in RAW_INPUTS}
    present = all(
        table_path(compliance_dir, name, FileFormat.PARQUET).is_file() for name in COMPLIANCE_INPUTS
    )
    if source is DataSource.RAW and present:
        directories.update({name: compliance_dir for name in COMPLIANCE_INPUTS})
    elif source is DataSource.RAW:
        logger.warning("No complete engine output in %s: its checks are skipped", compliance_dir)
    logger.info("Validating scale=%s source=%s from %s", scale.value, source.value, raw_dir)
    tables = {
        name: read_table(name, directory, check_schema=False)
        for name, directory in directories.items()
    }
    inputs = {
        table_path(directory, name, FileFormat.PARQUET)
        .relative_to(output_dir)
        .as_posix(): file_sha256(table_path(directory, name, FileFormat.PARQUET))
        for name, directory in directories.items()
    }
    result = validate_tables(config, load_reference(), tables)
    payload = summary_payload(result, scale=scale.value, source=source.value, inputs=inputs)
    target = validation_dir(output_dir, scale.value, source)
    md_path, json_path = write_report(target, SUMMARY_STEM, summary_markdown(payload), payload)
    totals = payload["totals"]
    logger.info(
        "Wrote %s and %s: %d checks, %d PASS, %d FAIL, %d ERROR, %d SKIPPED; "
        "%d integrity finding(s), %d business-rule finding(s)",
        md_path,
        json_path.name,
        totals["checks"],
        totals["pass"],
        totals["fail"],
        totals["error"],
        totals["skipped"],
        totals["integrity_findings"],
        totals["business_rule_findings"],
    )
    _record(
        PipelineStage.VALIDATE if source is DataSource.RAW else PipelineStage.VALIDATE_BASELINE,
        options,
        command=command,
        config=config,
        seed=None,
        inputs=[output_dir / relative for relative in inputs],
        outputs=[md_path, json_path],
    )
    failure = None
    if not result.passed:
        failed = ", ".join(r.check.check_id for r in result.integrity_failures)
        failure = f"integrity checks failed: {failed}"
    details = {"verdict": "PASS" if result.passed else "FAIL", "source": source.value, **totals}
    return StageResult(details=details, failure=failure)


def _require_fresh(validation: Mapping[str, Any], output_root: Path) -> None:
    """Fail unless every input file of the validation summary is unchanged since validation."""
    stale = [
        relative
        for relative, digest in validation.get("inputs", {}).items()
        if not (output_root / relative).is_file() or file_sha256(output_root / relative) != digest
    ]
    if stale or not validation.get("inputs"):
        raise EvaluationError(
            f"The validation summary is stale (changed inputs: {stale[:3]}); "
            "re-run `coldchain validate` first"
        )


def run_evaluate(options: RunOptions, *, command: str) -> StageResult:
    """Evaluate the engine against the ground truth and explain every validation finding.

    Needs ``check`` and ``validate`` to have run on the current data. Writes
    ``evaluation_report.md`` and ``.json`` to ``evaluation/``. Its verdict is FAIL on any
    unexplained validation finding, or (at a strict scale) on any metric below 100 %.

    Args:
        options: Shared options.
        command: The command line to record.

    Returns:
        The verdict and headline numbers; ``failure`` lists every reason for a FAIL verdict.
    """
    scale, output_dir = options.scale, options.output_dir
    config = load_config(options.config_path)
    summary_dir = validation_dir(output_dir, scale.value, DataSource.RAW)
    validation = read_report(summary_dir, SUMMARY_STEM)
    _require_fresh(validation, output_dir)
    read_paths = [report_paths(summary_dir, SUMMARY_STEM)[1]]
    tables = {}
    for name in EVALUATION_INPUTS:
        directory = layer_dir(output_dir, scale.value, TABLE_LAYERS[name])
        if (
            name is TableName.ANOMALIES
            and not table_path(directory, name, FileFormat.PARQUET).is_file()
        ):
            logger.warning("No %s in %s: nothing was injected", name.value, directory)
            empty = pd.DataFrame({column: [] for column in SCHEMAS[name].columns})
            tables[name] = coerce_to_schema(name, empty)
        else:
            tables[name] = read_table(name, directory)
            read_paths.append(table_path(directory, name, FileFormat.PARQUET))
    logger.info("Evaluating scale=%s", scale.value)
    result = evaluate_tables(config, scale, tables, validation)
    target = report_dir(output_dir, scale.value, ReportKind.EVALUATION)
    md_path, json_path = write_report(
        target, REPORT_STEM, report_markdown(result.payload), result.payload
    )
    payload = result.payload
    logger.info(
        "Wrote %s and %s: verdict %s; %d of %d shipments fully correct; unexplained findings: %d",
        md_path,
        json_path.name,
        payload["verdict"],
        payload["fully_correct_shipments"],
        payload["shipments"],
        payload["validation"]["unexplained"],
    )
    _record(
        PipelineStage.EVALUATE,
        options,
        command=command,
        config=config,
        seed=None,
        inputs=read_paths,
        outputs=[md_path, json_path],
    )
    details = {
        "verdict": payload["verdict"],
        "shipments": payload["shipments"],
        "fully_correct_shipments": payload["fully_correct_shipments"],
        "unexplained_findings": payload["validation"]["unexplained"],
    }
    failure = None
    if not result.passed:
        failure = "evaluation verdict FAIL: " + "; ".join(result.failures)
    return StageResult(details=details, failure=failure)


def _chain_records(output_root: Path, scale: str) -> dict[PipelineStage, dict]:
    """Read the provenance records of :data:`CHAIN_STAGES` (``inject`` may be absent)."""
    records = {}
    for stage in CHAIN_STAGES:
        path = provenance_path(output_root, scale, stage)
        if path.is_file():
            records[stage] = read_json(path)
        elif stage is not PipelineStage.INJECT:
            raise ExportError(
                f"No provenance record {path}: run `coldchain {stage.value} --scale {scale}` "
                "first (export needs generate → [inject] → check → validate → evaluate)"
            )
    return records


def _export_tables(
    output_root: Path, scale: str, *, injected: bool
) -> tuple[dict[TableName, pd.DataFrame], list[Path]]:
    """Read every exported table from its Parquet file (an empty ``anomalies`` if not injected).

    Returns:
        The tables and the paths that were read.
    """
    tables, read_paths = {}, []
    for name in EXPORT_TABLES:
        path = table_path(layer_dir(output_root, scale, TABLE_LAYERS[name]), name, PARQUET)
        if name is TableName.ANOMALIES and not injected and not path.is_file():
            logger.warning("No %s: nothing was injected, exporting it empty", name.value)
            empty = pd.DataFrame({column: [] for column in SCHEMAS[name].columns})
            tables[name] = coerce_to_schema(name, empty)
            continue
        tables[name] = read_table(name, path.parent, check_schema=False)
        read_paths.append(path)
    return tables, read_paths


def run_export(options: RunOptions, *, verify_sql: bool, command: str) -> StageResult:
    """Export every table as CSV, JSON and SQL, with a manifest.

    Needs the whole pipeline (generate → [inject] → check → validate → evaluate, with a PASS
    verdict) to have run on the current files: each stage's provenance record must still match
    the files on disk, and every seeded stage must have used the same seed. Writes ``export/``
    (replaced as a whole only when everything succeeded).

    Args:
        options: Shared options.
        verify_sql: Load the SQL into fresh DuckDB and SQLite databases and compare the counts.
        command: The command line to record (also the manifest's ``command``).

    Returns:
        File totals, the dataset and content hashes, and the SQL proof status.
    """
    scale, output_dir = options.scale, options.output_dir
    config = load_config(options.config_path)
    records = _chain_records(output_dir, scale.value)
    sha = functools.cache(
        lambda relative: (
            file_sha256(output_dir / relative) if (output_dir / relative).is_file() else None
        )
    )
    tables, read_paths = _export_tables(
        output_dir, scale.value, injected=PipelineStage.INJECT in records
    )
    summary_md, summary_json = report_paths(
        validation_dir(output_dir, scale.value, DataSource.RAW), SUMMARY_STEM
    )
    report_md, report_json = report_paths(
        report_dir(output_dir, scale.value, ReportKind.EVALUATION), REPORT_STEM
    )
    read_paths += [summary_md, summary_json, report_md, report_json]
    required = [_relative(path, output_dir) for path in read_paths]
    config_fp = provenance.config_fingerprint(config, options.config_path, file_sha256)
    provenance.verify_chain(
        records, sha, scale=scale.value, config_sha256=config_fp["sha256"], required=required
    )
    verdict = read_json(report_json).get("verdict")
    if verdict != "PASS":
        raise ExportError(f"The evaluation verdict is {verdict!r}, not PASS: nothing exported")

    target = export_root(output_dir, scale.value)
    staging = target.with_name(f"{target.name}.partial")
    remove_directory(staging)
    seed = records[PipelineStage.GENERATE].get("seed")
    attachments = {
        f"reports/{path.name}": path for path in (summary_md, summary_json, report_md, report_json)
    }
    logger.info("Exporting scale=%s (seed %s) to %s", scale.value, seed, target)
    files = write_bundle(
        tables,
        staging,
        config.export,
        scale=scale.value,
        seed=seed,
        attachments={**attachments, **EXPORT_DOCS},
    )
    scripts = sql_load_order(files)
    proof = None
    if verify_sql:
        expected = {f.table: count_csv_rows(staging / f.path) for f in files if f.kind == "csv"}
        proof = verify_loads([staging / path for path in scripts], expected)
    entries = [
        file_entry(f, file_sha256(staging / f.path), file_size(staging / f.path)) for f in files
    ]
    manifest = build_manifest(
        scale=scale.value,
        seed=seed,
        command=command,
        git=provenance.git_state().as_dict(),
        config=config_fp,
        export_settings=provenance.export_settings(config),
        pipeline=[pipeline_entry(records[s]) for s in CHAIN_STAGES if s in records],
        inputs={relative: sha(relative) for relative in required},
        entries=entries,
        sql={
            "batch_rows": config.export.sql_batch_rows,
            "gzip_threshold_mb": config.export.sql_gzip_threshold_mb,
            "load_order": scripts,
            "verification": proof,
        },
        generated_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    write_json(staging / MANIFEST_NAME, manifest)
    replace_directory(staging, target)
    _record(
        PipelineStage.EXPORT,
        options,
        command=command,
        config=config,
        seed=None,
        inputs=read_paths,
        outputs=[target / f.path for f in files] + [target / MANIFEST_NAME],
    )
    for entry in entries:
        if entry["kind"] in ("sql", "sql.gz") and entry["rows"] is not None:
            logger.info(
                "  %-32s %10d rows %9.1f MiB", entry["path"], entry["rows"], entry["bytes"] / MIB
            )
    sql_status = {k: v["status"] for k, v in proof.items()} if proof else None
    logger.info(
        "Wrote %d files (%.1f MiB) and %s to %s; SQL load check: %s; dataset_sha256 %s",
        manifest["totals"]["files"],
        manifest["totals"]["bytes"] / MIB,
        MANIFEST_NAME,
        target,
        ", ".join(f"{k} {v}" for k, v in sql_status.items()) if sql_status else "skipped",
        manifest["dataset_sha256"],
    )
    return StageResult(
        details={
            "files": manifest["totals"]["files"],
            "bytes": manifest["totals"]["bytes"],
            "dataset_sha256": manifest["dataset_sha256"],
            "content_sha256": manifest["content_sha256"],
            "sql_verification": sql_status,
        }
    )


# --------------------------------------------------------------------------------------------
# run-all
# --------------------------------------------------------------------------------------------


def run_summary_path(output_root: Path, scale: str) -> Path:
    """Return the run summary of a scale, ``<output_root>/<scale>/run_summary.json``."""
    return Path(output_root) / scale / RUN_SUMMARY_NAME


def run_pipeline(
    options: RunOptions,
    *,
    seed: int | None,
    verify_sql: bool,
    commands: Mapping[PipelineStage, str],
    command: str,
) -> dict[str, Any]:
    """Run every stage of :data:`RUN_ALL_STAGES` in order and write ``run_summary.json``.

    The run stops at the first stage that raises an error or ends with a FAIL verdict; the
    stages after it are recorded as NOT_RUN. The summary is written in every case.

    Args:
        options: Shared options.
        seed: Overrides the config's seed for ``generate`` and ``inject`` when given.
        verify_sql: Passed to ``export``.
        commands: Stage → the command line its single-stage command would record.
        command: The ``run-all`` command line.

    Returns:
        The run summary (``exit_code`` 0 only when every stage passed).
    """
    actions: dict[PipelineStage, Callable[[], StageResult]] = {
        PipelineStage.GENERATE: lambda: run_generate(
            options, seed=seed, command=commands[PipelineStage.GENERATE]
        ),
        PipelineStage.INJECT: lambda: run_inject(
            options, seed=seed, command=commands[PipelineStage.INJECT]
        ),
        PipelineStage.CHECK: lambda: run_check(options, command=commands[PipelineStage.CHECK]),
        PipelineStage.VALIDATE: lambda: run_validate(
            options, source=DataSource.RAW, command=commands[PipelineStage.VALIDATE]
        ),
        PipelineStage.VALIDATE_BASELINE: lambda: run_validate(
            options,
            source=DataSource.BASELINE,
            command=commands[PipelineStage.VALIDATE_BASELINE],
        ),
        PipelineStage.EVALUATE: lambda: run_evaluate(
            options, command=commands[PipelineStage.EVALUATE]
        ),
        PipelineStage.EXPORT: lambda: run_export(
            options, verify_sql=verify_sql, command=commands[PipelineStage.EXPORT]
        ),
    }
    logger.info("run-all scale=%s: %s", options.scale.value, " → ".join(RUN_ALL_STAGES))
    started = time.perf_counter()
    entries: list[dict[str, Any]] = []
    used_seed = seed
    failed: StageRun | None = None
    for stage in RUN_ALL_STAGES:
        if failed is not None:
            entries.append(
                stage_entry(stage.value, status=StageStatus.NOT_RUN, command=commands[stage])
            )
            continue
        run = execute_stage(stage, commands[stage], actions[stage])
        entries.append(run.entry())
        if run.result is not None and "seed" in run.result.details:
            used_seed = run.result.details["seed"]
        if not run.passed:
            failed = run
            logger.error("run-all stopped at stage %s: %s", stage.value, run.message)
    summary = build_run_summary(
        command=command,
        scale=options.scale.value,
        seed=used_seed,
        git=provenance.git_state().as_dict(),
        stages=entries,
        total_duration_s=time.perf_counter() - started,
    )
    path = write_json(run_summary_path(options.output_dir, options.scale.value), summary)
    _log_summary(summary, path)
    return summary


def _log_summary(summary: Mapping[str, Any], path: Path) -> None:
    """Log the per-stage status, duration and peak memory of a run."""
    logger.info("%-18s %-8s %10s %12s", "stage", "status", "seconds", "peak MiB")
    for stage in summary["stages"]:
        timing = stage["timing"] or {}
        seconds = timing.get("duration_s")
        peak = timing.get("peak_rss_mib")
        logger.info(
            "%-18s %-8s %10s %12s",
            stage["stage"],
            stage["status"],
            "-" if seconds is None else f"{seconds:.2f}",
            "-" if peak is None else f"{peak:.1f}",
        )
    total = summary["timing"]
    logger.info(
        "run-all %s in %.2f s (peak RSS %s MiB); summary written to %s",
        summary["status"],
        total["total_duration_s"],
        total["peak_rss_mib"],
        path,
    )


# --------------------------------------------------------------------------------------------
# reproduce
# --------------------------------------------------------------------------------------------


def runs_dir(output_root: Path, scale: str) -> Path:
    """Folder of the reproducibility runs, ``<scale>/reproducibility/runs``."""
    return report_dir(output_root, scale, ReportKind.REPRODUCIBILITY) / RUNS_DIR


def _run_facts(scale_root: Path) -> dict[str, dict[str, Any]]:
    """Describe every file of one run, with the content hashes of its JSON run records."""
    files = describe_files(scale_root)
    documents = {
        relative: read_json(scale_root / relative)
        for relative in reproducibility.CONTENT_HASHED
        if relative in files
    }
    return reproducibility.run_facts(files, documents)


def reproduce(
    options: RunOptions,
    *,
    seed: int | None,
    control_seed: int | None,
    verify_sql: bool,
    keep_runs: bool,
    commands: Callable[[Path, int | None], Mapping[PipelineStage, str]],
    run_all_command: Callable[[Path, int | None], str],
) -> dict[str, Any]:
    """Run the pipeline twice with one seed and once with a control seed; compare every file.

    Every run starts in an empty folder at one fixed path (``runs/current``). When it ends,
    every file is described (rows, sha256) and the run is removed, or, with ``keep_runs``,
    moved to ``runs/run1``, ``runs/run2`` or ``runs/control``. Because the path is the same,
    even the provenance records (which name ``--output-dir``) are comparable byte for byte.
    The report goes to ``<scale>/reproducibility/reproducibility_check.{md,json}``.

    Args:
        options: Shared options (``output_dir`` is where the report and runs go).
        seed: Seed of run1 and run2 (``None``: the config's seed, as a plain ``run-all``).
        control_seed: Seed of the control run (``None``: the run seed + 1).
        verify_sql: Passed to every run's ``export``.
        keep_runs: Keep ``runs/`` for inspection.
        commands: ``(run output root, seed)`` → the stage command lines of one run.
        run_all_command: ``(run output root, seed)`` → the equivalent ``run-all`` command line.

    Returns:
        The report payload (``verdict`` PASS only when every file matches and the control run
        changed the data).

    Raises:
        ReproducibilityError: If the control seed equals the run seed.
    """
    run_seed = load_config(options.config_path).seed if seed is None else seed
    control = run_seed + 1 if control_seed is None else control_seed
    if control == run_seed:
        raise ReproducibilityError(f"The control seed must differ from the run seed ({run_seed})")
    work = runs_dir(options.output_dir, options.scale.value)
    remove_directory(work)
    current = work / CURRENT_RUN
    plan = (("run1", seed, run_seed), ("run2", seed, run_seed), ("control", control, control))
    facts: dict[str, dict[str, dict[str, Any]]] = {}
    summaries: dict[str, dict[str, Any]] = {}
    for label, option_seed, effective_seed in plan:
        logger.info("Reproducibility %s (seed %d) in %s", label, effective_seed, current)
        summary = run_pipeline(
            replace(options, output_dir=current),
            seed=option_seed,
            verify_sql=verify_sql,
            commands=commands(current, option_seed),
            command=run_all_command(current, option_seed),
        )
        summaries[label] = summary
        if summary["exit_code"] != 0:
            break
        facts[label] = _run_facts(current / options.scale.value)
        if keep_runs:
            replace_directory(current, work / label)
        else:
            remove_directory(current)  # described; free the disk before the next run
    payload = reproducibility.build_report(
        scale=options.scale.value,
        seed=run_seed,
        control_seed=control,
        verify_sql=verify_sql,
        summaries=summaries,
        facts=facts,
    )
    target = report_dir(options.output_dir, options.scale.value, ReportKind.REPRODUCIBILITY)
    md_path, _ = write_report(
        target,
        reproducibility.REPORT_STEM,
        reproducibility.report_markdown(payload),
        payload,
    )
    if keep_runs:
        logger.info("Runs kept in %s", work)
    else:
        remove_directory(work)  # a failed run is left in runs/current until here
    totals = payload["totals"]
    logger.info(
        "Reproducibility %s: %d of %d files match; control run changed %s files; report %s",
        payload["verdict"],
        totals["matching"],
        totals["files"],
        payload["control"].get("changed", "n/a"),
        md_path,
    )
    return payload
