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
from coldchain.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_OUTPUT_DIR,
    load_config,
    load_reference,
)
from coldchain.enums import Layer, Scale
from coldchain.errors import ColdchainError
from coldchain.generate import generate_master_tables
from coldchain.io import layer_dir, write_tables

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
        typer.Option("--output-dir", help="Output root; tables go to <root>/<scale>/raw."),
    ] = DEFAULT_OUTPUT_DIR,
) -> None:
    """Generate the master data (parties … loggers) into ``<output-dir>/<scale>/raw/``."""

    def body() -> None:
        config = load_config(config_path)
        if seed is not None:
            config = config.model_copy(update={"seed": seed})
        logger.info(
            "Generating scale=%s seed=%d (config %s)", scale.value, config.seed, config_path
        )
        tables = generate_master_tables(config, load_reference(), scale)
        target = layer_dir(output_dir, scale.value, Layer.RAW)
        write_tables(tables, target)
        for name, frame in tables.items():
            logger.info("  %-24s %8d rows", name.value, len(frame))
        logger.info("Wrote %d tables (Parquet + CSV) to %s", len(tables), target)

    _run(body)
