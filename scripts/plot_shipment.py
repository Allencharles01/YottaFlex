"""Plot one shipment's temperature readings against its applied policy (UAT helper).

Reads only the ``raw/`` layer: the shipment's readings, and the policy effective at actual
departure for its products (compliance_rules.md §1.1). Saves
``<output-dir>/<scale>/plots/<shipment_id>.png`` with the readings line, the allowed
``min_c``-``max_c`` band (green), dashed absolute limits and every excursion window (red).

Usage:
    python scripts/plot_shipment.py SHP-2026-000018 [--scale starter] [--output-dir output]
"""

import argparse
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from coldchain.config import load_config
from coldchain.enums import Layer, TableName
from coldchain.generate.expected import applied_policies
from coldchain.io import layer_dir, read_tables

logger = logging.getLogger("plot_shipment")

BAND_COLOR = "#2e9e5b"
LINE_COLOR = "#1f4e79"
EXCURSION_COLOR = "#d64545"
LIMIT_COLOR = "#555555"

TABLES = (
    TableName.SHIPMENTS,
    TableName.SHIPMENT_LOTS,
    TableName.LOTS,
    TableName.PRODUCTS,
    TableName.THRESHOLD_POLICIES,
    TableName.LOGGERS,
    TableName.TEMPERATURE_READINGS,
)


def excursion_windows(
    readings: pd.DataFrame, min_c: float, max_c: float, interval_min: int
) -> pd.DataFrame:
    """Find the runs of consecutive readings outside ``[min_c, max_c]``.

    Each window spans from its first out-of-range reading to its last one plus one reading
    interval, matching the duration rule in compliance_rules.md.

    Args:
        readings: One logger's readings sorted by ``recorded_at``.
        min_c: Policy lower limit.
        max_c: Policy upper limit.
        interval_min: Reading interval in minutes.

    Returns:
        Columns ``start``, ``end`` and ``duration_min``; empty when nothing left the range.
    """
    outside = (readings["temperature_c"] < min_c) | (readings["temperature_c"] > max_c)
    run = (outside != outside.shift(fill_value=False)).cumsum()
    times = readings.loc[outside, "recorded_at"].groupby(run[outside])
    windows = pd.DataFrame({"start": times.min(), "last": times.max()}).reset_index(drop=True)
    windows["end"] = windows["last"] + pd.Timedelta(minutes=interval_min)
    windows["duration_min"] = (windows["end"] - windows["start"]).dt.total_seconds() // 60
    return windows[["start", "end", "duration_min"]]


def plot_shipment(shipment_id: str, raw_dir: Path, out_path: Path, interval_min: int) -> Path:
    """Draw and save the temperature plot of one shipment.

    Args:
        shipment_id: The shipment to plot.
        raw_dir: The ``raw/`` layer directory.
        out_path: Where to save the PNG.
        interval_min: Reading interval in minutes.

    Returns:
        The saved file path.

    Raises:
        SystemExit: If the shipment or its readings are not found.
    """
    tables = read_tables(TABLES, raw_dir)
    shipments = tables[TableName.SHIPMENTS]
    if shipment_id not in set(shipments["shipment_id"]):
        raise SystemExit(f"Unknown shipment: {shipment_id}")
    policy = applied_policies(
        shipments[shipments["shipment_id"] == shipment_id],
        tables[TableName.SHIPMENT_LOTS],
        tables[TableName.LOTS],
        tables[TableName.PRODUCTS],
        tables[TableName.THRESHOLD_POLICIES],
    ).iloc[0]
    loggers = tables[TableName.LOGGERS]
    logger_ids = loggers.loc[loggers["shipment_id"] == shipment_id, "logger_id"]
    readings = tables[TableName.TEMPERATURE_READINGS]
    readings = readings[readings["logger_id"].isin(logger_ids)].sort_values("recorded_at")
    if readings.empty:
        raise SystemExit(f"No readings for {shipment_id}")
    windows = excursion_windows(readings, policy["min_c"], policy["max_c"], interval_min)

    fig, ax = plt.subplots(figsize=(11, 4.5), dpi=120)
    ax.axhspan(
        policy["min_c"],
        policy["max_c"],
        color=BAND_COLOR,
        alpha=0.15,
        lw=0,
        label=f"Allowed range {policy['min_c']:g} to {policy['max_c']:g} °C",
    )
    for value in (policy["abs_min_c"], policy["abs_max_c"]):
        ax.axhline(value, color=LIMIT_COLOR, ls="--", lw=1)
    ax.plot([], [], color=LIMIT_COLOR, ls="--", lw=1, label="Absolute limits")
    for i, w in enumerate(windows.itertuples()):
        ax.axvspan(
            w.start,
            w.end,
            color=EXCURSION_COLOR,
            alpha=0.2,
            lw=0,
            label="Excursion" if i == 0 else None,
        )
        ax.annotate(
            f"{int(w.duration_min)} min",
            xy=(w.start, 1),
            xycoords=("data", "axes fraction"),
            xytext=(2, -12),
            textcoords="offset points",
            fontsize=8,
            color="#333333",
        )
    ax.plot(
        readings["recorded_at"],
        readings["temperature_c"],
        color=LINE_COLOR,
        lw=1.5,
        label="Readings",
    )
    ax.set_title(f"{shipment_id} — applied {policy['applied_policy_id']}", loc="left")
    ax.set_ylabel("Temperature (°C)")
    ax.set_xlabel("Recorded at (UTC)")
    ax.grid(axis="y", color="#e0e0e0", lw=0.8)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9)
    fig.autofmt_xdate()
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path)
    plt.close(fig)
    logger.info(
        "Saved %s (%d readings, %d excursion window(s))", out_path, len(readings), len(windows)
    )
    return out_path


def main() -> None:
    """Parse arguments and plot the shipment."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("shipment_id")
    parser.add_argument("--scale", default="starter")
    parser.add_argument("--output-dir", type=Path, default=Path("output"))
    parser.add_argument("--config", type=Path, default=None, help="Config YAML (optional).")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config(args.config) if args.config else load_config()
    raw_dir = layer_dir(args.output_dir, args.scale, Layer.RAW)
    out_path = args.output_dir / args.scale / "plots" / f"{args.shipment_id}.png"
    plot_shipment(args.shipment_id, raw_dir, out_path, config.reading_interval_min)


if __name__ == "__main__":
    main()
