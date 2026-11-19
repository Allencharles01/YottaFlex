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
    PartyType,
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

DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
"""Root of the generated output (``output/<scale>/<layer>/``); git-ignored."""

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


class IntRange(_StrictModel):
    """Inclusive range of integers (``min`` may be negative, e.g. an early departure)."""

    min: int
    max: int

    @model_validator(mode="after")
    def _check_order(self) -> "IntRange":
        if self.max < self.min:
            raise ValueError(f"max ({self.max}) must be >= min ({self.min})")
        return self


class FloatRange(_StrictModel):
    """Inclusive range of floats."""

    min: float
    max: float

    @model_validator(mode="after")
    def _check_order(self) -> "FloatRange":
        if self.max < self.min:
            raise ValueError(f"max ({self.max}) must be >= min ({self.min})")
        return self


def _require_non_negative(field: str, value: IntRange | FloatRange) -> None:
    """Fail if a range has a negative lower bound."""
    if value.min < 0:
        raise ValueError(f"{field}.min must be >= 0, got {value.min}")


class LotGeneration(_StrictModel):
    """How lots are generated per product."""

    min_per_product: int = Field(ge=1)
    shipments_per_lot: float = Field(gt=0)
    manufacture_lead_days: int = Field(ge=0)
    on_hold_rate: float = Field(ge=0, lt=1)
    utilisation: FloatRange

    @model_validator(mode="after")
    def _check_utilisation(self) -> "LotGeneration":
        if not 0 < self.utilisation.min <= self.utilisation.max < 1:
            raise ValueError("utilisation must satisfy 0 < min <= max < 1")
        return self


class DeviceGeneration(_StrictModel):
    """How physical logger devices are generated and scheduled."""

    models: tuple[str, ...] = Field(min_length=1)
    uncertainty_c: FloatRange
    calibration_validity_days: int = Field(ge=1)
    calibrated_before_window_days: IntRange
    turnaround_min: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_ranges(self) -> "DeviceGeneration":
        if not 0 < self.uncertainty_c.min <= self.uncertainty_c.max <= 1:
            raise ValueError("uncertainty_c must satisfy 0 < min <= max <= 1")
        _require_non_negative("calibrated_before_window_days", self.calibrated_before_window_days)
        if any(not m.strip() or len(m) > 40 for m in self.models):
            raise ValueError("device models must be non-empty and at most 40 characters")
        return self


class RouteGeneration(_StrictModel):
    """How routes (stops, dwell and drive times) are generated."""

    intermediate_stops: IntRange
    cross_dock_share: float = Field(ge=0, le=1)
    hub_detour_jitter: float = Field(ge=0, lt=1)
    dwell_min: IntRange
    planned_dwell_min: int = Field(ge=0)
    road_factor: float = Field(ge=1)
    average_speed_kmh: float = Field(gt=0)
    drive_time_factor: FloatRange

    @model_validator(mode="after")
    def _check_ranges(self) -> "RouteGeneration":
        _require_non_negative("intermediate_stops", self.intermediate_stops)
        if self.intermediate_stops.max > 97:  # route-stop sequences are 2 digits (1..99)
            raise ValueError("intermediate_stops.max must be <= 97")
        if self.dwell_min.min < 1:  # the two handoffs at a stop must be strictly ordered
            raise ValueError("dwell_min.min must be >= 1")
        if self.drive_time_factor.min <= 0:
            raise ValueError("drive_time_factor.min must be > 0")
        return self


class ScenarioGeneration(_StrictModel):
    """How the natural scenarios shape a shipment (Phase 3)."""

    long_dwell_min: IntRange
    breach_extra_readings: IntRange
    abs_breach_excess_c: FloatRange
    at_limit_readings: IntRange
    ramp_readings: IntRange
    separation_readings: int = Field(ge=1)

    @model_validator(mode="after")
    def _check_ranges(self) -> "ScenarioGeneration":
        if self.breach_extra_readings.min < 1:
            raise ValueError("breach_extra_readings.min must be >= 1")
        if self.at_limit_readings.min < 1:
            raise ValueError("at_limit_readings.min must be >= 1")
        if self.abs_breach_excess_c.min < 0.01:
            raise ValueError("abs_breach_excess_c.min must be >= 0.01 (beyond the limit at 2 dp)")
        if self.ramp_readings.min < 1:
            raise ValueError("ramp_readings.min must be >= 1")
        if self.separation_readings < 2 * self.ramp_readings.max:
            raise ValueError(
                "separation_readings must be >= 2 x ramp_readings.max so ramps never overlap"
            )
        return self


class ReadingGeneration(_StrictModel):
    """Parameters of the temperature model (Phase 3)."""

    noise_sd_c: float = Field(ge=0)
    drift_amplitude_c: FloatRange
    drift_period_hours: FloatRange
    dwell_warming_c_per_hour: float = Field(ge=0)
    max_dwell_warming_c: float = Field(ge=0)
    dwell_recovery_min: float = Field(gt=0)
    limit_margin_c: float = Field(ge=0.01)

    @model_validator(mode="after")
    def _check_ranges(self) -> "ReadingGeneration":
        _require_non_negative("drift_amplitude_c", self.drift_amplitude_c)
        if self.drift_period_hours.min <= 0:
            raise ValueError("drift_period_hours.min must be > 0")
        return self


class GenerationConfig(_StrictModel):
    """Knobs of the data generator (master data: Phase 2; events and scenarios: Phase 3)."""

    parties: dict[PartyType, int]
    party_name_labels: dict[PartyType, str]
    party_active_lead_days: IntRange
    lots: LotGeneration
    lots_per_shipment: IntRange
    devices: DeviceGeneration
    routes: RouteGeneration
    departure_delay_min: IntRange
    logger_lead_min: IntRange
    logger_tail_min: IntRange
    planning_lead_hours: IntRange
    scenarios: ScenarioGeneration
    readings: ReadingGeneration

    @model_validator(mode="after")
    def _check_invariants(self) -> "GenerationConfig":
        _require_all_keys("parties", self.parties, PartyType)
        _require_all_keys("party_name_labels", self.party_name_labels, PartyType)
        if any(count < 1 for count in self.parties.values()):
            raise ValueError("parties: every party type needs at least 1 party")
        if sum(self.parties.values()) > 9999:  # party IDs have 4 digits
            raise ValueError("parties: at most 9999 parties in total")
        labels = list(self.party_name_labels.values())
        if any(not label.strip() for label in labels) or len(set(labels)) != len(labels):
            raise ValueError("party_name_labels must be non-empty and distinct")
        _require_non_negative("party_active_lead_days", self.party_active_lead_days)
        if self.lots_per_shipment.min < 1:
            raise ValueError("lots_per_shipment.min must be >= 1")
        _require_non_negative("logger_lead_min", self.logger_lead_min)
        _require_non_negative("logger_tail_min", self.logger_tail_min)
        if self.planning_lead_hours.min * 60 < max(0, -self.departure_delay_min.min):
            raise ValueError(
                "planning_lead_hours.min must cover the earliest departure "
                "(PLANNED must not come after PICKED_UP)"
            )
        if self.planning_lead_hours.max > self.party_active_lead_days.min * 24:
            raise ValueError(
                "planning_lead_hours.max must be <= party_active_lead_days.min x 24 "
                "(planners are active before they plan)"
            )
        return self


class InjectionConfig(_StrictModel):
    """How the Phase 5 injectors shape an anomaly (``coldchain inject``)."""

    missing_extra_readings: IntRange

    @model_validator(mode="after")
    def _check_ranges(self) -> "InjectionConfig":
        _require_non_negative("missing_extra_readings", self.missing_extra_readings)
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
    generation: GenerationConfig
    injection: InjectionConfig

    @model_validator(mode="after")
    def _check_invariants(self) -> "AppConfig":
        _require_all_keys("scales", self.scales, Scale)
        _require_all_keys("scenario_weights", self.scenario_weights, Scenario)
        _require_all_keys("injection_rates", self.injection_rates, IssueType)
        if self.generation.routes.dwell_min.max > self.long_dwell_threshold_min:
            raise ValueError(
                "generation.routes.dwell_min.max must be <= long_dwell_threshold_min "
                "(baseline stops never trigger LONG_DWELL)"
            )
        if self.generation.scenarios.long_dwell_min.min <= self.long_dwell_threshold_min:
            raise ValueError(
                "generation.scenarios.long_dwell_min.min must be > long_dwell_threshold_min "
                "(a LONG_DWELL stop must trigger the warning)"
            )
        if (
            self.scenario_weights[Scenario.LONG_DWELL] > 0
            and self.generation.routes.intermediate_stops.max < 1
        ):
            raise ValueError(
                "LONG_DWELL needs an intermediate stop: set "
                "generation.routes.intermediate_stops.max >= 1 or its scenario weight to 0"
            )
        if self.generation.devices.turnaround_min < self.reading_interval_min:
            raise ValueError(
                "generation.devices.turnaround_min must be >= reading_interval_min "
                "(a device's assignment windows must not share a reading instant)"
            )

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
