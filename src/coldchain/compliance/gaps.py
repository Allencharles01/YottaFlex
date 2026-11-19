"""Reading coverage and gaps (compliance_rules.md §1.3 and §1.4, DECISIONS.md #14).

* ``expected_reading_count = ⌊(window_end - window_start) / i⌋ + 1`` (exact for a logger window,
  which lies on the reading grid; floored for the no-logger fallback, DECISIONS.md #59);
* ``coverage_pct = round(100 x trusted / expected, 2)``; LOW_COVERAGE if below
  ``min_coverage_pct`` (strict);
* gaps are measured over the trusted reading times padded with virtual readings at
  ``window_start - i`` and ``window_end + i``; DATA_GAP if ``max_gap_min > max_reading_gap_min``
  (strict). A complete stream has ``max_gap_min = i``.
"""

import numpy as np
import pandas as pd

from coldchain.enums import ReasonCode

COVERAGE_COLUMNS = ("reading_count", "expected_reading_count", "coverage_pct", "max_gap_min")
"""Metric columns of :func:`coverage_and_gaps`."""


def _minutes(delta: pd.Series) -> pd.Series:
    """Whole minutes of a timedelta column (timestamps are whole minutes by schema)."""
    return (delta.dt.total_seconds() // 60).astype("int64")


def coverage_and_gaps(
    windows: pd.DataFrame, trusted: pd.DataFrame, policies: pd.DataFrame
) -> pd.DataFrame:
    """Compute coverage and the largest gap of every shipment, and their reason flags.

    Args:
        windows: Per-shipment windows
            (:func:`coldchain.compliance.logger_check.assignment_windows`).
        trusted: Trusted readings (``shipment_id``, ``recorded_at``).
        policies: Applied policy per shipment with ``max_reading_gap_min`` and
            ``min_coverage_pct`` (:func:`coldchain.compliance.policy.resolve_policies`).

    Returns:
        Frame indexed by ``shipment_id`` with :data:`COVERAGE_COLUMNS` and boolean DATA_GAP and
        LOW_COVERAGE columns.
    """
    index = pd.Index(windows["shipment_id"], name="shipment_id")
    frame = windows.set_index("shipment_id")[["window_start", "window_end", "interval_min"]]
    limits = policies.set_index("shipment_id").loc[index]
    step = pd.to_timedelta(frame["interval_min"], unit="min")

    expected = _minutes(frame["window_end"] - frame["window_start"]) // frame["interval_min"] + 1
    count = trusted.groupby("shipment_id").size().reindex(index, fill_value=0).astype("int64")
    coverage = (100.0 * count / expected).round(2)

    padding = pd.DataFrame(
        {
            "shipment_id": np.concatenate([index.to_numpy(), index.to_numpy()]),
            "recorded_at": pd.concat(
                [frame["window_start"] - step, frame["window_end"] + step], ignore_index=True
            ),
        }
    )
    times = pd.concat([trusted[["shipment_id", "recorded_at"]], padding], ignore_index=True)
    times = times.sort_values(["shipment_id", "recorded_at"], kind="stable")
    gaps = times.groupby("shipment_id")["recorded_at"].diff().dropna()
    max_gap = (
        _minutes(gaps).groupby(times.loc[gaps.index, "shipment_id"]).max().reindex(index)
    ).astype("int64")

    return pd.DataFrame(
        {
            "reading_count": count,
            "expected_reading_count": expected.astype("int64"),
            "coverage_pct": coverage.astype("float64"),
            "max_gap_min": max_gap,
            ReasonCode.DATA_GAP.value: (max_gap > limits["max_reading_gap_min"]).to_numpy(),
            ReasonCode.LOW_COVERAGE.value: (coverage < limits["min_coverage_pct"]).to_numpy(),
        },
        index=index,
    )
