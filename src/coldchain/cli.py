"""Command-line entry point: ``coldchain ...``.

Commands log what they did (row counts, output location) and exit with status 1 on any
:class:`coldchain.errors.ColdchainError`, printing its message.
"""

import logging
import platform
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, TypeVar

import typer

from coldchain import __version__
from coldchain.compliance import ENGINE_INPUTS, check_tables
from coldchain.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    load_config,
    load_reference,
)
from coldchain.enums import FileFormat, Layer, Scale, TableName
from coldchain.errors import ColdchainError
from coldchain.generate import generate_tables
from coldchain.inject import INJECT_INPUTS, inject_tables
from coldchain.io import (
    DEFAULT_FORMATS,
    baseline_dir,
    clear_injection,
    ensure_baseline,
    layer_dir,
    read_table,
    read_tables,
    write_layers,
    write_table,
)
from coldchain.schemas import TABLE_LAYERS

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
