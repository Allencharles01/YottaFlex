import logging
from pathlib import Path

import numpy as np
import pytest
from typer.testing import CliRunner

from coldchain import observability
from coldchain.cli import app
from coldchain.enums import LogLevel, StageStatus
from coldchain.observability import (
    Measurement,
    MemoryMethod,
    build_run_summary,
    measure,
    run_summary_content_sha256,
    stage_entry,
)

runner = CliRunner()

MIB = 1 << 20


# --------------------------------------------------------------------------------------------
# measure()
# --------------------------------------------------------------------------------------------


def test_measure_records_duration_and_peak_memory() -> None:
    with measure() as timing:
        sum(range(10_000))
    assert timing.duration_s > 0
    assert timing.peak_rss_bytes is not None and timing.peak_rss_bytes > 0
    assert timing.memory_method in MemoryMethod


def test_the_peak_is_per_block_when_the_counter_can_be_reset() -> None:
    with measure() as big:
        block = np.ones(64 * MIB // 8)  # 64 MiB, touched
        block.sum()
        del block
    with measure() as small:
        sum(range(1000))
    if big.memory_method is not MemoryMethod.STAGE_PEAK:
        pytest.skip("this platform cannot reset the peak RSS counter")
    assert big.peak_rss_bytes - small.peak_rss_bytes >= 48 * MIB


def test_measure_falls_back_to_the_process_peak(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(observability, "_PROC_CLEAR_REFS", tmp_path / "missing" / "clear_refs")
    with measure() as timing:
        pass
    assert timing.memory_method in (MemoryMethod.PROCESS_PEAK, MemoryMethod.UNAVAILABLE)


def test_measure_fills_in_the_result_when_the_block_raises() -> None:
    with pytest.raises(RuntimeError), measure() as timing:
        raise RuntimeError("boom")
    assert timing.duration_s > 0


def test_measurement_rounds_and_describes() -> None:
    timing = Measurement(1.23456, 300 * MIB + 12345, MemoryMethod.STAGE_PEAK)
    assert timing.as_dict() == {
        "duration_s": 1.235,
        "peak_rss_mib": 300.0,
        "memory_method": "vmhwm_reset_per_stage",
    }
    assert timing.describe() == "1.23 s, peak RSS 300.0 MiB"
    assert Measurement().describe() == "0.00 s, peak RSS n/a"
    assert Measurement().as_dict()["peak_rss_mib"] is None


# --------------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("level", list(LogLevel))
def test_configure_logging_sets_the_root_level(level: LogLevel) -> None:
    observability.configure_logging(level)
    assert logging.getLogger().level == getattr(logging, level.value)
    observability.configure_logging(LogLevel.INFO)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ([], logging.INFO),
        (["--log-level", "WARNING"], logging.WARNING),
        (["--log-level", "DEBUG"], logging.DEBUG),
        (["-v"], logging.DEBUG),
        (["--log-level", "ERROR", "--verbose"], logging.DEBUG),
    ],
)
def test_log_level_option(args: list[str], expected: int) -> None:
    result = runner.invoke(app, [*args, "version"])
    assert result.exit_code == 0, result.output
    assert logging.getLogger().level == expected
    observability.configure_logging(LogLevel.INFO)


def test_an_unknown_log_level_is_rejected() -> None:
    assert runner.invoke(app, ["--log-level", "LOUD", "version"]).exit_code != 0


def test_a_failing_stage_logs_its_duration_and_memory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["check", "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Stage check FAILED in " in result.output
    assert "peak RSS" in result.output


def test_the_log_level_hides_the_stage_timing(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--log-level", "ERROR", "check", "--output-dir", str(tmp_path)])
    assert result.exit_code == 1
    assert "Stage check" not in result.output
    assert "Table file not found" in result.output


# --------------------------------------------------------------------------------------------
# Run summary
# --------------------------------------------------------------------------------------------


def _stage(name: str, status: StageStatus, seconds: float = 1.0, **kwargs) -> dict:
    timing = None if status is StageStatus.NOT_RUN else Measurement(seconds, 100 * MIB)
    return stage_entry(name, status=status, command=f"coldchain {name}", timing=timing, **kwargs)


def _summary(stages: list[dict], total: float = 3.0) -> dict:
    return build_run_summary(
        command="coldchain run-all --scale starter",
        scale="starter",
        seed=20261022,
        git={"commit": "abc", "dirty": False},
        stages=stages,
        total_duration_s=total,
    )


def test_a_run_where_every_stage_passed() -> None:
    summary = _summary(
        [
            _stage("generate", StageStatus.PASS, rows={"raw/shipments": 50}),
            _stage("check", StageStatus.PASS, details={"statuses": {"COMPLIANT": 39}}),
        ]
    )
    assert (summary["status"], summary["exit_code"]) == ("PASS", 0)
    assert summary["failed_stage"] is None and summary["error"] is None
    assert summary["stages"][0]["rows"] == {"raw/shipments": 50}
    assert summary["timing"]["total_duration_s"] == 3.0
    assert summary["timing"]["peak_rss_mib"] == 100.0


def test_a_failed_run_names_the_stage_and_its_error() -> None:
    summary = _summary(
        [
            _stage("generate", StageStatus.PASS),
            _stage("inject", StageStatus.FAIL, error="Too few eligible shipments"),
            _stage("check", StageStatus.NOT_RUN),
        ]
    )
    assert (summary["status"], summary["exit_code"]) == ("FAIL", 1)
    assert summary["failed_stage"] == "inject"
    assert summary["error"] == "Too few eligible shipments"
    assert summary["stages"][2]["timing"] is None


def test_an_empty_run_is_not_a_pass() -> None:
    assert _summary([_stage("generate", StageStatus.NOT_RUN)])["exit_code"] == 1


def test_the_content_hash_ignores_only_the_timing() -> None:
    fast = _summary([_stage("generate", StageStatus.PASS, seconds=1.0)], total=1.0)
    slow = _summary([_stage("generate", StageStatus.PASS, seconds=9.0)], total=9.0)
    assert fast["timing"] != slow["timing"]
    assert fast["content_sha256"] == slow["content_sha256"]
    assert run_summary_content_sha256(fast) == fast["content_sha256"]
    other = _summary([_stage("generate", StageStatus.PASS, rows={"raw/shipments": 51})])
    assert other["content_sha256"] != fast["content_sha256"]
    edited = {**fast, "seed": 7}
    assert run_summary_content_sha256(edited) != fast["content_sha256"]
