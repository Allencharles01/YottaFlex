"""Command-line entry point: ``coldchain ...``.

Commands log what they did (row counts, output location) and exit with status 1 on any
:class:`coldchain.errors.ColdchainError`, printing its message. Every pipeline command also
writes its provenance record to ``<output-dir>/<scale>/provenance/<stage>.json`` (command,
seed, config hash, git commit, and the sha256 of every file read and written), which
``coldchain export`` checks before building the manifest.
"""

import functools
import logging
import platform
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, TypeVar

import pandas as pd
import typer

from coldchain import __version__, provenance
from coldchain.compliance import ENGINE_INPUTS, check_tables
from coldchain.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    PROJECT_ROOT,
    AppConfig,
    load_config,
    load_reference,
)
from coldchain.enums import (
    DataSource,
    FileFormat,
    Layer,
    PipelineStage,
    ReportKind,
    Scale,
    TableName,
)
from coldchain.errors import ColdchainError, EvaluationError, ExportError
from coldchain.evaluation import EVALUATION_INPUTS, REPORT_STEM, evaluate_tables
from coldchain.evaluation.report import report_markdown
from coldchain.export import EXPORT_TABLES, write_bundle
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
from coldchain.schemas import SCHEMAS, TABLE_LAYERS
from coldchain.validation import COMPLIANCE_INPUTS, RAW_INPUTS, validate_tables
from coldchain.validation.summary import SUMMARY_STEM, summary_markdown, summary_payload

app = typer.Typer(
    name="coldchain",
    help="YottaFlex cold-chain compliance pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

logger = logging.getLogger("coldchain")

T = TypeVar("T")

PARQUET = FileFormat.PARQUET

CHAIN_STAGES: tuple[PipelineStage, ...] = (
    PipelineStage.GENERATE,
    PipelineStage.INJECT,
    PipelineStage.CHECK,
    PipelineStage.VALIDATE,
    PipelineStage.EVALUATE,
)
"""Stages whose provenance ``export`` checks; all but ``inject`` are required."""

EXPORT_DOCS: dict[str, Path] = {
    "docs/data_dictionary.md": PROJECT_ROOT / "docs" / "data_dictionary.md",
}
"""Project documents copied into the export bundle (bundle path → source)."""


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging."),
) -> None:
    """Configure logging for every command."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


def _run(action: Callable[[], T]) -> T:
    """Run a command body, turning a :class:`ColdchainError` into exit status 1."""
    try:
        return action()
    except ColdchainError as exc:
        logger.error("%s", exc)
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


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


def _record(
    ctx: typer.Context,
    stage: PipelineStage,
    *,
    scale: Scale,
    output_dir: Path,
    config: AppConfig,
    config_path: Path,
    seed: int | None,
    inputs: Iterable[Path],
    outputs: Iterable[Path],
) -> Path:
    """Write a stage's provenance record (see :mod:`coldchain.provenance`)."""
    record = provenance.stage_record(
        stage,
        command=provenance.command_line(
            ctx.info_name or stage.value, ctx.command.params, ctx.params
        ),
        scale=scale.value,
        seed=seed,
        config=provenance.config_fingerprint(config, config_path, file_sha256),
        git=provenance.git_state(),
        inputs=_digests(inputs, output_dir),
        outputs=_digests(outputs, output_dir),
    )
    path = write_json(provenance_path(output_dir, scale.value, stage), record)
    logger.debug("Wrote provenance record %s", path)
    return path


@app.command()
def version() -> None:
    """Print the package version and the Python version."""
    typer.echo(f"coldchain {__version__} (Python {platform.python_version()})")


@app.command()
def generate(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to generate.")] = (
        Scale.STARTER
    ),
    seed: Annotated[
        int | None, typer.Option("--seed", min=0, help="Override the seed from the config file.")
    ] = None,
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path,
        typer.Option(
            "--output-dir", help="Output root; tables go to <root>/<scale>/{raw,ground_truth}."
        ),
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Generate master data, events and natural scenarios, plus the ground truth.

    Raw tables go to ``<output-dir>/<scale>/raw/``; the answer-key tables go to
    ``<output-dir>/<scale>/ground_truth/``. A previous injection's baseline snapshot and
    anomaly list are removed, since they belong to the data being replaced.
    """

    def body() -> None:
        config = load_config(config_path)
        if seed is not None:
            config = config.model_copy(update={"seed": seed})
        logger.info(
            "Generating scale=%s seed=%d (config %s)", scale.value, config.seed, config_path
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
            ctx,
            PipelineStage.GENERATE,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
            seed=config.seed,
            inputs=(),
            outputs=_table_files(output_dir, scale.value, tables, DEFAULT_FORMATS),
        )

    _run(body)


@app.command()
def inject(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to inject.")] = (
        Scale.STARTER
    ),
    seed: Annotated[
        int | None, typer.Option("--seed", min=0, help="Override the seed from the config file.")
    ] = None,
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Output root holding <root>/<scale>/ from `generate`."),
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Inject the five labelled edge cases into the generated data.

    The first run saves the clean generator output to ``<output-dir>/<scale>/baseline/``.
    Every run starts from that snapshot, so re-running gives the same result. It rewrites
    ``raw/`` (only the target shipments' records change), updates
    ``ground_truth/expected_outcomes`` and writes ``ground_truth/anomalies`` (Parquet, CSV
    and JSON).
    """

    def body() -> None:
        config = load_config(config_path)
        if seed is not None:
            config = config.model_copy(update={"seed": seed})
        if ensure_baseline(output_dir, scale.value):
            logger.info("Snapshot of the clean data taken before the first injection")
        logger.info("Injecting scale=%s seed=%d (config %s)", scale.value, config.seed, config_path)
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
        for name, frame in {**tables, TableName.ANOMALIES: anomalies}.items():
            logger.info("  %-13s %-24s %8d rows", TABLE_LAYERS[name].value, name.value, len(frame))
        logger.info(
            "Wrote %d anomaly rows (%d anomalies) to %s",
            len(anomalies),
            anomalies["anomaly_id"].nunique(),
            written[Layer.GROUND_TRUTH],
        )
        _record(
            ctx,
            PipelineStage.INJECT,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
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

    _run(body)


@app.command()
def check(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to check.")] = (
        Scale.STARTER
    ),
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path,
        typer.Option("--output-dir", help="Output root holding <root>/<scale>/raw/."),
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Run the compliance engine on the raw tables.

    Reads only ``<output-dir>/<scale>/raw/`` (never the ground truth) and writes the excursion
    reports, their reading links, the decisions and the appended status events to
    ``<output-dir>/<scale>/compliance/``. Raw files are never modified.
    """

    def body() -> None:
        config = load_config(config_path)
        raw_dir = layer_dir(output_dir, scale.value, Layer.RAW)
        logger.info("Checking scale=%s from %s", scale.value, raw_dir)
        tables = check_tables(config, read_tables(ENGINE_INPUTS, raw_dir))
        written = write_layers(tables, output_dir, scale.value)
        for name, frame in tables.items():
            logger.info("  %-13s %-26s %8d rows", TABLE_LAYERS[name].value, name.value, len(frame))
        logger.info(
            "Wrote %d compliance tables (Parquet + CSV) to %s",
            len(tables),
            written[Layer.COMPLIANCE],
        )
        _record(
            ctx,
            PipelineStage.CHECK,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
            seed=None,
            inputs=_table_files(output_dir, scale.value, ENGINE_INPUTS, [FileFormat.PARQUET]),
            outputs=_table_files(output_dir, scale.value, tables, DEFAULT_FORMATS),
        )

    _run(body)


def validation_dir(output_root: Path, scale: str, source: DataSource) -> Path:
    """Folder of a validation summary: ``validation/`` (raw) or ``validation/baseline/``."""
    folder = report_dir(output_root, scale, ReportKind.VALIDATION)
    return folder if source is DataSource.RAW else folder / source.value


@app.command()
def validate(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to validate.")] = (
        Scale.STARTER
    ),
    source: Annotated[
        DataSource,
        typer.Option(
            "--source",
            help="raw: raw/ plus the engine output in compliance/; baseline: the clean "
            "pre-injection snapshot baseline/raw/ (no engine output).",
        ),
    ] = DataSource.RAW,
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Output root holding <root>/<scale>/.")
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Validate data integrity: keys, enums, dates, lifecycle, duplicates, reconciliation.

    Judges the raw tables (and the engine output in ``compliance/`` when present) on their own,
    without the answer key, and writes ``validation_summary.md`` and ``.json`` to
    ``<output-dir>/<scale>/validation/`` (``validation/baseline/`` for ``--source baseline``).
    Exits with status 1 if any INTEGRITY check fails; BUSINESS_RULE findings are reported for
    ``coldchain evaluate`` to explain.
    """

    def body() -> None:
        config = load_config(config_path)
        if source is DataSource.RAW:
            raw_dir = layer_dir(output_dir, scale.value, Layer.RAW)
        else:
            raw_dir = baseline_dir(output_dir, scale.value, Layer.RAW)
        compliance_dir = layer_dir(output_dir, scale.value, Layer.COMPLIANCE)
        directories = {name: raw_dir for name in RAW_INPUTS}
        present = all(
            table_path(compliance_dir, name, FileFormat.PARQUET).is_file()
            for name in COMPLIANCE_INPUTS
        )
        if source is DataSource.RAW and present:
            directories.update({name: compliance_dir for name in COMPLIANCE_INPUTS})
        elif source is DataSource.RAW:
            logger.warning(
                "No complete engine output in %s: its checks are skipped", compliance_dir
            )
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
            ctx,
            PipelineStage.VALIDATE if source is DataSource.RAW else PipelineStage.VALIDATE_BASELINE,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
            seed=None,
            inputs=[output_dir / relative for relative in inputs],
            outputs=[md_path, json_path],
        )
        if not result.passed:
            failed = ", ".join(r.check.check_id for r in result.integrity_failures)
            typer.echo(f"Error: integrity checks failed: {failed}", err=True)
            raise typer.Exit(code=1)

    _run(body)


def _require_fresh(validation: dict, output_root: Path) -> None:
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


@app.command()
def evaluate(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to evaluate.")] = (
        Scale.STARTER
    ),
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Output root holding <root>/<scale>/.")
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Evaluate the engine against the ground truth and explain every validation finding.

    Needs ``check`` and ``validate`` to have run on the current data. Writes
    ``evaluation_report.md`` and ``.json`` to ``<output-dir>/<scale>/evaluation/``. Exits with
    status 1 on any unexplained validation finding, or (at a strict scale, ``starter`` and
    ``full`` by default) on any metric below 100 %.
    """

    def body() -> None:
        config = load_config(config_path)
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
            "Wrote %s and %s: verdict %s; %d of %d shipments fully correct; "
            "unexplained findings: %d",
            md_path,
            json_path.name,
            payload["verdict"],
            payload["fully_correct_shipments"],
            payload["shipments"],
            payload["validation"]["unexplained"],
        )
        _record(
            ctx,
            PipelineStage.EVALUATE,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
            seed=None,
            inputs=read_paths,
            outputs=[md_path, json_path],
        )
        if not result.passed:
            for failure in result.failures:
                typer.echo(f"Error: {failure}", err=True)
            raise typer.Exit(code=1)

    _run(body)


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


@app.command()
def export(
    ctx: typer.Context,
    scale: Annotated[Scale, typer.Option("--scale", help="Scale preset to export.")] = (
        Scale.STARTER
    ),
    config_path: Annotated[
        Path, typer.Option("--config", help="Path to the pipeline config YAML.")
    ] = DEFAULT_CONFIG_PATH,
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Output root holding <root>/<scale>/.")
    ] = DEFAULT_OUTPUT_DIR,
    verify_sql: Annotated[
        bool,
        typer.Option(
            "--verify-sql/--no-verify-sql",
            help="Load the SQL into fresh DuckDB and SQLite databases (foreign keys enforced) "
            "and compare the row counts with the CSV files.",
        ),
    ] = True,
) -> None:
    """Export every table as CSV, JSON and SQL, with a manifest (Phase 7).

    Needs the whole pipeline (generate → [inject] → check → validate → evaluate, with a PASS
    verdict) to have run on the current files: each stage's provenance record must still match
    the files on disk. Writes ``<output-dir>/<scale>/export/`` (replaced as a whole only when
    everything succeeded): ``csv/<layer>/``, ``json/<layer>/``, ``sql/schema.sql`` and
    ``sql/seed.sql`` (raw + compliance), ``sql/ground_truth_*.sql`` (answer key, separate),
    the reports, the data dictionary, a README and ``manifest.json``.
    """

    def body() -> None:
        config = load_config(config_path)
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
        config_fp = provenance.config_fingerprint(config, config_path, file_sha256)
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
            f"reports/{path.name}": path
            for path in (summary_md, summary_json, report_md, report_json)
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
            command=provenance.command_line(
                ctx.info_name or "export", ctx.command.params, ctx.params
            ),
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
            ctx,
            PipelineStage.EXPORT,
            scale=scale,
            output_dir=output_dir,
            config=config,
            config_path=config_path,
            seed=None,
            inputs=read_paths,
            outputs=[target / f.path for f in files] + [target / MANIFEST_NAME],
        )
        for entry in entries:
            if entry["kind"] in ("sql", "sql.gz") and entry["rows"] is not None:
                logger.info(
                    "  %-32s %10d rows %9.1f MiB",
                    entry["path"],
                    entry["rows"],
                    entry["bytes"] / MIB,
                )
        logger.info(
            "Wrote %d files (%.1f MiB) and %s to %s; SQL load check: %s; dataset_sha256 %s",
            manifest["totals"]["files"],
            manifest["totals"]["bytes"] / MIB,
            MANIFEST_NAME,
            target,
            ", ".join(f"{k} {v['status']}" for k, v in proof.items()) if proof else "skipped",
            manifest["dataset_sha256"],
        )

    _run(body)
