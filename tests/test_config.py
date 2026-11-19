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
