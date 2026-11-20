"""Command-line entry point: ``coldchain ...``.

Commands log what they did (row counts, output location) and exit with status 1 on any
:class:`coldchain.errors.ColdchainError`, printing its message.
"""

import logging
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import pandas as pd
import typer

from coldchain import __version__
from coldchain.compliance import ENGINE_INPUTS, check_tables
from coldchain.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    load_config,
    load_reference,
)
from coldchain.enums import DataSource, FileFormat, Layer, ReportKind, Scale, TableName
from coldchain.errors import ColdchainError, EvaluationError
from coldchain.evaluation import EVALUATION_INPUTS, REPORT_STEM, evaluate_tables
from coldchain.evaluation.report import report_markdown
from coldchain.generate import generate_tables
from coldchain.inject import INJECT_INPUTS, inject_tables
from coldchain.io import (
    DEFAULT_FORMATS,
    baseline_dir,
    clear_injection,
    coerce_to_schema,
    ensure_baseline,
    file_sha256,
    layer_dir,
    read_report,
    read_table,
    read_tables,
    report_dir,
    table_path,
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


@app.command()
def version() -> None:
    """Print the package version and the Python version."""
    typer.echo(f"coldchain {__version__} (Python {platform.python_version()})")


@app.command()
def generate(
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

    _run(body)


@app.command()
def inject(
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

    _run(body)


@app.command()
def check(
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

    _run(body)


def validation_dir(output_root: Path, scale: str, source: DataSource) -> Path:
    """Folder of a validation summary: ``validation/`` (raw) or ``validation/baseline/``."""
    folder = report_dir(output_root, scale, ReportKind.VALIDATION)
    return folder if source is DataSource.RAW else folder / source.value


@app.command()
def validate(
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
        validation = read_report(
            validation_dir(output_dir, scale.value, DataSource.RAW), SUMMARY_STEM
        )
        _require_fresh(validation, output_dir)
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
        if not result.passed:
            for failure in result.failures:
                typer.echo(f"Error: {failure}", err=True)
            raise typer.Exit(code=1)

    _run(body)
