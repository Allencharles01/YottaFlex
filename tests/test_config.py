from datetime import date
from pathlib import Path
from typing import Any

import pytest
import yaml

from coldchain.config import DEFAULT_CONFIG_PATH, AppConfig, load_config, parse_config
from coldchain.enums import IssueType, Scale, Scenario
from coldchain.errors import ConfigError


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "cfg.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_default_config_loads_and_has_expected_values() -> None:
    cfg = load_config()
    assert isinstance(cfg, AppConfig)
    assert cfg.seed == 20261022
    assert cfg.scale("starter").shipments == 50
    assert cfg.scale(Scale.STARTER).devices == 20
    assert cfg.scale("full").shipments == 5000
    assert cfg.scale("full").devices == 1000
    assert cfg.reading_interval_min == 10
    assert cfg.date_window.start == date(2026, 1, 1)
    assert cfg.date_window.end == date(2026, 10, 31)
    assert cfg.trip_duration_hours.min == 4
    assert cfg.trip_duration_hours.max == 72
    assert cfg.long_dwell_threshold_min == 120
    assert set(cfg.scenario_weights) == set(Scenario)
    assert cfg.scenario_weights[Scenario.NORMAL] == pytest.approx(0.80)
    assert set(cfg.injection_rates) == set(IssueType)
    assert all(r == pytest.approx(0.01) for r in cfg.injection_rates.values())


def test_load_config_from_explicit_path(tmp_path: Path, raw_config: dict) -> None:
    raw_config["seed"] = 7
    assert load_config(_write(tmp_path, raw_config)).seed == 7


def test_config_is_immutable() -> None:
    cfg = load_config(DEFAULT_CONFIG_PATH)
    with pytest.raises(Exception):  # noqa: B017 - pydantic raises ValidationError on frozen
        cfg.seed = 1  # type: ignore[misc]


def test_missing_file_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_raises_config_error(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("seed: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not parse YAML"):
        load_config(path)


def test_non_mapping_top_level_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_config(_write(tmp_path, [1, 2, 3]))


def _mutations() -> list[tuple[str, Any, str]]:
    """(description, mutator, expected message fragment)."""

    def drop(key: str):
        return lambda d: d.pop(key)

    def setv(path: list[str], value: Any):
        def _apply(d: dict) -> None:
            node = d
            for p in path[:-1]:
                node = node[p]
            node[path[-1]] = value

        return _apply

    return [
        ("missing seed", drop("seed"), "seed"),
        ("negative seed", setv(["seed"], -1), "seed"),
        ("seed wrong type", setv(["seed"], "abc"), "seed"),
        ("unknown top-level key", setv(["sede"], 1), "sede"),
        ("zero shipments", setv(["scales", "starter", "shipments"], 0), "shipments"),
        ("unknown scale", setv(["scales", "huge"], {"shipments": 1, "devices": 1}), "huge"),
        ("missing scale", lambda d: d["scales"].pop("full"), "missing keys: full"),
        ("zero interval", setv(["reading_interval_min"], 0), "reading_interval_min"),
        (
            "end before start",
            setv(["date_window"], {"start": "2026-05-01", "end": "2026-04-01"}),
            "must be after start",
        ),
        ("trip max < min", setv(["trip_duration_hours"], {"min": 10, "max": 5}), "must be >= min"),
        ("weights do not sum to 1", setv(["scenario_weights", "NORMAL"], 0.5), "sum to 1.0"),
        ("unknown scenario", setv(["scenario_weights", "ALIENS"], 0.0), "ALIENS"),
        (
            "missing scenario",
            lambda d: d["scenario_weights"].pop("BOUNDARY"),
            "missing keys: BOUNDARY",
        ),
        (
            "rate above 1",
            setv(["injection_rates", "logger_not_assigned"], 1.5),
            "between 0 and 1",
        ),
        (
            "missing issue type",
            lambda d: d["injection_rates"].pop("custody_gap_not_flagged"),
            "custody_gap_not_flagged",
        ),
        ("zero dwell threshold", setv(["long_dwell_threshold_min"], 0), "long_dwell"),
        ("missing generation", drop("generation"), "generation"),
        (
            "baseline dwell above long-dwell threshold",
            setv(["generation", "routes", "dwell_min"], {"min": 20, "max": 121}),
            "dwell_min.max must be <= long_dwell_threshold_min",
        ),
        (
            "turnaround shorter than reading interval",
            setv(["generation", "devices", "turnaround_min"], 5),
            "turnaround_min must be >= reading_interval_min",
        ),
        (
            "missing party type",
            lambda d: d["generation"]["parties"].pop("PLANNER"),
            "missing keys: PLANNER",
        ),
        ("zero parties of a type", setv(["generation", "parties", "CARRIER"], 0), "at least 1"),
        (
            "duplicate party labels",
            setv(["generation", "party_name_labels", "RECEIVER"], "Pharma"),
            "distinct",
        ),
        (
            "utilisation of 1",
            setv(["generation", "lots", "utilisation"], {"min": 0.5, "max": 1.0}),
            "utilisation",
        ),
        (
            "range max < min",
            setv(["generation", "lots_per_shipment"], {"min": 3, "max": 1}),
            "must be >= min",
        ),
        ("zero lots per shipment", setv(["generation", "lots_per_shipment", "min"], 0), ">= 1"),
        (
            "uncertainty above 1",
            setv(["generation", "devices", "uncertainty_c"], {"min": 0.2, "max": 1.5}),
            "uncertainty_c",
        ),
        ("no device models", setv(["generation", "devices", "models"], []), "models"),
        (
            "negative calibration offset",
            setv(["generation", "devices", "calibrated_before_window_days"], {"min": -1, "max": 5}),
            "calibrated_before_window_days.min",
        ),
        (
            "too many intermediate stops",
            setv(["generation", "routes", "intermediate_stops"], {"min": 0, "max": 98}),
            "<= 97",
        ),
        (
            "negative logger lead",
            setv(["generation", "logger_lead_min"], {"min": -5, "max": 5}),
            "logger_lead_min.min",
        ),
        (
            "unknown generation key",
            setv(["generation", "lots", "colour"], "blue"),
            "colour",
        ),
        # Phase 3: scenarios, readings and events
        (
            "zero baseline dwell",
            setv(["generation", "routes", "dwell_min"], {"min": 0, "max": 120}),
            "dwell_min.min must be >= 1",
        ),
        (
            "long dwell not above threshold",
            setv(["generation", "scenarios", "long_dwell_min"], {"min": 120, "max": 300}),
            "long_dwell_min.min must be > long_dwell_threshold_min",
        ),
        (
            "long dwell without intermediate stops",
            setv(["generation", "routes", "intermediate_stops"], {"min": 0, "max": 0}),
            "LONG_DWELL needs an intermediate stop",
        ),
        (
            "breach without extra readings",
            setv(["generation", "scenarios", "breach_extra_readings"], {"min": 0, "max": 2}),
            "breach_extra_readings.min",
        ),
        (
            "no at-limit readings",
            setv(["generation", "scenarios", "at_limit_readings"], {"min": 0, "max": 2}),
            "at_limit_readings.min",
        ),
        (
            "absolute excess below 2 dp",
            setv(["generation", "scenarios", "abs_breach_excess_c"], {"min": 0.0, "max": 1.0}),
            "abs_breach_excess_c.min",
        ),
        ("zero separation", setv(["generation", "scenarios", "separation_readings"], 0), "separ"),
        (
            "no ramp readings",
            setv(["generation", "scenarios", "ramp_readings"], {"min": 0, "max": 2}),
            "ramp_readings.min",
        ),
        (
            "separation shorter than two ramps",
            setv(["generation", "scenarios", "separation_readings"], 7),
            "ramps never overlap",
        ),
        ("negative noise", setv(["generation", "readings", "noise_sd_c"], -0.1), "noise_sd_c"),
        (
            "negative drift amplitude",
            setv(["generation", "readings", "drift_amplitude_c"], {"min": -1.0, "max": 0.5}),
            "drift_amplitude_c.min",
        ),
        (
            "zero drift period",
            setv(["generation", "readings", "drift_period_hours"], {"min": 0, "max": 5}),
            "drift_period_hours.min",
        ),
        ("margin below 2 dp", setv(["generation", "readings", "limit_margin_c"], 0.0), "margin"),
        (
            "planning lead shorter than earliest departure",
            setv(["generation", "planning_lead_hours"], {"min": 0, "max": 10}),
            "planning_lead_hours.min",
        ),
        (
            "planning lead longer than party activity",
            setv(["generation", "planning_lead_hours"], {"min": 24, "max": 100000}),
            "planning_lead_hours.max",
        ),
        ("missing injection", drop("injection"), "injection"),
        (
            "negative extra missing readings",
            setv(["injection", "missing_extra_readings"], {"min": -1, "max": 2}),
            "missing_extra_readings.min must be >= 0",
        ),
        (
            "extra missing readings max < min",
            setv(["injection", "missing_extra_readings"], {"min": 3, "max": 2}),
            "must be >= min",
        ),
        ("unknown injection key", setv(["injection", "typo"], 1), "typo"),
    ]


@pytest.mark.parametrize(("label", "mutate", "fragment"), _mutations(), ids=lambda x: str(x))
def test_invalid_config_raises_clear_error(
    tmp_path: Path, raw_config: dict, label: str, mutate: Any, fragment: str
) -> None:
    mutate(raw_config)
    path = _write(tmp_path, raw_config)
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    message = str(excinfo.value)
    assert str(path) in message, label
    assert fragment in message, f"{label}: {message}"


def test_injection_rates_sum_above_one_rejected(raw_config: dict) -> None:
    raw_config["injection_rates"] = {k: 0.3 for k in raw_config["injection_rates"]}
    with pytest.raises(ConfigError, match="sum to <= 1"):
        parse_config(raw_config)


def test_negative_weight_rejected(raw_config: dict) -> None:
    raw_config["scenario_weights"]["NORMAL"] = 0.9
    raw_config["scenario_weights"]["BOUNDARY"] = -0.07
    with pytest.raises(ConfigError, match=">= 0"):
        parse_config(raw_config)


def test_unknown_scale_lookup_raises() -> None:
    with pytest.raises(ConfigError, match="Unknown scale 'huge'"):
        load_config().scale("huge")
