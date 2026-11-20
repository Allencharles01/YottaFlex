"""Shared inputs of the validation checks: the tables, the limits, and derived frames.

The validation suite judges the data on its own. It never imports the compliance engine, so
every derived value below (assignment windows, trusted readings, excursion runs, applied
policies) is recomputed here, independently, from the raw tables and the documented rules
(``docs/compliance_rules.md``). Agreement with the engine's output is then real evidence, not a
tautology.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import cached_property

import duckdb
import numpy as np
import pandas as pd

from coldchain.config import AppConfig, ReferenceData
from coldchain.enums import ExcursionDirection, StopType, TableName
from coldchain.schemas import effective_policy_ids

MINUTE = pd.Timedelta(minutes=1)
"""One minute, for timestamp arithmetic."""

INTERMEDIATE_STOP_TYPES = frozenset({StopType.WAREHOUSE.value, StopType.CROSS_DOCK.value})
"""Stop types that sit between the origin and the destination."""


@dataclass(frozen=True)
class Limits:
    """Config and reference values the checks compare against.

    Attributes:
        reading_interval_min: Config reading interval (fallback when no device is known).
        window_start: First instant a shipment may be planned to depart (UTC).
        window_end: First instant after the date window (exclusive, UTC).
        trip_hours: Inclusive range of planned trip durations, in hours.
        planning_lead_hours: Inclusive range of the PLANNED event's lead before departure.
        long_dwell_threshold_min: Dwell above which LONG_DWELL applies.
        cities: Every reference city.
        hub_cities: Cities that host warehouses and cross-docks.
        manufacturing_cities: Cities that host suppliers.
    """

    reading_interval_min: int
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    trip_hours: tuple[float, float]
    planning_lead_hours: tuple[float, float]
    long_dwell_threshold_min: int
    cities: frozenset[str]
    hub_cities: frozenset[str]
    manufacturing_cities: frozenset[str]

    @classmethod
    def from_config(cls, config: AppConfig, reference: ReferenceData) -> "Limits":
        """Collect the limits from the validated config and reference data.

        Args:
            config: Pipeline configuration.
            reference: Reference data (locations).

        Returns:
            The limits.
        """
        window = config.date_window
        lead = config.generation.planning_lead_hours
        return cls(
            reading_interval_min=config.reading_interval_min,
            window_start=pd.Timestamp(window.start).tz_localize("UTC"),
            window_end=pd.Timestamp(window.end).tz_localize("UTC") + pd.Timedelta(days=1),
            trip_hours=(config.trip_duration_hours.min, config.trip_duration_hours.max),
            planning_lead_hours=(float(lead.min), float(lead.max)),
            long_dwell_threshold_min=config.long_dwell_threshold_min,
            cities=frozenset(loc.city for loc in reference.locations),
            hub_cities=frozenset(loc.city for loc in reference.locations if loc.is_hub),
            manufacturing_cities=frozenset(
                loc.city for loc in reference.locations if loc.is_manufacturing_cluster
            ),
        )


def minutes_between(later: pd.Series, earlier: pd.Series) -> pd.Series:
    """Return ``later - earlier`` in (float) minutes."""
    return (later - earlier) / MINUTE


@dataclass
class ValidationContext:
    """The validated tables, the limits, a DuckDB connection, and lazily derived frames.

    Derived frames are computed once and shared by every check that needs them. They assume
    the tables they read are present; the runner only calls a check when its tables are.

    Attributes:
        tables: The input tables, keyed by name (a subset of the raw and compliance tables).
        limits: Config and reference limits.
        memo: Results shared by several checks (e.g. one batched DuckDB query for all keys).
    """

    tables: Mapping[TableName, pd.DataFrame]
    limits: Limits
    memo: dict[str, pd.DataFrame] = field(default_factory=dict, init=False, repr=False)
    _con: duckdb.DuckDBPyConnection | None = field(default=None, init=False, repr=False)

    def has(self, *names: TableName) -> bool:
        """Return whether every named table is part of the input."""
        return all(name in self.tables for name in names)

    def sql(self, query: str) -> pd.DataFrame:
        """Run a DuckDB query over the input tables (each registered under its name).

        Args:
            query: SQL text; tables are referenced by their :class:`TableName` value.

        Returns:
            The result as a DataFrame.
        """
        if self._con is None:
            self._con = duckdb.connect(database=":memory:")
            for name, frame in self.tables.items():
                self._con.register(name.value, frame)
        return self._con.execute(query).df()

    def close(self) -> None:
        """Close the DuckDB connection, if one was opened."""
        if self._con is not None:
            self._con.close()
            self._con = None

    # ----------------------------------------------------------------------------------------
    # Shipment-level facts
    # ----------------------------------------------------------------------------------------

    @cached_property
    def shipment_classes(self) -> pd.DataFrame:
        """Policy codes and suppliers of each shipment's products (``shipment_lots`` → products).

        Returns:
            One row per shipment with lots: ``shipment_id``, ``code_count``, ``policy_code``
            (the first code in sorted order), ``supplier_count`` and ``product_supplier_id``.
        """
        links = (
            self.tables[TableName.SHIPMENT_LOTS][["shipment_id", "lot_id"]]
            .merge(self.tables[TableName.LOTS][["lot_id", "product_id"]], on="lot_id")
            .merge(
                self.tables[TableName.PRODUCTS][["product_id", "policy_code", "supplier_party_id"]],
                on="product_id",
            )
        )
        grouped = links.sort_values(["shipment_id", "policy_code"], kind="stable").groupby(
            "shipment_id"
        )
        return pd.DataFrame(
            {
                "code_count": grouped["policy_code"].nunique(),
                "policy_code": grouped["policy_code"].first(),
                "supplier_count": grouped["supplier_party_id"].nunique(),
                "product_supplier_id": grouped["supplier_party_id"].first(),
            }
        ).reset_index()

    @cached_property
    def applied_policies(self) -> pd.DataFrame:
        """The policy each shipment must be judged by, recomputed from its products.

        A shipment's products must share one code (otherwise it gets no applied policy here,
        and the class-consistency check reports it). The applied version is the one effective
        at ``actual_departure_at``; the version the planner should have declared is the one
        effective at ``planned_departure_at`` (DECISIONS.md #26).

        Returns:
            One row per single-class shipment: ``shipment_id``, ``policy_code``,
            ``applied_policy_id`` and ``planned_policy_id`` (missing if no version is effective).
        """
        shipments = self.tables[TableName.SHIPMENTS]
        classes = self.shipment_classes
        single = classes.loc[classes["code_count"] == 1, ["shipment_id", "policy_code"]]
        frame = single.merge(
            shipments[["shipment_id", "planned_departure_at", "actual_departure_at"]],
            on="shipment_id",
        )
        policies = self.tables[TableName.THRESHOLD_POLICIES]
        return pd.DataFrame(
            {
                "shipment_id": frame["shipment_id"],
                "policy_code": frame["policy_code"],
                "applied_policy_id": effective_policy_ids(
                    policies, frame["policy_code"], frame["actual_departure_at"]
                ),
                "planned_policy_id": effective_policy_ids(
                    policies, frame["policy_code"], frame["planned_departure_at"]
                ),
            }
        )

    @cached_property
    def windows(self) -> pd.DataFrame:
        """The judged window *W* of every shipment (compliance_rules.md §1.2).

        With a logger, *W* = ``[assigned_at, unassigned_at]`` and *i* = the device's
        ``reading_interval_min``; without one, *W* = actual departure → arrival and *i* = the
        config interval (DECISIONS.md #59).

        Returns:
            One row per shipment: ``shipment_id``, ``logger_id`` and ``device_id`` (missing
            without a logger), ``start``, ``end``, ``interval_min``.
        """
        shipments = self.tables[TableName.SHIPMENTS][
            ["shipment_id", "actual_departure_at", "actual_arrival_at"]
        ]
        loggers = self.tables[TableName.LOGGERS][
            ["shipment_id", "logger_id", "device_id", "assigned_at", "unassigned_at"]
        ].drop_duplicates("shipment_id")
        devices = self.tables[TableName.DEVICES][["device_id", "reading_interval_min"]]
        frame = shipments.merge(loggers, on="shipment_id", how="left").merge(
            devices.drop_duplicates("device_id"), on="device_id", how="left"
        )
        has_logger = frame["logger_id"].notna()
        interval = frame["reading_interval_min"].astype("Float64")
        interval = interval.where(has_logger, np.nan).fillna(self.limits.reading_interval_min)
        return pd.DataFrame(
            {
                "shipment_id": frame["shipment_id"],
                "logger_id": frame["logger_id"],
                "device_id": frame["device_id"],
                "start": frame["assigned_at"].where(has_logger, frame["actual_departure_at"]),
                "end": frame["unassigned_at"].where(has_logger, frame["actual_arrival_at"]),
                "interval_min": interval.astype("int64"),
            }
        )

    @cached_property
    def trusted_readings(self) -> pd.DataFrame:
        """Readings that count for their shipment (compliance_rules.md §1.2).

        A reading is trusted when it is on the shipment's logger, carries the assigned device
        and lies inside *W* (both ends inclusive).

        Returns:
            ``reading_id``, ``logger_id``, ``shipment_id``, ``recorded_at``, ``temperature_c``
            and ``interval_min``, sorted by (``logger_id``, ``recorded_at``).
        """
        readings = self.tables[TableName.TEMPERATURE_READINGS]
        windows = self.windows.dropna(subset=["logger_id"])
        frame = readings.merge(
            windows[["logger_id", "shipment_id", "device_id", "start", "end", "interval_min"]],
            on="logger_id",
            suffixes=("", "_assigned"),
        )
        keep = (
            (frame["device_id"] == frame["device_id_assigned"])
            & (frame["recorded_at"] >= frame["start"])
            & (frame["recorded_at"] <= frame["end"])
        )
        columns = [
            "reading_id",
            "logger_id",
            "shipment_id",
            "recorded_at",
            "temperature_c",
            "interval_min",
        ]
        return (
            frame.loc[keep, columns]
            .sort_values(["logger_id", "recorded_at"], kind="stable")
            .reset_index(drop=True)
        )

    @cached_property
    def policy_limits(self) -> pd.DataFrame:
        """The applied policy's limits per shipment (from :attr:`applied_policies`).

        Returns:
            ``shipment_id``, ``applied_policy_id`` and the policy's limit columns.
        """
        policies = self.tables[TableName.THRESHOLD_POLICIES]
        return self.applied_policies[["shipment_id", "applied_policy_id"]].merge(
            policies.rename(columns={"policy_id": "applied_policy_id"}),
            on="applied_policy_id",
        )

    @cached_property
    def excursion_runs(self) -> pd.DataFrame:
        """Excursions recomputed from the trusted readings (compliance_rules.md §1.5).

        An excursion is a maximal run of consecutive trusted readings of one logger that are
        out of range in the same direction (a gap does not end a run, DECISIONS.md #9).

        Returns:
            One row per out-of-range trusted reading: ``reading_id``, ``logger_id``,
            ``shipment_id``, ``recorded_at``, ``temperature_c``, ``direction``, ``run_start``
            (the run's first reading time) and the policy limits used.
        """
        limits = self.policy_limits[
            [
                "shipment_id",
                "applied_policy_id",
                "min_c",
                "max_c",
                "abs_min_c",
                "abs_max_c",
                "max_single_excursion_min",
            ]
        ]
        frame = self.trusted_readings.merge(limits, on="shipment_id")
        frame = frame.sort_values(["logger_id", "recorded_at"], kind="stable").reset_index(
            drop=True
        )
        high = frame["temperature_c"] > frame["max_c"]
        low = frame["temperature_c"] < frame["min_c"]
        direction = pd.Series(
            np.select(
                [high, low],
                [ExcursionDirection.HIGH.value, ExcursionDirection.LOW.value],
                default="",
            ),
            index=frame.index,
            dtype="str",
        )
        new_run = (frame["logger_id"] != frame["logger_id"].shift()) | (
            direction != direction.shift()
        )
        run_key = new_run.cumsum()
        frame = frame.assign(direction=direction, run_key=run_key)
        out = frame[direction != ""].copy()
        out["run_start"] = out.groupby("run_key")["recorded_at"].transform("min")
        return out.drop(columns="run_key").reset_index(drop=True)

    @cached_property
    def intermediate_stops(self) -> pd.DataFrame:
        """Intermediate route stops with their rank (0-based) within the shipment.

        Returns:
            ``shipment_id``, ``stop_id``, ``party_id``, ``arrived_at``, ``departed_at``,
            ``rank``.
        """
        stops = self.tables[TableName.ROUTE_STOPS]
        middle = stops[stops["stop_type"].isin(INTERMEDIATE_STOP_TYPES)].sort_values(
            ["shipment_id", "sequence"], kind="stable"
        )
        middle = middle.assign(rank=middle.groupby("shipment_id").cumcount())
        return middle[
            ["shipment_id", "stop_id", "party_id", "arrived_at", "departed_at", "rank"]
        ].reset_index(drop=True)


def padded_max_gaps(
    times: pd.DataFrame, windows: pd.DataFrame, key: str = "shipment_id"
) -> pd.Series:
    """Largest gap per window, padding the readings with virtual ones at both window edges.

    Implements compliance_rules.md §1.4: the reading times are padded with ``start - i`` and
    ``end + i``; a complete stream then has a largest gap of exactly *i*, and a window with no
    reading has ``(end - start) + 2i``.

    Args:
        times: ``key`` and ``recorded_at`` of the readings to measure.
        windows: ``key``, ``start``, ``end`` and ``interval_min`` per window.
        key: Column that identifies a window (``shipment_id`` or ``logger_id``).

    Returns:
        Largest gap in minutes (int), indexed by ``key``.
    """
    step = windows["interval_min"] * MINUTE
    edges = pd.concat(
        [
            pd.DataFrame({key: windows[key], "recorded_at": windows["start"] - step}),
            pd.DataFrame({key: windows[key], "recorded_at": windows["end"] + step}),
        ]
    )
    points = pd.concat([times[[key, "recorded_at"]], edges], ignore_index=True)
    points = points.sort_values([key, "recorded_at"], kind="stable")
    gaps = points.groupby(key)["recorded_at"].diff() / MINUTE
    return gaps.groupby(points[key]).max().round().astype("int64")
