"""Load and validate configuration: ``config/default.yaml`` and ``config/reference/*.yaml``.

The YAML is parsed into typed pydantic models. Any problem (missing file, bad YAML, unknown key,
wrong type, broken invariant) is raised as :class:`coldchain.errors.ConfigError` with the file
path and the offending field.
"""

import math
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from coldchain.enums import (
    IssueType,
    PackagingType,
    PolicyCode,
    ProductCategory,
    Scale,
    Scenario,
)
from coldchain.errors import ConfigError
from coldchain.schemas import overlapping_policy_versions

PROJECT_ROOT = Path(__file__).resolve().parents[2]
"""Repository root (``src/coldchain/config.py`` → two levels up)."""

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"
"""Path of the default configuration file."""

DEFAULT_REFERENCE_DIR = PROJECT_ROOT / "config" / "reference"
"""Directory holding the reference-data YAML files."""

REFERENCE_FILES: dict[str, str] = {
    "policies": "threshold_policies.yaml",
    "products": "products.yaml",
    "packaging": "packaging.yaml",
    "locations": "locations.yaml",
}
"""Top-level key of each reference file → its file name in the reference directory."""

INDIA_LATITUDE = (6.0, 38.0)
"""Latitude bounds (degrees) accepted for a location."""

INDIA_LONGITUDE = (68.0, 98.0)
"""Longitude bounds (degrees) accepted for a location."""

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


class PolicySpec(_StrictModel):
    """One version of a threshold policy (a row of ``threshold_policies.yaml``)."""

    policy_code: PolicyCode
    version: int = Field(ge=1)
    min_c: float
    max_c: float
    abs_min_c: float
    abs_max_c: float
    max_single_excursion_min: int = Field(gt=0)
    max_cumulative_excursion_min: int = Field(gt=0)
    max_reading_gap_min: int = Field(gt=0)
    min_coverage_pct: float = Field(gt=0, le=100)
    effective_from: date
    effective_to: date | None

    @model_validator(mode="after")
    def _check_limits(self) -> "PolicySpec":
        if not self.abs_min_c < self.min_c < self.max_c < self.abs_max_c:
            raise ValueError("limits must satisfy abs_min_c < min_c < max_c < abs_max_c")
        if self.max_cumulative_excursion_min < self.max_single_excursion_min:
            raise ValueError("max_cumulative_excursion_min must be >= max_single_excursion_min")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be after effective_from")
        return self


class LotSizeRange(_StrictModel):
    """Inclusive range of units in one generated lot."""

    min: int = Field(gt=0)
    max: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> "LotSizeRange":
        if self.max < self.min:
            raise ValueError(f"max ({self.max}) must be >= min ({self.min})")
        return self


class ProductTemplate(_StrictModel):
    """A product catalogue entry (a row of ``products.yaml``)."""

    name: str = Field(min_length=1, max_length=80)
    category: ProductCategory
    policy_code: PolicyCode
    shelf_life_days: int = Field(gt=0)
    lot_size_units: LotSizeRange


class PackagingProfile(_StrictModel):
    """A packaging profile (a row of ``packaging.yaml``)."""

    name: str = Field(min_length=1, max_length=80)
    packaging_type: PackagingType
    supported_policy_code: PolicyCode
    setpoint_c: float
    max_hold_hours: float = Field(gt=0)
    thermal_response: float = Field(ge=0, le=1)


class Location(_StrictModel):
    """A city that can host parties (a row of ``locations.yaml``)."""

    city: str = Field(min_length=1, max_length=40)
    state: str = Field(min_length=1)
    latitude: float = Field(ge=INDIA_LATITUDE[0], le=INDIA_LATITUDE[1])
    longitude: float = Field(ge=INDIA_LONGITUDE[0], le=INDIA_LONGITUDE[1])
    is_hub: bool
    is_manufacturing_cluster: bool


class ReferenceData(_StrictModel):
    """All reference data, validated within and across files."""

    policies: tuple[PolicySpec, ...] = Field(min_length=1)
    products: tuple[ProductTemplate, ...] = Field(min_length=1)
    packaging: tuple[PackagingProfile, ...] = Field(min_length=1)
    locations: tuple[Location, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_cross_file(self) -> "ReferenceData":
        _require_unique(
            "policies (policy_code, version)", [(p.policy_code, p.version) for p in self.policies]
        )
        _require_unique("product names", [p.name for p in self.products])
        _require_unique("packaging names", [p.name for p in self.packaging])
        _require_unique("location cities", [loc.city for loc in self.locations])

        windows = pd.DataFrame(
            [
                {
                    "policy_code": p.policy_code.value,
                    "effective_from": p.effective_from,
                    "effective_to": p.effective_to,
                }
                for p in self.policies
            ]
        )
        overlapping = windows.loc[overlapping_policy_versions(windows), "policy_code"]
        if not overlapping.empty:
            raise ValueError(f"policy versions overlap in time for: {sorted(set(overlapping))}")

        missing = sorted(set(PolicyCode) - {p.policy_code for p in self.policies})
        if missing:
            raise ValueError(f"no policy defined for codes: {missing}")
        unpackaged = sorted(
            {p.policy_code for p in self.products}
            - {p.supported_policy_code for p in self.packaging}
        )
        if unpackaged:
            raise ValueError(f"no packaging supports product policy codes: {unpackaged}")
        for profile in self.packaging:
            for policy in self.policies:
                same_code = policy.policy_code == profile.supported_policy_code
                if same_code and not policy.min_c <= profile.setpoint_c <= policy.max_c:
                    raise ValueError(
                        f"packaging {profile.name!r} setpoint {profile.setpoint_c} is outside "
                        f"{policy.policy_code} V{policy.version} band "
                        f"[{policy.min_c}, {policy.max_c}]"
                    )
        if not any(loc.is_hub for loc in self.locations):
            raise ValueError("at least one location must be a hub")
        if not any(loc.is_manufacturing_cluster for loc in self.locations):
            raise ValueError("at least one location must be a manufacturing cluster")
        return self


def _require_unique(label: str, values: list[Any]) -> None:
    """Fail if ``values`` contains duplicates."""
    duplicates = sorted({str(v) for v in values if values.count(v) > 1})
    if duplicates:
        raise ValueError(f"duplicate {label}: {', '.join(duplicates)}")


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
        raise ConfigError(f"Invalid config in {source}: {_format_errors(exc)}") from exc


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
    return parse_config(_read_yaml(config_path), source=str(config_path))


def _read_yaml(path: Path) -> Any:
    """Read and parse one YAML file.

    Raises:
        ConfigError: If the file is missing or is not valid YAML.
    """
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse YAML in {path}: {exc}") from exc


def _format_errors(exc: ValidationError) -> str:
    """Render pydantic errors as ``field.path: message`` pairs."""
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}" for err in exc.errors()
    )


def load_reference(directory: Path | str | None = None) -> ReferenceData:
    """Load and validate the reference-data files (policies, products, packaging, locations).

    Args:
        directory: Directory containing the files in :data:`REFERENCE_FILES`. Defaults to
            :data:`DEFAULT_REFERENCE_DIR`.

    Returns:
        The validated :class:`ReferenceData`.

    Raises:
        ConfigError: If a file is missing or malformed, or any rule (within or across files)
            fails.
    """
    ref_dir = Path(directory) if directory is not None else DEFAULT_REFERENCE_DIR
    sections: dict[str, Any] = {}
    for key, file_name in REFERENCE_FILES.items():
        path = ref_dir / file_name
        data = _read_yaml(path)
        if not isinstance(data, dict) or set(data) != {key}:
            raise ConfigError(f"{path}: expected a mapping with the single top-level key {key!r}")
        sections[key] = data[key]
    try:
        return ReferenceData.model_validate(sections)
    except ValidationError as exc:
        raise ConfigError(f"Invalid reference data in {ref_dir}: {_format_errors(exc)}") from exc
