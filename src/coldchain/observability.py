"""Run observability: logging setup, per-stage timing and peak memory, and the run summary.

* :func:`configure_logging` applies the ``coldchain --log-level`` option.
* :func:`measure` times a block of work and reports the peak resident memory (RSS) reached
  inside it. On Linux the process resets its own peak counter first (writing ``5`` to
  ``/proc/self/clear_refs``) and reads ``VmHWM`` from ``/proc/self/status`` afterwards, so each
  stage gets its own peak, not the peak of the whole process. Elsewhere it falls back to
  ``resource.getrusage`` (the process peak so far). The method used is recorded with the value.
* :func:`build_run_summary` assembles ``run_summary.json``, the machine-readable record of one
  ``coldchain run-all`` run: every stage with its status, command, row counts, verdict and
  timing, and the run's overall status and exit code.

Durations and memory are wall-clock measurements, so they differ between two runs of the same
data. They are kept under ``timing`` keys only, and :func:`run_summary_content_sha256` hashes
everything else, so two summaries of identical runs have the same content hash
(``coldchain reproduce`` compares that hash, DECISIONS.md #116).
"""

import hashlib
import json
import logging
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from coldchain import __version__
from coldchain.enums import LogLevel, StageStatus

try:
    import resource
except ImportError:  # pragma: no cover - not a POSIX platform
    resource = None

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
"""Format of every log line."""

RUN_SUMMARY_NAME = "run_summary.json"
"""File name of the run summary in ``<output-dir>/<scale>/``."""

RUN_SUMMARY_VERSION = 1
"""Version of the run-summary layout."""

TIMING_KEY = "timing"
"""Key of the wall-clock measurements, left out of :func:`run_summary_content_sha256`."""

_CONTENT_KEY = "content_sha256"
_MIB = 1 << 20
_PROC_STATUS = Path("/proc/self/status")
_PROC_CLEAR_REFS = Path("/proc/self/clear_refs")
_RESET_PEAK_RSS = "5"  # clear_refs code that resets the peak RSS (VmHWM), Linux >= 4.0


class MemoryMethod(StrEnum):
    """How a peak-memory value was measured."""

    STAGE_PEAK = "vmhwm_reset_per_stage"
    """Linux: the peak RSS counter was reset at the start of the measured block."""

    PROCESS_PEAK = "ru_maxrss_process_peak"
    """Fallback: the peak RSS of the whole process so far (may include earlier stages)."""

    UNAVAILABLE = "unavailable"
    """Neither source exists on this platform."""


def configure_logging(level: LogLevel) -> None:
    """Configure the root logger for a ``coldchain`` command.

    Args:
        level: The minimum level to show.
    """
    logging.basicConfig(level=getattr(logging, level.value), format=LOG_FORMAT, force=True)


@dataclass
class Measurement:
    """Duration and peak memory of a measured block, filled in when the block ends.

    Attributes:
        duration_s: Wall-clock seconds (``time.perf_counter``).
        peak_rss_bytes: Peak resident memory in bytes, or ``None`` when unavailable.
        memory_method: How ``peak_rss_bytes`` was measured.
    """

    duration_s: float = 0.0
    peak_rss_bytes: int | None = None
    memory_method: MemoryMethod = MemoryMethod.UNAVAILABLE

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-ready timing block: seconds (3 dp) and MiB (1 dp)."""
        return {
            "duration_s": round(self.duration_s, 3),
            "peak_rss_mib": None if self.peak_rss_bytes is None else mib(self.peak_rss_bytes),
            "memory_method": self.memory_method.value,
        }

    def describe(self) -> str:
        """Return a short human-readable form, e.g. ``1.62 s, peak RSS 250.1 MiB``."""
        memory = "n/a" if self.peak_rss_bytes is None else f"{mib(self.peak_rss_bytes):.1f} MiB"
        return f"{self.duration_s:.2f} s, peak RSS {memory}"


def mib(size: int) -> float:
    """Convert bytes to MiB, rounded to 1 decimal."""
    return round(size / _MIB, 1)


def _reset_peak_rss() -> bool:
    """Reset this process's peak-RSS counter; return whether that worked (Linux only)."""
    try:
        _PROC_CLEAR_REFS.write_text(_RESET_PEAK_RSS, encoding="ascii")
    except OSError:
        return False
    return True


def _vmhwm_bytes() -> int | None:
    """Read the peak RSS (``VmHWM``) from ``/proc/self/status``, or ``None``."""
    try:
        lines = _PROC_STATUS.read_text(encoding="ascii").splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("VmHWM:"):
            return int(line.split()[1]) * 1024  # reported in kB
    return None


def _ru_maxrss_bytes() -> int | None:
    """Return the process peak RSS from ``getrusage`` (kB on Linux, bytes on macOS)."""
    if resource is None:  # pragma: no cover - not a POSIX platform
        return None
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _peak_rss(was_reset: bool) -> tuple[int | None, MemoryMethod]:
    """Return the peak RSS since the reset (or of the process) and how it was measured."""
    if was_reset:
        value = _vmhwm_bytes()
        if value is not None:
            return value, MemoryMethod.STAGE_PEAK
    value = _ru_maxrss_bytes()
    if value is not None:
        return value, MemoryMethod.PROCESS_PEAK
    return None, MemoryMethod.UNAVAILABLE


@contextmanager
def measure() -> Iterator[Measurement]:
    """Measure the wall-clock duration and peak RSS of the enclosed block.

    Measurements must not be nested: an inner block resets the peak counter of the outer one.

    Yields:
        A :class:`Measurement`, filled in when the block exits (also on an exception).
    """
    result = Measurement()
    was_reset = _reset_peak_rss()
    start = time.perf_counter()
    try:
        yield result
    finally:
        result.duration_s = time.perf_counter() - start
        result.peak_rss_bytes, result.memory_method = _peak_rss(was_reset)


def stage_entry(
    stage: str,
    *,
    status: StageStatus,
    command: str,
    rows: Mapping[str, int] | None = None,
    details: Mapping[str, Any] | None = None,
    error: str | None = None,
    timing: Measurement | None = None,
) -> dict[str, Any]:
    """Describe one stage of a run for ``run_summary.json``.

    Args:
        stage: Stage name (``PipelineStage`` value).
        status: PASS, FAIL or NOT_RUN.
        command: The equivalent single-stage command line.
        rows: ``<layer>/<table>`` → rows written by the stage.
        details: Stage-specific results (verdicts, check totals, dataset hash, ...).
        error: The failure message, when the stage failed.
        timing: Its measurement (``None`` for a stage that did not run).

    Returns:
        A JSON-ready mapping; the wall-clock values sit under ``timing`` only.
    """
    return {
        "stage": stage,
        "status": status.value,
        "command": command,
        "rows": dict(sorted((rows or {}).items())),
        "details": dict(details or {}),
        "error": error,
        TIMING_KEY: None if timing is None else timing.as_dict(),
    }


def build_run_summary(
    *,
    command: str,
    scale: str,
    seed: int,
    git: Mapping[str, Any],
    stages: Sequence[Mapping[str, Any]],
    total_duration_s: float,
) -> dict[str, Any]:
    """Assemble ``run_summary.json`` for one ``run-all`` run.

    Args:
        command: The ``run-all`` command line.
        scale: Scale name.
        seed: The seed the seeded stages used.
        git: Repository state (``commit``, ``dirty``).
        stages: One :func:`stage_entry` per pipeline stage, in order (NOT_RUN ones included).
        total_duration_s: Wall-clock seconds of the whole run.

    Returns:
        The summary, with ``status`` (PASS only when every stage passed), ``exit_code``,
        ``failed_stage`` and ``error``, a ``timing`` block, and ``content_sha256`` over
        everything except the timing.
    """
    failed = next((s for s in stages if s["status"] == StageStatus.FAIL.value), None)
    passed = failed is None and all(s["status"] == StageStatus.PASS.value for s in stages)
    peaks = [s[TIMING_KEY]["peak_rss_mib"] for s in stages if s.get(TIMING_KEY)]
    methods = sorted({s[TIMING_KEY]["memory_method"] for s in stages if s.get(TIMING_KEY)})
    summary: dict[str, Any] = {
        "run_summary_version": RUN_SUMMARY_VERSION,
        "command": command,
        "scale": scale,
        "seed": seed,
        "package_version": __version__,
        "git": dict(git),
        "status": (StageStatus.PASS if passed else StageStatus.FAIL).value,
        "exit_code": 0 if passed else 1,
        "failed_stage": None if failed is None else failed["stage"],
        "error": None if failed is None else failed["error"],
        "stages": [dict(s) for s in stages],
        TIMING_KEY: {
            "total_duration_s": round(total_duration_s, 3),
            "peak_rss_mib": max((p for p in peaks if p is not None), default=None),
            "memory_method": methods,
            "note": "Wall-clock measurements; not covered by content_sha256.",
        },
    }
    summary[_CONTENT_KEY] = run_summary_content_sha256(summary)
    return summary


def run_summary_content_sha256(summary: Mapping[str, Any]) -> str:
    """Hash a run summary without its ``timing`` blocks and without this hash itself.

    Args:
        summary: A run summary (as built by :func:`build_run_summary` or read from disk).

    Returns:
        The hex sha256 of the sorted-key, compact JSON rendering of the deterministic content.
    """
    core = {k: v for k, v in summary.items() if k not in (TIMING_KEY, _CONTENT_KEY)}
    core["stages"] = [
        {k: v for k, v in stage.items() if k != TIMING_KEY} for stage in core.get("stages", [])
    ]
    text = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
