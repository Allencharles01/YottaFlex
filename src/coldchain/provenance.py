"""Stage provenance: which command, seed, config and code wrote each pipeline file.

Every pipeline command (``generate``, ``inject``, ``check``, ``validate``, ``evaluate``,
``export``) writes one small JSON record to ``output/<scale>/provenance/<stage>.json``:

* the command line (effective options, so defaults are explicit) and the scale and seed;
* the package version and the git commit with a dirty flag;
* the config fingerprint: the hash of the validated pipeline settings (without ``export:``
  and ``seed``) and of every reference file, plus one combined hash;
* the sha256 of every file the stage read (``inputs``) and wrote (``outputs``), keyed by
  their path relative to the output root.

Records hold no wall-clock time, so a rerun on the same commit writes the same bytes.
``coldchain export`` checks the chain of records with :func:`verify_chain` before it builds
the manifest. That way the manifest's seed and commands are the ones that actually produced
the exported bytes, not assumptions (DECISIONS.md #98).

The functions here are pure, except :func:`git_state`, which asks ``git`` about the
repository.
"""

import hashlib
import json
import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from coldchain import __version__
from coldchain.config import DEFAULT_REFERENCE_DIR, PROJECT_ROOT, REFERENCE_FILES, AppConfig
from coldchain.enums import PipelineStage
from coldchain.errors import ProvenanceError

PROVENANCE_VERSION = 1
"""Version of the provenance record layout."""

_GIT_TIMEOUT_S = 10


@dataclass(frozen=True)
class GitState:
    """The repository state a command ran from.

    Attributes:
        commit: Full hash of ``HEAD``, or ``None`` outside a git checkout.
        dirty: ``True`` if tracked or untracked (non-ignored) files differ from ``HEAD``;
            ``None`` when unknown.
    """

    commit: str | None
    dirty: bool | None

    def as_dict(self) -> dict[str, Any]:
        """Return the state as a JSON-ready mapping."""
        return {"commit": self.commit, "dirty": self.dirty}


def git_state(repo: Path = PROJECT_ROOT) -> GitState:
    """Ask git for the commit and dirty flag of a repository.

    Args:
        repo: Repository folder (the project root by default).

    Returns:
        The state, or ``GitState(None, None)`` if git is missing or ``repo`` is not a checkout.
    """

    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(  # fixed argument list, no shell
                ["git", "-C", str(repo), *args],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if commit is None:
        return GitState(None, None)
    status = git("status", "--porcelain", "--untracked-files=normal")
    return GitState(commit.strip(), None if status is None else bool(status.strip()))


def display_path(path: Path) -> str:
    """Return a path relative to the project root when it lies inside it, else absolute.

    Args:
        path: Any path.

    Returns:
        A POSIX path string, e.g. ``config/default.yaml`` or ``/tmp/run/output``.
    """
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix() or "."
    except ValueError:
        return resolved.as_posix()


def combined_sha256(digests: Mapping[str, str]) -> str:
    """Hash a set of file digests into one, independent of their order.

    The input is rendered like ``sha256sum`` output (``<sha256>  <name>`` per line, sorted by
    name) and hashed.

    Args:
        digests: File name → hex sha256.

    Returns:
        The combined hex sha256.
    """
    lines = "".join(f"{digests[name]}  {name}\n" for name in sorted(digests))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


EXCLUDED_CONFIG_FIELDS: frozenset[str] = frozenset({"export", "seed"})
"""Top-level config fields left out of the pipeline fingerprint (DECISIONS.md #108).

``export`` only shapes the export bundle, so changing it must not make the pipeline stale (its
own hash is in the manifest, :func:`settings_sha256`). ``seed`` is recorded per stage instead,
so ``generate --seed N`` is not mistaken for a different config.
"""


def settings_sha256(settings: Mapping[str, Any]) -> str:
    """Hash JSON-ready settings: sha256 of their sorted-key, compact JSON rendering.

    Args:
        settings: Settings as produced by ``model_dump(mode="json")``.

    Returns:
        The hex sha256 (independent of key order, YAML comments and formatting).
    """
    text = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def config_fingerprint(
    config: AppConfig,
    config_path: Path,
    digest: Callable[[Path], str],
    reference_dir: Path = DEFAULT_REFERENCE_DIR,
) -> dict[str, Any]:
    """Fingerprint the pipeline settings and the reference files.

    The pipeline settings are the *validated* config without :data:`EXCLUDED_CONFIG_FIELDS`,
    hashed with :func:`settings_sha256`. So only a change that can alter the pipeline's data
    changes the fingerprint; YAML comments, formatting and ``export:`` settings do not.

    Args:
        config: The validated config the stage ran with.
        config_path: The pipeline config file (``--config``), recorded for reference.
        digest: Returns a file's hex sha256 (``coldchain.io.file_sha256``).
        reference_dir: Folder of the reference YAML files.

    Returns:
        ``{"path", "sha256", "files"}``: the config's display path, the combined hash
        (:func:`combined_sha256`) and its parts: ``pipeline_settings`` and each reference file.
    """
    settings = config.model_dump(mode="json", exclude=set(EXCLUDED_CONFIG_FIELDS))
    files = {"pipeline_settings": settings_sha256(settings)}
    for file_name in sorted(REFERENCE_FILES.values()):
        files[f"reference/{file_name}"] = digest(Path(reference_dir) / file_name)
    return {"path": display_path(config_path), "sha256": combined_sha256(files), "files": files}


def export_settings(config: AppConfig) -> dict[str, Any]:
    """Return the ``export:`` settings and their own hash, for the manifest.

    Args:
        config: The validated config ``export`` ran with.

    Returns:
        ``{"sha256", "values"}``.
    """
    values = config.export.model_dump(mode="json")
    return {"sha256": settings_sha256(values), "values": values}


def command_line(name: str, params: Sequence[Any], values: Mapping[str, Any]) -> str:
    """Rebuild a ``coldchain`` command line from its effective option values.

    Options left at ``None`` are omitted; every other option is spelled out, so defaults are
    explicit. Boolean ``--x/--no-x`` flags show the side that was in effect.

    Args:
        name: Sub-command name, e.g. ``export``.
        params: The command's parameter objects (``ctx.command.params``).
        values: Their values (``ctx.params``).

    Returns:
        A shell-quoted command line, e.g. ``coldchain check --scale full --output-dir output``.
    """
    parts = ["coldchain", name]
    for param in params:
        value = values.get(param.name)
        opts = getattr(param, "opts", [])
        if value is None or not opts:
            continue
        if getattr(param, "is_flag", False):
            secondary = getattr(param, "secondary_opts", [])
            if value:
                parts.append(opts[0])
            elif secondary:
                parts.append(secondary[0])
            continue
        if isinstance(value, Enum):
            text = str(value.value)
        elif isinstance(value, Path):
            text = display_path(value)
        else:
            text = str(value)
        parts += [opts[0], text]
    return shlex.join(parts)


def stage_record(
    stage: PipelineStage,
    *,
    command: str,
    scale: str,
    seed: int | None,
    config: Mapping[str, Any],
    git: GitState,
    inputs: Mapping[str, str],
    outputs: Mapping[str, str],
) -> dict[str, Any]:
    """Build one stage's provenance record.

    Args:
        stage: The stage.
        command: Its command line (:func:`command_line`).
        scale: Scale name.
        seed: The seed it used (``None`` for stages that draw no random numbers).
        config: The config fingerprint (:func:`config_fingerprint`).
        git: The repository state.
        inputs: Path (relative to the output root) → sha256 of every file read.
        outputs: Path → sha256 of every file written.

    Returns:
        A JSON-ready record with sorted ``inputs`` and ``outputs``.
    """
    return {
        "provenance_version": PROVENANCE_VERSION,
        "stage": stage.value,
        "command": command,
        "scale": scale,
        "seed": seed,
        "package_version": __version__,
        "git": git.as_dict(),
        "config": dict(config),
        "inputs": dict(sorted(inputs.items())),
        "outputs": dict(sorted(outputs.items())),
    }


def verify_chain(
    records: Mapping[PipelineStage, Mapping[str, Any]],
    current: Callable[[str], str | None],
    *,
    scale: str,
    config_sha256: str,
    required: Iterable[str],
) -> None:
    """Check that a chain of stage records still describes the files on disk.

    The chain is valid when:

    1. every record is for ``scale`` and was made with the config ``config_sha256``;
    2. every file a stage read is unchanged since (otherwise that stage is stale);
    3. every file a stage wrote is unchanged, unless a later stage (in pipeline order)
       rewrote it, as ``inject`` rewrites ``raw/``;
    4. every path in ``required`` (the files the caller will rely on) was written by a stage.

    Args:
        records: Stage → its record (only the stages that make up the chain).
        current: Returns the current sha256 of a path relative to the output root, or ``None``
            if the file does not exist.
        scale: The scale being checked.
        config_sha256: The combined config hash the chain must have used.
        required: Paths (relative to the output root) that must be stage outputs.

    Raises:
        ProvenanceError: Listing every problem found, with the stage to re-run.
    """
    problems: list[str] = []
    ordered = [stage for stage in PipelineStage if stage in records]
    last_writer: dict[str, PipelineStage] = {}
    for stage in ordered:
        for path in records[stage].get("outputs", {}):
            last_writer[path] = stage

    for stage in ordered:
        record = records[stage]
        name = stage.value
        if record.get("scale") != scale:
            problems.append(f"{name}: recorded for scale {record.get('scale')!r}, not {scale!r}")
        if record.get("config", {}).get("sha256") != config_sha256:
            problems.append(f"{name}: ran with a different config (re-run `coldchain {name}`)")
        for path, digest in record.get("inputs", {}).items():
            if current(path) != digest:
                problems.append(f"{name}: input {path} changed since (re-run `coldchain {name}`)")
        for path, digest in record.get("outputs", {}).items():
            if last_writer[path] is stage and current(path) != digest:
                problems.append(f"{name}: output {path} was modified or removed since")
    problems += [
        f"{path} was not written by any recorded stage"
        for path in required
        if path not in last_writer
    ]
    if problems:
        shown = "; ".join(problems[:5])
        more = f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""
        raise ProvenanceError(f"Stale or incomplete pipeline: {shown}{more}")
