"""Command-line entry point: ``coldchain ...``.

Each pipeline command is a thin wrapper around one stage function of
:mod:`coldchain.pipeline`. Commands log what they did (row counts, output location) and how
long they took with their peak memory. They exit with status 1 on any
:class:`coldchain.errors.ColdchainError` or FAIL verdict, printing its message. Every pipeline
command also writes its provenance record to ``<output-dir>/<scale>/provenance/<stage>.json``
(command, seed, config hash, git commit, and the sha256 of every file read and written), which
``coldchain export`` checks before building the manifest.

``coldchain run-all`` runs every stage in one go and writes ``run_summary.json``;
``coldchain reproduce`` runs the pipeline three times and compares every file.
"""

import logging
import platform
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from coldchain import __version__, pipeline, provenance
from coldchain.config import DEFAULT_CONFIG_PATH, DEFAULT_OUTPUT_DIR
from coldchain.enums import DataSource, LogLevel, PipelineStage, Scale
from coldchain.errors import ColdchainError
from coldchain.observability import configure_logging
from coldchain.pipeline import RunOptions, StageResult

app = typer.Typer(
    name="coldchain",
    help="YottaFlex cold-chain compliance pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

logger = logging.getLogger("coldchain")

ScaleOption = Annotated[Scale, typer.Option("--scale", help="Scale preset.")]
SeedOption = Annotated[
    int | None, typer.Option("--seed", min=0, help="Override the seed from the config file.")
]
ConfigOption = Annotated[Path, typer.Option("--config", help="Path to the pipeline config YAML.")]
OutputOption = Annotated[
    Path, typer.Option("--output-dir", help="Output root; files go to <root>/<scale>/.")
]
VerifySqlOption = Annotated[
    bool,
    typer.Option(
        "--verify-sql/--no-verify-sql",
        help="Load the SQL into fresh DuckDB and SQLite databases (foreign keys enforced) "
        "and compare the row counts with the CSV files.",
    ),
]


@app.callback()
def main(
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Minimum level of the log lines shown.")
    ] = LogLevel.INFO,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Shorthand for --log-level DEBUG.")
    ] = False,
) -> None:
    """Configure logging for every command."""
    configure_logging(LogLevel.DEBUG if verbose else log_level)


def _run(action: Callable[[], Any]) -> Any:
    """Run a command body, turning a :class:`ColdchainError` into exit status 1."""
    try:
        return action()
    except ColdchainError as exc:
        _fail(str(exc))


def _fail(message: str) -> None:
    """Log and print an error message, then exit with status 1."""
    logger.error("%s", message)
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _command(ctx: typer.Context) -> str:
    """The effective command line of the running command (recorded in provenance)."""
    return provenance.command_line(ctx.info_name or "", ctx.command.params, ctx.params)


def _stage(ctx: typer.Context, stage: PipelineStage, action: Callable[[], StageResult]) -> None:
    """Run one stage as a command: time it, and exit with status 1 if it fails."""
    run = pipeline.execute_stage(stage, _command(ctx), action)
    if not run.passed:
        _fail(run.message or f"stage {stage.value} failed")


@app.command()
def version() -> None:
    """Print the package version and the Python version."""
    typer.echo(f"coldchain {__version__} (Python {platform.python_version()})")


@app.command()
def generate(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    seed: SeedOption = None,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
) -> None:
    """Generate master data, events and natural scenarios, plus the ground truth.

    Raw tables go to ``<output-dir>/<scale>/raw/``; the answer-key tables go to
    ``<output-dir>/<scale>/ground_truth/``. A previous injection's baseline snapshot and
    anomaly list are removed, since they belong to the data being replaced.
    """
    options = RunOptions(scale, config_path, output_dir)
    _stage(
        ctx,
        PipelineStage.GENERATE,
        lambda: pipeline.run_generate(options, seed=seed, command=_command(ctx)),
    )


@app.command()
def inject(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    seed: SeedOption = None,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
) -> None:
    """Inject the five labelled edge cases into the generated data.

    The first run saves the clean generator output to ``<output-dir>/<scale>/baseline/``.
    Every run starts from that snapshot, so re-running gives the same result. It rewrites
    ``raw/`` (only the target shipments' records change), updates
    ``ground_truth/expected_outcomes`` and writes ``ground_truth/anomalies`` (Parquet, CSV
    and JSON). Use the same seed as ``generate``: ``export`` refuses mixed seeds.
    """
    options = RunOptions(scale, config_path, output_dir)
    _stage(
        ctx,
        PipelineStage.INJECT,
        lambda: pipeline.run_inject(options, seed=seed, command=_command(ctx)),
    )


@app.command()
def check(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
) -> None:
    """Run the compliance engine on the raw tables.

    Reads only ``<output-dir>/<scale>/raw/`` (never the ground truth) and writes the excursion
    reports, their reading links, the decisions and the appended status events to
    ``<output-dir>/<scale>/compliance/``. Raw files are never modified.
    """
    options = RunOptions(scale, config_path, output_dir)
    _stage(ctx, PipelineStage.CHECK, lambda: pipeline.run_check(options, command=_command(ctx)))


@app.command()
def validate(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    source: Annotated[
        DataSource,
        typer.Option(
            "--source",
            help="raw: raw/ plus the engine output in compliance/; baseline: the clean "
            "pre-injection snapshot baseline/raw/ (no engine output).",
        ),
    ] = DataSource.RAW,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
) -> None:
    """Validate data integrity: keys, enums, dates, lifecycle, duplicates, reconciliation.

    Judges the raw tables (and the engine output in ``compliance/`` when present) on their own,
    without the answer key, and writes ``validation_summary.md`` and ``.json`` to
    ``<output-dir>/<scale>/validation/`` (``validation/baseline/`` for ``--source baseline``).
    Exits with status 1 if any INTEGRITY check fails; BUSINESS_RULE findings are reported for
    ``coldchain evaluate`` to explain.
    """
    options = RunOptions(scale, config_path, output_dir)
    stage = PipelineStage.VALIDATE if source is DataSource.RAW else PipelineStage.VALIDATE_BASELINE
    _stage(
        ctx,
        stage,
        lambda: pipeline.run_validate(options, source=source, command=_command(ctx)),
    )


@app.command()
def evaluate(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
) -> None:
    """Evaluate the engine against the ground truth and explain every validation finding.

    Needs ``check`` and ``validate`` to have run on the current data. Writes
    ``evaluation_report.md`` and ``.json`` to ``<output-dir>/<scale>/evaluation/``. Exits with
    status 1 on any unexplained validation finding, or (at a strict scale, ``starter`` and
    ``full`` by default) on any metric below 100 %.
    """
    options = RunOptions(scale, config_path, output_dir)
    _stage(
        ctx, PipelineStage.EVALUATE, lambda: pipeline.run_evaluate(options, command=_command(ctx))
    )


@app.command()
def export(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
    verify_sql: VerifySqlOption = True,
) -> None:
    """Export every table as CSV, JSON and SQL, with a manifest.

    Needs the whole pipeline (generate → [inject] → check → validate → evaluate, with a PASS
    verdict) to have run on the current files: each stage's provenance record must still match
    the files on disk, and ``generate`` and ``inject`` must have used one seed. Writes
    ``<output-dir>/<scale>/export/`` (replaced as a whole only when everything succeeded):
    ``csv/<layer>/``, ``json/<layer>/``, ``sql/schema.sql`` and ``sql/seed.sql`` (raw +
    compliance), ``sql/ground_truth_*.sql`` (answer key, separate), the reports, the data
    dictionary, a README and ``manifest.json``.
    """
    options = RunOptions(scale, config_path, output_dir)
    _stage(
        ctx,
        PipelineStage.EXPORT,
        lambda: pipeline.run_export(options, verify_sql=verify_sql, command=_command(ctx)),
    )


# --------------------------------------------------------------------------------------------
# run-all and reproduce
# --------------------------------------------------------------------------------------------


def _root_group(ctx: typer.Context) -> TyperGroup:
    """The ``coldchain`` command group (to read each sub-command's options)."""
    group = ctx.find_root().command
    if not isinstance(group, TyperGroup):  # pragma: no cover - always a group via the app
        raise TypeError("coldchain commands must run inside the command group")
    return group


def _command_for(group: TyperGroup, name: str, values: Mapping[str, Any]) -> str:
    """The command line ``coldchain <name>`` would record for these option values."""
    command = group.commands[name]
    return provenance.command_line(name, command.params, values)


def stage_commands(
    group: TyperGroup,
    *,
    scale: Scale,
    seed: int | None,
    config_path: Path,
    output_dir: Path,
    verify_sql: bool,
) -> dict[PipelineStage, str]:
    """The command line of every ``run-all`` stage, as its single-stage command records it.

    Args:
        group: The ``coldchain`` command group.
        scale: Scale preset.
        seed: Seed override for ``generate`` and ``inject`` (``None``: the config's seed).
        config_path: Pipeline config file.
        output_dir: Output root.
        verify_sql: ``export``'s SQL proof flag.

    Returns:
        Stage → command line; ``run-all`` records exactly what the separate commands would.
    """
    shared = {"scale": scale, "config_path": config_path, "output_dir": output_dir}
    seeded = {**shared, "seed": seed}
    values: dict[PipelineStage, tuple[str, dict[str, Any]]] = {
        PipelineStage.GENERATE: ("generate", seeded),
        PipelineStage.INJECT: ("inject", seeded),
        PipelineStage.CHECK: ("check", shared),
        PipelineStage.VALIDATE: ("validate", {**shared, "source": DataSource.RAW}),
        PipelineStage.VALIDATE_BASELINE: ("validate", {**shared, "source": DataSource.BASELINE}),
        PipelineStage.EVALUATE: ("evaluate", shared),
        PipelineStage.EXPORT: ("export", {**shared, "verify_sql": verify_sql}),
    }
    return {stage: _command_for(group, name, vals) for stage, (name, vals) in values.items()}


@app.command("run-all")
def run_all(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    seed: SeedOption = None,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
    verify_sql: VerifySqlOption = True,
) -> None:
    """Run the whole pipeline: generate → inject → check → validate → evaluate → export.

    Also validates the clean baseline snapshot (``validate --source baseline``) after
    ``validate``. Stops at the first stage that fails, naming it. Each stage writes the same
    files and provenance record as its own command would. Writes
    ``<output-dir>/<scale>/run_summary.json``: every stage with its status, command, row
    counts, verdict, duration and peak memory, and the run's exit status.
    """

    def body() -> None:
        options = RunOptions(scale, config_path, output_dir)
        commands = stage_commands(
            _root_group(ctx),
            scale=scale,
            seed=seed,
            config_path=config_path,
            output_dir=output_dir,
            verify_sql=verify_sql,
        )
        summary = pipeline.run_pipeline(
            options, seed=seed, verify_sql=verify_sql, commands=commands, command=_command(ctx)
        )
        if summary["exit_code"] != 0:
            _fail(f"run-all stopped at stage {summary['failed_stage']}: {summary['error']}")

    _run(body)


@app.command()
def reproduce(
    ctx: typer.Context,
    scale: ScaleOption = Scale.STARTER,
    seed: SeedOption = None,
    control_seed: Annotated[
        int | None,
        typer.Option("--control-seed", min=0, help="Seed of the control run (default: seed + 1)."),
    ] = None,
    config_path: ConfigOption = DEFAULT_CONFIG_PATH,
    output_dir: OutputOption = DEFAULT_OUTPUT_DIR,
    verify_sql: Annotated[
        bool,
        typer.Option(
            "--verify-sql/--no-verify-sql",
            help="Prove the SQL load inside every run (off by default: identical SQL bytes "
            "load identically, and run-all/export prove the load).",
        ),
    ] = False,
    keep_runs: Annotated[
        bool,
        typer.Option(
            "--keep-runs/--no-keep-runs",
            help="Keep the three runs in <scale>/reproducibility/runs/ for inspection.",
        ),
    ] = False,
) -> None:
    """Prove reproducibility: the same seed gives identical files, another seed does not.

    Runs the whole pipeline (as ``run-all``) twice with the same seed and once with a control
    seed, each into an empty folder at the same path under
    ``<output-dir>/<scale>/reproducibility/runs/``. Compares the row count and sha256 of every
    file of run1 and run2 (the export manifest and the run summary on their content without
    the wall-clock values) and checks that the control seed changed the data. Writes
    ``<output-dir>/<scale>/reproducibility/reproducibility_check.md`` and ``.json``. Exits
    with status 1 on any mismatch, failed run, or unchanged control run.
    """

    def body() -> None:
        group = _root_group(ctx)

        def commands(root: Path, run_seed: int | None) -> dict[PipelineStage, str]:
            return stage_commands(
                group,
                scale=scale,
                seed=run_seed,
                config_path=config_path,
                output_dir=root,
                verify_sql=verify_sql,
            )

        def run_all_command(root: Path, run_seed: int | None) -> str:
            values = {
                "scale": scale,
                "seed": run_seed,
                "config_path": config_path,
                "output_dir": root,
                "verify_sql": verify_sql,
            }
            return _command_for(group, "run-all", values)

        payload = pipeline.reproduce(
            RunOptions(scale, config_path, output_dir),
            seed=seed,
            control_seed=control_seed,
            verify_sql=verify_sql,
            keep_runs=keep_runs,
            commands=commands,
            run_all_command=run_all_command,
        )
        if payload["verdict"] != "PASS":
            _fail("reproducibility check FAILED: " + "; ".join(payload["failures"]))

    _run(body)
