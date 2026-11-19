"""Temperature readings, generated fully vectorised (no Python loop over readings).

Every logger assignment records one reading every ``reading_interval_min`` from ``assigned_at``
to ``unassigned_at`` inclusive. The temperature model (PROJECT_PLAN.md §7.1):

``natural = setpoint + device offset + drift + dwell warming + noise``

* **setpoint**: the packaging's ``setpoint_c``;
* **device offset**: one systematic offset per device, drawn uniformly in ±``uncertainty_c``
  and rounded to 2 decimals; it is hidden from the raw layer and written only to
  ``ground_truth/device_truth`` (:func:`device_truth`, DECISIONS.md #54);
* **drift**: a slow sinusoid per shipment (amplitude and period from config, random phase);
* **dwell warming**: at an intermediate stop the load warms at ``dwell_warming_c_per_hour`` x
  the packaging's ``thermal_response`` (capped at ``max_dwell_warming_c``), then recovers
  exponentially after departure (time constant ``dwell_recovery_min``);
* **noise**: Gaussian, ``noise_sd_c``.

Natural readings are clipped to the *safe band* ``[min_c + u + margin, max_c - u - margin]``
of the applied policy (``u`` = the device's ``uncertainty_c``), so they can never be out of
range or near a limit; the share of clipped natural readings is logged (DECISIONS.md #53).
Only the designed segments (:mod:`coldchain.generate.scenarios`) leave that band, and each
one is shaped like a real excursion (DECISIONS.md #52):

* **ramps**: the ``ramp_before`` readings before a segment move linearly from their natural
  value toward the safe-band edge on the segment's side, and the ``ramp_after`` readings move
  back; ramp readings stay inside the safe band, so they never trigger a rule;
* **plateau**: an excursion reading lies in ``[max_c + u + margin, abs_max_c - margin]``
  (HIGH) or ``[abs_min_c + margin, min_c - u - margin]`` (LOW): out of range, not near the
  limit, not an absolute breach. Within a segment the readings follow a parabolic hump from
  just past the range limit up to the segment's peak level (at the absolute-breach reading
  if there is one, else mid-segment), plus the reading's noise;
* the absolute-breach reading lies ``abs_breach_excess_c`` beyond ``abs_max_c`` / ``abs_min_c``;
* an at-limit reading is exactly ``max_c`` (HIGH) or ``min_c`` (LOW).

All temperatures are rounded to 2 decimals.
"""

import logging

import numpy as np
import pandas as pd

from coldchain import ids
from coldchain.config import AppConfig
from coldchain.enums import ExcursionDirection, TableName
from coldchain.errors import GenerationError
from coldchain.schemas import SCHEMAS

logger = logging.getLogger(__name__)

STAGES: dict[str, str] = {
    "offsets": "readings.offsets",
    "drift": "readings.drift",
    "noise": "readings.noise",
    "excursions": "readings.excursions",
}
"""RNG stream name of every random component of the readings."""


def reading_grid(loggers: pd.DataFrame, interval_min: int) -> pd.DataFrame:
    """Every reading instant of every logger window, on the reading grid.

    Args:
        loggers: The ``loggers`` table.
        interval_min: Minutes between two readings.

    Returns:
        One row per reading, grouped by logger in table order: ``logger_id``,
        ``shipment_id``, ``device_id``, ``index`` (0 at ``assigned_at``) and ``recorded_at``.
    """
    window = (loggers["unassigned_at"] - loggers["assigned_at"]).dt.total_seconds() // 60
    steps = (window.to_numpy() // interval_min).astype("int64") + 1
    row = np.repeat(np.arange(len(loggers)), steps)
    index = np.arange(steps.sum()) - np.repeat(np.cumsum(steps) - steps, steps)
    grid = loggers[["logger_id", "shipment_id", "device_id", "assigned_at"]].iloc[row]
    grid = grid.reset_index(drop=True)
    recorded = grid["assigned_at"] + pd.to_timedelta(index * interval_min, unit="min")
    return grid.drop(columns="assigned_at").assign(index=index, recorded_at=recorded)


def _rounded(values: np.ndarray | pd.Series) -> np.ndarray:
    """Round to 2 decimals (the project's temperature precision)."""
    return np.round(np.asarray(values, dtype="float64"), 2)


def _dwell_warming(config: AppConfig, grid: pd.DataFrame, stops: pd.DataFrame) -> np.ndarray:
    """Warming (°C, ≥ 0) of every reading from the latest intermediate stop before it.

    Args:
        config: Validated configuration.
        grid: Reading grid with ``shipment_id``, ``recorded_at`` and ``thermal_response``.
        stops: Intermediate stops with ``shipment_id``, ``arrived_at``, ``departed_at``.

    Returns:
        Warming per grid row (aligned with ``grid``).
    """
    settings = config.generation.readings
    if stops.empty:
        return np.zeros(len(grid))
    left = grid[["shipment_id", "recorded_at", "thermal_response"]].assign(row=np.arange(len(grid)))
    matched = pd.merge_asof(
        left.sort_values("recorded_at", kind="stable"),
        stops.sort_values("arrived_at", kind="stable"),
        left_on="recorded_at",
        right_on="arrived_at",
        by="shipment_id",
        direction="backward",
    ).sort_values("row")
    rate = matched["thermal_response"] * settings.dwell_warming_c_per_hour / 60.0
    since_arrival = (matched["recorded_at"] - matched["arrived_at"]).dt.total_seconds() / 60
    dwell = (matched["departed_at"] - matched["arrived_at"]).dt.total_seconds() / 60
    since_departure = (matched["recorded_at"] - matched["departed_at"]).dt.total_seconds() / 60
    cap = settings.max_dwell_warming_c
    during = np.minimum(cap, rate * since_arrival)
    after = np.minimum(cap, rate * dwell) * np.exp(
        -np.maximum(since_departure, 0) / settings.dwell_recovery_min
    )
    warming = np.where(since_departure <= 0, during, after)  # NaN before the first stop
    return np.nan_to_num(np.asarray(warming, dtype="float64"), nan=0.0)


def device_truth(devices: pd.DataFrame, rng: np.random.Generator) -> pd.DataFrame:
    """Draw the hidden systematic offset of every device (the ``device_truth`` table).

    The offset is uniform in ``[-uncertainty_c, +uncertainty_c]``, rounded to 2 decimals. It
    is ground truth only: the compliance engine never reads it (DECISIONS.md #54).

    Args:
        devices: The ``devices`` table.
        rng: The ``readings.offsets`` stream.

    Returns:
        ``device_id`` and ``systematic_offset_c``, in device order.
    """
    uncertainty = devices["uncertainty_c"].to_numpy(dtype="float64")
    offset = _rounded(rng.uniform(-1.0, 1.0, size=len(devices)) * uncertainty)
    return pd.DataFrame(
        {"device_id": devices["device_id"].to_numpy(), "systematic_offset_c": offset + 0.0}
    )


def clipping_counts(
    natural: np.ndarray, low: np.ndarray, high: np.ndarray, designed_rows: np.ndarray
) -> tuple[int, int]:
    """Count the natural readings that the safe band clipped.

    Args:
        natural: Rounded natural temperature of every grid row (before clipping).
        low: Lower safe-band bound per row.
        high: Upper safe-band bound per row.
        designed_rows: Rows overwritten by designed segments or ramps (not natural).

    Returns:
        ``(clipped, natural_count)``.
    """
    keep = np.ones(len(natural), dtype=bool)
    keep[designed_rows] = False
    clipped = ((natural < low) | (natural > high)) & keep
    return int(clipped.sum()), int(keep.sum())


def _bands(context: pd.DataFrame, margin: float) -> dict[str, np.ndarray]:
    """Safe, excursion and absolute-breach bounds per row (rounded to 2 decimals)."""
    u = context["uncertainty_c"]
    bands = {
        "safe_low": _rounded(context["min_c"] + u + margin),
        "safe_high": _rounded(context["max_c"] - u - margin),
        "high_low": _rounded(context["max_c"] + u + margin),
        "high_high": _rounded(context["abs_max_c"] - margin),
        "low_low": _rounded(context["abs_min_c"] + margin),
        "low_high": _rounded(context["min_c"] - u - margin),
    }
    for name in ("safe", "high", "low"):
        if (bands[f"{name}_low"] > bands[f"{name}_high"]).any():
            raise GenerationError(
                f"The {name} temperature band is empty for some shipment: lower "
                "generation.readings.limit_margin_c or the devices' uncertainty_c"
            )
    return bands


def generate_readings(
    config: AppConfig,
    tables: dict[TableName, pd.DataFrame],
    applied: pd.DataFrame,
    segments: pd.DataFrame,
    rngs: dict[str, np.random.Generator],
) -> pd.DataFrame:
    """Generate the ``temperature_readings`` table.

    Args:
        config: Validated configuration.
        tables: Generated tables; uses ``shipments``, ``loggers``, ``devices``,
            ``device_truth``, ``packaging`` and ``route_stops``.
        applied: Applied policy per shipment
            (:func:`coldchain.generate.expected.applied_policies`).
        segments: Designed segments (:func:`coldchain.generate.scenarios.design_segments`).
        rngs: One generator per key of :data:`STAGES` (``offsets`` is used by
            :func:`device_truth`, not here).

    Returns:
        The ``temperature_readings`` table (schema columns, one reading per grid instant).

    Raises:
        GenerationError: If a temperature band is empty for some shipment, or two designed
            segments' readings or ramps overlap.
    """
    settings = config.generation.readings
    loggers, devices = tables[TableName.LOGGERS], tables[TableName.DEVICES]
    grid = reading_grid(loggers, config.reading_interval_min)

    offsets = tables[TableName.DEVICE_TRUTH].set_index("device_id")["systematic_offset_c"]
    drift = loggers[["logger_id"]].assign(
        amplitude=rngs["drift"].uniform(
            settings.drift_amplitude_c.min, settings.drift_amplitude_c.max, len(loggers)
        ),
        period_min=60.0
        * rngs["drift"].uniform(
            settings.drift_period_hours.min, settings.drift_period_hours.max, len(loggers)
        ),
        phase=rngs["drift"].uniform(0.0, 2 * np.pi, len(loggers)),
    )
    packaging = tables[TableName.SHIPMENTS][["shipment_id", "packaging_id"]].merge(
        tables[TableName.PACKAGING][["packaging_id", "setpoint_c", "thermal_response"]],
        on="packaging_id",
    )
    context = (
        grid.merge(packaging, on="shipment_id", how="left")
        .merge(
            applied[["shipment_id", "min_c", "max_c", "abs_min_c", "abs_max_c"]],
            on="shipment_id",
            how="left",
        )
        .merge(devices[["device_id", "uncertainty_c"]], on="device_id", how="left")
        .merge(drift, on="logger_id", how="left")
    )
    stops = tables[TableName.ROUTE_STOPS]
    stops = stops.loc[
        stops["arrived_at"].notna() & stops["departed_at"].notna(),
        ["shipment_id", "arrived_at", "departed_at"],
    ]

    minutes = context["index"].to_numpy() * config.reading_interval_min
    noise = rngs["noise"].normal(0.0, settings.noise_sd_c, size=len(context))
    natural = (
        context["setpoint_c"].to_numpy()
        + offsets.loc[context["device_id"]].to_numpy()
        + context["amplitude"].to_numpy()
        * np.sin(
            2 * np.pi * minutes / context["period_min"].to_numpy() + context["phase"].to_numpy()
        )
        + _dwell_warming(config, context, stops)
        + noise
    )
    bands = _bands(context, settings.limit_margin_c)
    natural = _rounded(natural)
    temperature = np.clip(natural, bands["safe_low"], bands["safe_high"])

    rows, values = _designed_values(
        config, loggers, context, segments, bands, temperature, noise, rngs
    )
    clipped, natural_count = clipping_counts(natural, bands["safe_low"], bands["safe_high"], rows)
    logger.info(
        "Clipped %d of %d natural readings to the safe band (%.2f%%)",
        clipped,
        natural_count,
        100.0 * clipped / natural_count if natural_count else 0.0,
    )
    temperature[rows] = values

    readings = pd.DataFrame(
        {
            "reading_id": ids.reading_ids(context["logger_id"], context["recorded_at"]),
            "logger_id": context["logger_id"],
            "device_id": context["device_id"],
            "recorded_at": context["recorded_at"],
            "temperature_c": _rounded(temperature),
        }
    )
    return readings[list(SCHEMAS[TableName.TEMPERATURE_READINGS].columns)]


def _designed_values(
    config: AppConfig,
    loggers: pd.DataFrame,
    context: pd.DataFrame,
    segments: pd.DataFrame,
    bands: dict[str, np.ndarray],
    natural: np.ndarray,
    noise: np.ndarray,
    rngs: dict[str, np.random.Generator],
) -> tuple[np.ndarray, np.ndarray]:
    """Grid positions and temperatures of every designed reading and of its ramps.

    Args:
        config: Validated configuration.
        loggers: The ``loggers`` table (same order as the grid).
        context: The reading grid joined with its limits.
        segments: Designed segments.
        bands: Output of :func:`_bands`, aligned with ``context``.
        natural: Natural temperature per grid row, already clipped to the safe band.
        noise: Per-reading noise, aligned with ``context``.
        rngs: One generator per key of :data:`STAGES`.

    Returns:
        ``(rows, values)``: positions in ``context`` and their temperatures.

    Raises:
        GenerationError: If two designed readings or ramps fall on the same grid row.
    """
    rng = rngs["excursions"]
    excess = config.generation.scenarios.abs_breach_excess_c
    level_u = rng.uniform(0.0, 1.0, size=len(segments))
    excess_c = rng.uniform(excess.min, excess.max, size=len(segments))

    window = (loggers["unassigned_at"] - loggers["assigned_at"]).dt.total_seconds() // 60
    steps = (window.to_numpy() // config.reading_interval_min).astype("int64") + 1
    logger_start = pd.Series(np.cumsum(steps) - steps, index=loggers["logger_id"].to_numpy())
    first_row = (
        logger_start.loc[segments["logger_id"]].to_numpy() + segments["first_index"].to_numpy()
    ).astype("int64")
    count = segments["reading_count"].to_numpy(dtype="int64")
    high_side = (segments["direction"] == ExcursionDirection.HIGH.value).to_numpy()

    # The designed readings: a parabolic hump from just past the range limit to the peak.
    seg = np.repeat(np.arange(len(segments)), count)
    offset = pd.Series(seg).groupby(seg).cumcount().to_numpy()
    rows = first_row[seg] + offset
    high = high_side[seg]
    inner = np.where(high, bands["high_low"][rows], bands["low_high"][rows])
    outer = np.where(high, bands["high_high"][rows], bands["low_low"][rows])
    absolute_index = segments["absolute_index"].to_numpy()
    peak = np.where(absolute_index >= 0, absolute_index, (count - 1) / 2.0)
    half_width = np.maximum(peak, count - 1 - peak) + 1.0
    hump = 1.0 - ((offset - peak[seg]) / half_width[seg]) ** 2
    level = inner + level_u[seg] * (outer - inner)
    shaped = inner + hump * (level - inner) + noise[rows]
    excursion = np.clip(_rounded(shaped), np.minimum(inner, outer), np.maximum(inner, outer))

    absolute = absolute_index[seg] == offset
    beyond = np.maximum(_rounded(excess_c[seg]), 0.01)
    breach = np.where(
        high,
        context["abs_max_c"].to_numpy()[rows] + beyond,
        context["abs_min_c"].to_numpy()[rows] - beyond,
    )
    at_limit = np.where(high, context["max_c"].to_numpy()[rows], context["min_c"].to_numpy()[rows])
    is_excursion = segments["is_excursion"].to_numpy(dtype=bool)[seg]
    values = np.where(is_excursion, np.where(absolute, breach, excursion), at_limit)

    # The ramps: linear from the natural value toward the safe-band edge, and back.
    ramp_rows, ramp_weight, ramp_seg = [], [], []
    for side in ("ramp_before", "ramp_after"):
        length = segments[side].to_numpy(dtype="int64")
        ramp = np.repeat(np.arange(len(segments)), length)
        k = pd.Series(ramp).groupby(ramp).cumcount().to_numpy() + 1  # 1 .. length
        if side == "ramp_before":
            ramp_rows.append(first_row[ramp] - length[ramp] - 1 + k)
            ramp_weight.append(k / (length[ramp] + 1.0))
        else:
            ramp_rows.append(first_row[ramp] + count[ramp] - 1 + k)
            ramp_weight.append((length[ramp] + 1.0 - k) / (length[ramp] + 1.0))
        ramp_seg.append(ramp)
    r_rows = np.concatenate(ramp_rows).astype("int64")
    weight = np.concatenate(ramp_weight)
    edge = np.where(
        high_side[np.concatenate(ramp_seg)], bands["safe_high"][r_rows], bands["safe_low"][r_rows]
    )
    ramp_values = np.clip(
        _rounded(natural[r_rows] + weight * (edge - natural[r_rows])),
        bands["safe_low"][r_rows],
        bands["safe_high"][r_rows],
    )

    all_rows = np.concatenate([rows, r_rows]).astype("int64")
    if len(np.unique(all_rows)) != len(all_rows):
        raise GenerationError("Designed segments or their ramps overlap on the reading grid")
    return all_rows, _rounded(np.concatenate([values, ramp_values]))
