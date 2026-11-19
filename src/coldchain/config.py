"""Load and validate the pipeline configuration (``config/default.yaml``).

The YAML is parsed into typed pydantic models. Any problem (missing file, bad YAML, unknown key,
wrong type, broken invariant) is raised as :class:`coldchain.errors.ConfigError` with the file
path and the offending field.
"""

import math
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldchain.enums import IssueType, Scale, Scenario
from coldchain.errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""Repository root (``src/coldchain/config.py`` → two levels up)."""

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
"""Path of the default configuration file."""

_WEIGHT_TOLERANCE = 1e-6


class _StrictModel(BaseModel):
    """Base model: immutable and rejects unknown keys (catches YAML typos)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScaleConfig(_StrictModel):
    """Volumes for one scale preset."""

    shipments: int = Field(gt=0)
    devices: int = Field(gt=0)


class DateWindow(_StrictModel):
    """Inclusive window of dates in which shipments depart."""

    start: date
    end: date

    @model_validator(mode="after")
    def _check_order(self) -> "DateWindow":
        if self.end <= self.start:
            raise ValueError(f"end ({self.end}) must be after start ({self.start})")
        return self


class TripDurationHours(_StrictModel):
    """Range of trip durations, in hours."""

    min: float = Field(gt=0)
    max: float = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> "TripDurationHours":
        if self.max < self.min:
            raise ValueError(f"max ({self.max}) must be >= min ({self.min})")
        return self


class AppConfig(_StrictModel):
    """Validated top-level configuration."""

    seed: int = Field(ge=0)
    scales: dict[Scale, ScaleConfig]
    reading_interval_min: int = Field(gt=0)
    date_window: DateWindow
    trip_duration_hours: TripDurationHours
    scenario_weights: dict[Scenario, float]
    injection_rates: dict[IssueType, float]
    long_dwell_threshold_min: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_invariants(self) -> "AppConfig":
        _require_all_keys("scales", self.scales, Scale)
        _require_all_keys("scenario_weights", self.scenario_weights, Scenario)
        _require_all_keys("injection_rates", self.injection_rates, IssueType)

        if any(w < 0 for w in self.scenario_weights.values()):
            raise ValueError("scenario_weights must all be >= 0")
        total = sum(self.scenario_weights.values())
        if not math.isclose(total, 1.0, abs_tol=_WEIGHT_TOLERANCE):
            raise ValueError(f"scenario_weights must sum to 1.0, got {total:.6f}")

        if any(not 0.0 <= r <= 1.0 for r in self.injection_rates.values()):
            raise ValueError("injection_rates must each be between 0 and 1")
        if sum(self.injection_rates.values()) > 1.0 + _WEIGHT_TOLERANCE:
            raise ValueError("injection_rates must sum to <= 1 (injections use disjoint shipments)")
        return self

    def scale(self, name: Scale | str) -> ScaleConfig:
        """Return the volumes for a scale preset.

        Args:
            name: Scale name, e.g. ``"starter"``.

        Returns:
            The matching :class:`ScaleConfig`.

        Raises:
            ConfigError: If the scale name is unknown.
        """
        try:
            return self.scales[Scale(name)]
        except ValueError as exc:
            allowed = ", ".join(s.value for s in Scale)
            raise ConfigError(f"Unknown scale {name!r}; expected one of: {allowed}") from exc


def _require_all_keys(field: str, mapping: dict[Any, Any], enum_type: type) -> None:
    """Fail if ``mapping`` does not define a value for every member of ``enum_type``."""
    missing = [m.value for m in enum_type if m not in mapping]
    if missing:
        raise ValueError(f"{field} is missing keys: {', '.join(missing)}")


def parse_config(data: Any, source: str = "<dict>") -> AppConfig:
    """Validate an already-parsed config mapping.

    Args:
        data: Mapping produced by ``yaml.safe_load`` (or built in code/tests).
        source: Label used in error messages (usually the file path).

    Returns:
        The validated :class:`AppConfig`.

    Raises:
        ConfigError: If the data is not a mapping or fails validation.
    """
    if not isinstance(data, dict):
        raise ConfigError(f"{source}: top level must be a mapping, got {type(data).__name__}")
    try:
        return AppConfig.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise ConfigError(f"Invalid config in {source}: {problems}") from exc


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load and validate a YAML config file.

    Args:
        path: Path to the YAML file. Defaults to :data:`DEFAULT_CONFIG_PATH`.

    Returns:
        The validated :class:`AppConfig`.

    Raises:
        ConfigError: If the file is missing, is not valid YAML, or fails validation.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.is_file():
        raise ConfigError(f"Config file not found: {config_path}")
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML in {config_path}: {exc}") from exc
    return parse_config(data, source=str(config_path))
