"""Pandera schemas for every table: the executable twin of ``docs/data_dictionary.md``.

Schemas enforce *structural* rules only: column set and order, dtypes, nulls, allowed values,
ID formats, primary-key uniqueness, row-level consistency (e.g. arrival after departure) and IDs
that must embed their parent's ID. Business rules that the injected anomalies deliberately break
(custody continuity, device overlap, declared-policy match, reading gaps, ...) are *not* schema
rules: they are detected by the compliance engine and the validation suite, so anomalous but
well-formed data still passes its schema. A test keeps this module and the data dictionary in
sync (same tables, columns, types and required flags).
"""

import re
from collections.abc import Callable, Iterable
from datetime import date
from enum import StrEnum
from typing import Any

import numpy as np
import pandas as pd
import pandera.pandas as pa
from pandera.engines import pandas_engine

from coldchain import ids
from coldchain.enums import (
    STATUS_ACTION,
    BreachType,
    CarrierReportedStatus,
    ComplianceStatus,
    CustodyEventType,
    DecidedAction,
    ExcursionDirection,
    IssueType,
    Layer,
    LotStatus,
    PackagingType,
    PartyType,
    PolicyCode,
    PolicySource,
    ProductCategory,
    ReasonCode,
    Scenario,
    ShipmentStatus,
    StopType,
    TableName,
    worst_status,
)
from coldchain.errors import SchemaValidationError

TIMESTAMP_DTYPE = pd.DatetimeTZDtype(unit="us", tz="UTC")
"""Dtype of every timestamp column: timezone-aware UTC, microsecond resolution."""

DATE_DTYPE = pandas_engine.Date
"""Dtype of every calendar-date column (``datetime.date`` values)."""

ISO_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
"""Text form of a UTC timestamp wherever one is embedded in a string (e.g. excursion windows)."""

LIST_SEPARATOR = "|"
"""Separator for multi-valued text columns (reason codes, ID lists, durations)."""

SENSOR_RANGE_C = (-100.0, 100.0)
"""Physical measurement range of a temperature sensor, in °C (sanity bound)."""

GENERATOR_STATUSES = frozenset(
    {
        ShipmentStatus.PLANNED,
        ShipmentStatus.PICKED_UP,
        ShipmentStatus.IN_TRANSIT,
        ShipmentStatus.AT_STOP,
        ShipmentStatus.DELIVERED,
    }
)
"""Shipment statuses written by the generator (``raw/shipment_status_events``)."""

ENGINE_STATUSES = frozenset(set(ShipmentStatus) - GENERATOR_STATUSES)
"""Shipment statuses appended by the compliance engine (``compliance_status_events``)."""

_ANY_ID = re.compile("|".join(f"(?:{p.pattern})" for p in ids.ID_PATTERNS.values()))
_INT_LIST = re.compile(r"^\d+(?:\|\d+)*$")
_TS = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z"
_WINDOW_LIST = re.compile(rf"^{_TS}/{_TS}(?:\|{_TS}/{_TS})*$")
_REASON_ORDER = {code.value: rank for rank, code in enumerate(ReasonCode)}


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------


def split_list(value: Any) -> list[str]:
    """Split a ``|``-separated cell into its items.

    Args:
        value: Cell value; ``None``/``NaN``/empty string mean "no items".

    Returns:
        The list of items (possibly empty).
    """
    if value is None or (isinstance(value, float) and np.isnan(value)) or value == "":
        return []
    return str(value).split(LIST_SEPARATOR)


def _minutes(later: pd.Series, earlier: pd.Series) -> pd.Series:
    """Difference between two timestamp columns, in minutes."""
    return (later - earlier).dt.total_seconds() / 60.0


def _as_datetime(dates: pd.Series) -> pd.Series:
    """Convert a column of ``datetime.date`` (or nulls) to ``datetime64`` for arithmetic."""
    return pd.to_datetime(dates)


def overlapping_policy_versions(policies: pd.DataFrame) -> pd.Series:
    """Flag policy versions whose effective window overlaps an earlier version of the same code.

    Windows are half-open: ``[effective_from, effective_to)``; a null ``effective_to`` means
    open-ended. Rows are compared within each ``policy_code`` in ``effective_from`` order.

    Args:
        policies: Frame with ``policy_code``, ``effective_from`` and ``effective_to`` columns.

    Returns:
        Boolean Series aligned with ``policies``: ``True`` where the row overlaps.
    """
    if policies.empty:
        return pd.Series(False, index=policies.index, dtype=bool)
    start = _as_datetime(policies["effective_from"])
    end = _as_datetime(policies["effective_to"].fillna(date.max))  # open-ended → far future
    frame = pd.DataFrame({"code": policies["policy_code"], "start": start, "end": end})
    frame = frame.sort_values(["code", "start"], kind="stable")
    previous_end = frame.groupby("code")["end"].cummax().groupby(frame["code"]).shift()
    overlaps = frame["start"] < previous_end
    return overlaps.reindex(policies.index).fillna(False).astype(bool)


def effective_policy_ids(
    policies: pd.DataFrame, codes: pd.Series, instants: pd.Series
) -> pd.Series:
    """Resolve, per row, the policy version of a code that is effective at an instant.

    Windows are half-open ``[effective_from, effective_to)`` with dates read as UTC midnight and
    a null ``effective_to`` open-ended (DECISIONS.md #12).

    Args:
        policies: Frame with ``policy_id``, ``policy_code``, ``effective_from``, ``effective_to``.
        codes: Policy code per row.
        instants: UTC timestamp per row, aligned with ``codes``.

    Returns:
        Series of policy IDs aligned with ``codes``; missing where no version is effective.
    """
    start = pd.to_datetime(policies["effective_from"]).dt.tz_localize("UTC")
    end = pd.to_datetime(policies["effective_to"].fillna(date.max)).dt.tz_localize("UTC")
    windows = pd.DataFrame(
        {"policy_code": policies["policy_code"], "policy_id": policies["policy_id"], "s": start}
    ).assign(e=end)
    rows = pd.DataFrame(
        {"policy_code": codes.to_numpy(), "t": instants.to_numpy(), "row": np.arange(len(codes))}
    )
    matched = rows.merge(windows, on="policy_code", how="inner")
    matched = matched[(matched["s"] <= matched["t"]) & (matched["t"] < matched["e"])]
    resolved = matched.drop_duplicates("row").set_index("row")["policy_id"]
    result = resolved.reindex(np.arange(len(codes)))
    result.index = codes.index
    return result


# --------------------------------------------------------------------------------------------
# Column builders (metadata["role"] lets tests introspect the schemas)
# --------------------------------------------------------------------------------------------


def _column(
    dtype: Any,
    checks: Iterable[pa.Check] = (),
    *,
    nullable: bool = False,
    unique: bool = False,
    role: str = "value",
    description: str = "",
) -> pa.Column:
    """Build a column with the project-wide defaults."""
    return pa.Column(
        dtype,
        checks=list(checks),
        nullable=nullable,
        unique=unique,
        description=description,
        metadata={"role": role},
    )


def _id(kind: str, *, nullable: bool = False, unique: bool = False) -> pa.Column:
    """An ID column that must match :data:`coldchain.ids.ID_PATTERNS` ``[kind]``."""
    check = pa.Check.str_matches(ids.ID_PATTERNS[kind].pattern, error=f"{kind}_id_format")
    return _column(str, [check], nullable=nullable, unique=unique, role="id")


def _enum(enum_type: type[StrEnum], *, nullable: bool = False) -> pa.Column:
    """A categorical column restricted to the values of ``enum_type``."""
    check = pa.Check.isin([m.value for m in enum_type], error=f"allowed_{enum_type.__name__}")
    return _column(str, [check], nullable=nullable, role="enum")


def _text(max_length: int, *, unique: bool = False) -> pa.Column:
    """A required free-text column: non-empty, no surrounding whitespace, bounded length."""
    checks = [
        pa.Check.str_length(min_value=1, max_value=max_length),
        pa.Check(lambda s: s == s.str.strip(), error="no_surrounding_whitespace"),
    ]
    return _column(str, checks, unique=unique)


def _int(
    min_value: int, max_value: int | None = None, *, nullable: bool = False, unique: bool = False
) -> pa.Column:
    """An integer column within ``[min_value, max_value]`` (nullable columns use ``Int64``)."""
    checks = [pa.Check.ge(min_value)]
    if max_value is not None:
        checks.append(pa.Check.le(max_value))
    dtype = pd.Int64Dtype() if nullable else "int64"
    return _column(dtype, checks, nullable=nullable, unique=unique)


def _two_decimals() -> pa.Check:
    """Check that floats are rounded to 2 decimal places."""
    return pa.Check(lambda s: (s - s.round(2)).abs() <= 1e-9, error="rounded_to_2_decimals")


def _float(
    min_value: float, max_value: float, *, min_inclusive: bool = True, rounded: bool = True
) -> pa.Column:
    """A required float column within a range (optionally rounded to 2 decimals)."""
    lower = pa.Check.ge(min_value) if min_inclusive else pa.Check.gt(min_value)
    checks = [lower, pa.Check.le(max_value)]
    if rounded:
        checks.append(_two_decimals())
    return _column("float64", checks)


def _celsius() -> pa.Column:
    """A temperature in °C: float, within the sensor range, rounded to 2 decimals."""
    return _float(*SENSOR_RANGE_C)


def _timestamp(*, nullable: bool = False) -> pa.Column:
    """A UTC timestamp column with whole-minute precision."""
    check = pa.Check(
        lambda s: (s.dt.second == 0) & (s.dt.microsecond == 0), error="whole_minute_timestamp"
    )
    return _column(TIMESTAMP_DTYPE, [check], nullable=nullable)


def _date(*, nullable: bool = False) -> pa.Column:
    """A calendar-date column."""
    return _column(DATE_DTYPE, nullable=nullable)


def _list_column(item_ok: Callable[[list[str]], bool], error: str, role: str) -> pa.Column:
    """A nullable ``|``-separated multi-valued text column, validated per cell."""
    check = pa.Check(lambda value: item_ok(split_list(value)), element_wise=True, error=error)
    return _column(str, [check], nullable=True, role=role)


def _id_list(pattern: re.Pattern[str]) -> pa.Column:
    """A nullable list of distinct IDs, each matching ``pattern``."""

    def ok(items: list[str]) -> bool:
        return bool(items) and len(set(items)) == len(items) and all(map(pattern.match, items))

    return _list_column(ok, "distinct_ids_in_list", role="id_list")


def _reason_list() -> pa.Column:
    """A nullable list of distinct reason codes in canonical (enum declaration) order."""

    def ok(items: list[str]) -> bool:
        if not items or not all(item in _REASON_ORDER for item in items):
            return False
        ranks = [_REASON_ORDER[item] for item in items]
        return ranks == sorted(set(ranks))

    return _list_column(ok, "reason_codes_distinct_and_ordered", role="enum_list")


def _pattern_cell(pattern: re.Pattern[str]) -> pa.Column:
    """A nullable text column whose non-null cells match ``pattern``."""
    check = pa.Check.str_matches(pattern.pattern, error="list_format")
    return _column(str, [check], nullable=True)


# --------------------------------------------------------------------------------------------
# Table-level rules
# --------------------------------------------------------------------------------------------


def _rule(name: str, func: Callable[[pd.DataFrame], pd.Series]) -> pa.Check:
    """Wrap a row-wise boolean rule over the whole frame as a named pandera check."""
    return pa.Check(func, name=name, error=name)


def _unique_together(*columns: str) -> pa.Check:
    """Rule: the combination of ``columns`` is unique."""
    return _rule(f"unique_{'_'.join(columns)}", lambda df: ~df.duplicated(list(columns)))


def _not_before(later: str, earlier: str, *, strict: bool) -> pa.Check:
    """Rule: column ``later`` is after (``strict``) or not before column ``earlier``.

    Rows where either value is null pass (presence is checked separately).
    """

    def check(df: pd.DataFrame) -> pd.Series:
        a, b = df[later], df[earlier]
        ok = a > b if strict else a >= b
        return ok | a.isna() | b.isna()

    word = "after" if strict else "not_before"
    return _rule(f"{later}_{word}_{earlier}", check)


def _embeds_shipment(column: str) -> pa.Check:
    """Rule: the shipment ID embedded in ``column`` equals the row's ``shipment_id``."""
    return _rule(
        f"{column}_belongs_to_shipment",
        lambda df: ids.embedded_shipment_ids(df[column]) == df["shipment_id"],
    )


def _scoped_id_matches(
    column: str, builder: Callable[[pd.Series, pd.Series], pd.Series]
) -> pa.Check:
    """Rule: ``column`` equals ``builder(shipment_id, sequence)``."""

    def check(df: pd.DataFrame) -> pd.Series:
        if df.empty:
            return pd.Series(True, index=df.index, dtype=bool)
        sequences = df["sequence"].clip(1, 99)  # out-of-range values fail their own check
        return df[column] == builder(df["shipment_id"], sequences)

    return _rule(f"{column}_matches_shipment_and_sequence", check)


def _status_matches_reasons(status_col: str, reasons_col: str) -> pa.Check:
    """Rule: the status equals the most severe status triggered by the reason codes."""

    def check(df: pd.DataFrame) -> pd.Series:
        def row_ok(status: str, reasons: Any) -> bool:
            codes = split_list(reasons)
            if not all(code in _REASON_ORDER for code in codes):
                return True  # reported by the reason-list column check
            return worst_status(codes) == status

        return pd.Series(
            [row_ok(s, r) for s, r in zip(df[status_col], df[reasons_col], strict=True)],
            index=df.index,
            dtype=bool,
        )

    return _rule(f"{status_col}_matches_{reasons_col}", check)


def _action_matches_status(action_col: str, status_col: str) -> pa.Check:
    """Rule: the action is the one :data:`STATUS_ACTION` prescribes for the status."""
    expected = {status.value: action.value for status, action in STATUS_ACTION.items()}
    return _rule(
        f"{action_col}_matches_{status_col}",
        lambda df: df[status_col].map(expected) == df[action_col],
    )


def _policy_id_matches(df: pd.DataFrame) -> pd.Series:
    """Rule: ``policy_id`` equals ``ids.policy_id(policy_code, version)``."""

    def build(code: str, version: int) -> str | None:
        try:
            return ids.policy_id(code, int(version))
        except (ValueError, TypeError):
            return None

    expected = [build(c, v) for c, v in zip(df["policy_code"], df["version"], strict=True)]
    return df["policy_id"] == pd.Series(expected, index=df.index, dtype=object)


def _versions_increase_with_date(df: pd.DataFrame) -> pd.Series:
    """Rule: within a policy code, a later ``effective_from`` has a higher version."""
    if df.empty:
        return pd.Series(True, index=df.index, dtype=bool)
    ordered = df.assign(_start=_as_datetime(df["effective_from"])).sort_values(
        ["policy_code", "_start"], kind="stable"
    )
    previous = ordered.groupby("policy_code")["version"].shift()
    ok = previous.isna() | (ordered["version"] > previous)
    return ok.reindex(df.index).astype(bool)


def _stop_timestamps_by_type(df: pd.DataFrame) -> pd.Series:
    """Rule: ORIGIN has only ``departed_at``, DESTINATION only ``arrived_at``, others both."""
    arrived, departed = df["arrived_at"].notna(), df["departed_at"].notna()
    is_origin = df["stop_type"] == StopType.ORIGIN
    is_destination = df["stop_type"] == StopType.DESTINATION
    ok = np.where(
        is_origin,
        ~arrived & departed,
        np.where(is_destination, arrived & ~departed, arrived & departed),
    )
    return pd.Series(ok, index=df.index, dtype=bool)


def _dwell_matches(df: pd.DataFrame) -> pd.Series:
    """Rule: ``dwell_min`` = ``departed_at - arrived_at`` in minutes, null when either is null."""
    both = df["arrived_at"].notna() & df["departed_at"].notna()
    expected = _minutes(df["departed_at"], df["arrived_at"])
    dwell = df["dwell_min"].astype("Float64")
    matches = (dwell == expected).fillna(False).astype(bool)
    return pd.Series(np.where(both, matches, df["dwell_min"].isna()), index=df.index, dtype=bool)


def _reading_id_matches(id_col: str, time_col: str) -> pa.Check:
    """Rule: ``id_col`` equals the reading ID built from ``logger_id`` and ``time_col``."""

    def check(df: pd.DataFrame) -> pd.Series:
        if df.empty:
            return pd.Series(True, index=df.index, dtype=bool)
        return df[id_col] == ids.reading_ids(df["logger_id"], df[time_col])

    return _rule(f"{id_col}_matches_logger_and_{time_col}", check)


def _coverage_matches_counts(df: pd.DataFrame) -> pd.Series:
    """Rule: ``coverage_pct`` = round(100 * reading_count / expected_reading_count, 2)."""
    expected = (100.0 * df["reading_count"] / df["expected_reading_count"]).round(2)
    counts_ok = df["reading_count"] <= df["expected_reading_count"]
    return counts_ok & ((df["coverage_pct"] - expected).abs() <= 1e-9)


def _list_lengths_match(df: pd.DataFrame) -> pd.Series:
    """Rule: excursion count = number of windows = number of durations; total = their sum."""

    def row_ok(count: int, total: int, windows: Any, durations: Any) -> bool:
        window_items, duration_items = split_list(windows), split_list(durations)
        if not all(item.isdigit() for item in duration_items):
            return True  # reported by the column format check
        return (
            len(window_items) == len(duration_items) == count
            and sum(int(d) for d in duration_items) == total
        )

    rows = zip(
        df["expected_excursion_count"],
        df["expected_total_excursion_min"],
        df["expected_excursion_windows"],
        df["expected_excursion_durations_min"],
        strict=True,
    )
    return pd.Series([row_ok(*row) for row in rows], index=df.index, dtype=bool)


def _windows_ordered(df: pd.DataFrame) -> pd.Series:
    """Rule: every expected excursion window ends at or after its start."""

    def row_ok(windows: Any) -> bool:
        for window in split_list(windows):
            start, _, end = window.partition("/")
            if end < start:  # ISO-8601 UTC strings sort chronologically
                return False
        return True

    return df["expected_excursion_windows"].map(row_ok).astype(bool)


# --------------------------------------------------------------------------------------------
# Table schemas
# --------------------------------------------------------------------------------------------

PRIMARY_KEYS: dict[TableName, tuple[str, ...]] = {
    TableName.PARTIES: ("party_id",),
    TableName.THRESHOLD_POLICIES: ("policy_id",),
    TableName.PRODUCTS: ("product_id",),
    TableName.PACKAGING: ("packaging_id",),
    TableName.LOTS: ("lot_id",),
    TableName.DEVICES: ("device_id",),
    TableName.SHIPMENTS: ("shipment_id",),
    TableName.SHIPMENT_LOTS: ("shipment_id", "lot_id"),
    TableName.LOGGERS: ("logger_id",),
    TableName.TEMPERATURE_READINGS: ("reading_id",),
    TableName.ROUTE_STOPS: ("stop_id",),
    TableName.CUSTODY_EVENTS: ("custody_event_id",),
    TableName.SHIPMENT_STATUS_EVENTS: ("status_event_id",),
    TableName.EXCURSION_REPORTS: ("report_id",),
    TableName.EXCURSION_REPORT_READINGS: ("report_id", "reading_id"),
    TableName.COMPLIANCE_DECISIONS: ("shipment_id",),
    TableName.COMPLIANCE_STATUS_EVENTS: ("status_event_id",),
    TableName.EXPECTED_OUTCOMES: ("shipment_id",),
    TableName.DEVICE_TRUTH: ("device_id",),
    TableName.ANOMALIES: ("anomaly_id", "shipment_id"),
}
"""Primary key columns of every table (also the sort order before writing)."""

TABLE_LAYERS: dict[TableName, Layer] = {
    **{
        t: Layer.RAW
        for t in (
            TableName.PARTIES,
            TableName.THRESHOLD_POLICIES,
            TableName.PRODUCTS,
            TableName.PACKAGING,
            TableName.LOTS,
            TableName.DEVICES,
            TableName.SHIPMENTS,
            TableName.SHIPMENT_LOTS,
            TableName.LOGGERS,
            TableName.TEMPERATURE_READINGS,
            TableName.ROUTE_STOPS,
            TableName.CUSTODY_EVENTS,
            TableName.SHIPMENT_STATUS_EVENTS,
        )
    },
    **{
        t: Layer.COMPLIANCE
        for t in (
            TableName.EXCURSION_REPORTS,
            TableName.EXCURSION_REPORT_READINGS,
            TableName.COMPLIANCE_DECISIONS,
            TableName.COMPLIANCE_STATUS_EVENTS,
        )
    },
    TableName.EXPECTED_OUTCOMES: Layer.GROUND_TRUTH,
    TableName.DEVICE_TRUTH: Layer.GROUND_TRUTH,
    TableName.ANOMALIES: Layer.GROUND_TRUTH,
}
"""Output layer of every table. Raw tables never contain ground-truth labels."""

FOREIGN_KEYS: tuple[tuple[TableName, str, TableName, str], ...] = (
    (TableName.PRODUCTS, "supplier_party_id", TableName.PARTIES, "party_id"),
    (TableName.LOTS, "product_id", TableName.PRODUCTS, "product_id"),
    (TableName.SHIPMENTS, "supplier_party_id", TableName.PARTIES, "party_id"),
    (TableName.SHIPMENTS, "carrier_party_id", TableName.PARTIES, "party_id"),
    (TableName.SHIPMENTS, "receiver_party_id", TableName.PARTIES, "party_id"),
    (TableName.SHIPMENTS, "planner_party_id", TableName.PARTIES, "party_id"),
    (TableName.SHIPMENTS, "packaging_id", TableName.PACKAGING, "packaging_id"),
    (TableName.SHIPMENTS, "declared_policy_id", TableName.THRESHOLD_POLICIES, "policy_id"),
    (TableName.SHIPMENT_LOTS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.SHIPMENT_LOTS, "lot_id", TableName.LOTS, "lot_id"),
    (TableName.LOGGERS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.LOGGERS, "device_id", TableName.DEVICES, "device_id"),
    (TableName.TEMPERATURE_READINGS, "logger_id", TableName.LOGGERS, "logger_id"),
    (TableName.TEMPERATURE_READINGS, "device_id", TableName.DEVICES, "device_id"),
    (TableName.ROUTE_STOPS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.ROUTE_STOPS, "party_id", TableName.PARTIES, "party_id"),
    (TableName.CUSTODY_EVENTS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.CUSTODY_EVENTS, "stop_id", TableName.ROUTE_STOPS, "stop_id"),
    (TableName.CUSTODY_EVENTS, "from_party_id", TableName.PARTIES, "party_id"),
    (TableName.CUSTODY_EVENTS, "to_party_id", TableName.PARTIES, "party_id"),
    (TableName.SHIPMENT_STATUS_EVENTS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.SHIPMENT_STATUS_EVENTS, "actor_party_id", TableName.PARTIES, "party_id"),
    (TableName.EXCURSION_REPORTS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.EXCURSION_REPORTS, "logger_id", TableName.LOGGERS, "logger_id"),
    (TableName.EXCURSION_REPORTS, "policy_id", TableName.THRESHOLD_POLICIES, "policy_id"),
    (TableName.EXCURSION_REPORT_READINGS, "report_id", TableName.EXCURSION_REPORTS, "report_id"),
    (
        TableName.EXCURSION_REPORT_READINGS,
        "reading_id",
        TableName.TEMPERATURE_READINGS,
        "reading_id",
    ),
    (TableName.COMPLIANCE_DECISIONS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (
        TableName.COMPLIANCE_DECISIONS,
        "applied_policy_id",
        TableName.THRESHOLD_POLICIES,
        "policy_id",
    ),
    (TableName.COMPLIANCE_STATUS_EVENTS, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.COMPLIANCE_STATUS_EVENTS, "actor_party_id", TableName.PARTIES, "party_id"),
    (TableName.EXPECTED_OUTCOMES, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
    (TableName.DEVICE_TRUTH, "device_id", TableName.DEVICES, "device_id"),
    (TableName.ANOMALIES, "shipment_id", TableName.SHIPMENTS, "shipment_id"),
)
"""Every single-column foreign key: (child table, child column, parent table, parent column).

ID lists (``excursion_report_ids``, ``affected_record_ids``) and the logical ``policy_code``
references (resolved by effective date) are documented in the data dictionary instead.
"""


def _table(
    name: TableName, columns: dict[str, pa.Column], checks: Iterable[pa.Check] = ()
) -> pa.DataFrameSchema:
    """Build a strict, ordered table schema keyed on the table's primary key."""
    return pa.DataFrameSchema(
        columns,
        checks=list(checks),
        unique=list(PRIMARY_KEYS[name]),
        strict=True,
        ordered=True,
        name=name.value,
    )


def _status_events(name: TableName, allowed: frozenset[ShipmentStatus]) -> pa.DataFrameSchema:
    """Status-event history schema, restricted to the statuses its writer may append."""
    status = _column(
        str,
        [pa.Check.isin(sorted(s.value for s in allowed), error="allowed_status_for_layer")],
        role="enum",
    )
    return _table(
        name,
        {
            "status_event_id": _id("status_event"),
            "shipment_id": _id("shipment"),
            "sequence": _int(1, 99),
            "status": status,
            "event_at": _timestamp(),
            "actor_party_id": _id("party"),
        },
        [
            _scoped_id_matches("status_event_id", ids.status_event_ids),
            _unique_together("shipment_id", "sequence"),
        ],
    )


SCHEMAS: dict[TableName, pa.DataFrameSchema] = {
    TableName.PARTIES: _table(
        TableName.PARTIES,
        {
            "party_id": _id("party"),
            "party_type": _enum(PartyType),
            "name": _text(80, unique=True),
            "city": _text(40),
            "active_from": _date(),
        },
    ),
    TableName.THRESHOLD_POLICIES: _table(
        TableName.THRESHOLD_POLICIES,
        {
            "policy_id": _id("policy"),
            "policy_code": _enum(PolicyCode),
            "version": _int(1),
            "min_c": _celsius(),
            "max_c": _celsius(),
            "abs_min_c": _celsius(),
            "abs_max_c": _celsius(),
            "max_single_excursion_min": _int(1),
            "max_cumulative_excursion_min": _int(1),
            "max_reading_gap_min": _int(1),
            "min_coverage_pct": _float(0.0, 100.0, min_inclusive=False),
            "effective_from": _date(),
            "effective_to": _date(nullable=True),
        },
        [
            _rule("policy_id_matches_code_and_version", _policy_id_matches),
            _unique_together("policy_code", "version"),
            _rule(
                "limits_ordered",
                lambda df: (
                    (df["abs_min_c"] < df["min_c"])
                    & (df["min_c"] < df["max_c"])
                    & (df["max_c"] < df["abs_max_c"])
                ),
            ),
            _rule(
                "cumulative_allowance_ge_single",
                lambda df: df["max_cumulative_excursion_min"] >= df["max_single_excursion_min"],
            ),
            _rule(
                "effective_to_after_effective_from",
                lambda df: (
                    df["effective_to"].isna()
                    | (_as_datetime(df["effective_to"]) > _as_datetime(df["effective_from"]))
                ),
            ),
            _rule("no_overlapping_versions", lambda df: ~overlapping_policy_versions(df)),
            _rule("versions_increase_with_effective_from", _versions_increase_with_date),
        ],
    ),
    TableName.PRODUCTS: _table(
        TableName.PRODUCTS,
        {
            "product_id": _id("product"),
            "name": _text(80, unique=True),
            "category": _enum(ProductCategory),
            "supplier_party_id": _id("party"),
            "policy_code": _enum(PolicyCode),
            "shelf_life_days": _int(1),
        },
    ),
    TableName.PACKAGING: _table(
        TableName.PACKAGING,
        {
            "packaging_id": _id("packaging"),
            "name": _text(80, unique=True),
            "packaging_type": _enum(PackagingType),
            "supported_policy_code": _enum(PolicyCode),
            "setpoint_c": _celsius(),
            "max_hold_hours": _float(0.0, 10_000.0, min_inclusive=False),
            "thermal_response": _float(0.0, 1.0),
        },
    ),
    TableName.LOTS: _table(
        TableName.LOTS,
        {
            "lot_id": _id("lot"),
            "product_id": _id("product"),
            "manufactured_on": _date(),
            "expires_on": _date(),
            "quantity_units": _int(1),
            "lot_status": _enum(LotStatus),
        },
        [
            _rule(
                "expires_on_after_manufactured_on",
                lambda df: _as_datetime(df["expires_on"]) > _as_datetime(df["manufactured_on"]),
            )
        ],
    ),
    TableName.DEVICES: _table(
        TableName.DEVICES,
        {
            "device_id": _id("device"),
            "model": _text(40),
            "serial_number": _text(40, unique=True),
            "calibrated_on": _date(),
            "calibration_due_on": _date(),
            "uncertainty_c": _float(0.0, 1.0, min_inclusive=False),
            "reading_interval_min": _int(1),
        },
        [
            _rule(
                "calibration_due_on_after_calibrated_on",
                lambda df: (
                    _as_datetime(df["calibration_due_on"]) > _as_datetime(df["calibrated_on"])
                ),
            )
        ],
    ),
    TableName.SHIPMENTS: _table(
        TableName.SHIPMENTS,
        {
            "shipment_id": _id("shipment"),
            "supplier_party_id": _id("party"),
            "carrier_party_id": _id("party"),
            "receiver_party_id": _id("party"),
            "planner_party_id": _id("party"),
            "packaging_id": _id("packaging"),
            "declared_policy_id": _id("policy"),
            "planned_departure_at": _timestamp(),
            "planned_arrival_at": _timestamp(),
            "actual_departure_at": _timestamp(),
            "actual_arrival_at": _timestamp(),
            "carrier_reported_status": _enum(CarrierReportedStatus),
        },
        [
            _not_before("planned_arrival_at", "planned_departure_at", strict=True),
            _not_before("actual_arrival_at", "actual_departure_at", strict=True),
            _rule(
                "parties_distinct",
                lambda df: (
                    df[
                        [
                            "supplier_party_id",
                            "carrier_party_id",
                            "receiver_party_id",
                            "planner_party_id",
                        ]
                    ].nunique(axis=1)
                    == 4
                ),
            ),
        ],
    ),
    TableName.SHIPMENT_LOTS: _table(
        TableName.SHIPMENT_LOTS,
        {
            "shipment_id": _id("shipment"),
            "lot_id": _id("lot"),
            "quantity_units": _int(1),
        },
    ),
    TableName.LOGGERS: _table(
        TableName.LOGGERS,
        {
            "logger_id": _id("logger"),
            "shipment_id": _id("shipment", unique=True),
            "device_id": _id("device"),
            "assigned_at": _timestamp(),
            "unassigned_at": _timestamp(),
        },
        [
            _rule(
                "logger_id_matches_shipment",
                lambda df: df["logger_id"] == ids.logger_ids(df["shipment_id"]),
            ),
            _not_before("unassigned_at", "assigned_at", strict=True),
        ],
    ),
    TableName.TEMPERATURE_READINGS: _table(
        TableName.TEMPERATURE_READINGS,
        {
            "reading_id": _id("reading"),
            "logger_id": _id("logger"),
            "device_id": _id("device"),
            "recorded_at": _timestamp(),
            "temperature_c": _celsius(),
        },
        [
            _reading_id_matches("reading_id", "recorded_at"),
            _unique_together("logger_id", "recorded_at"),
        ],
    ),
    TableName.ROUTE_STOPS: _table(
        TableName.ROUTE_STOPS,
        {
            "stop_id": _id("route_stop"),
            "shipment_id": _id("shipment"),
            "sequence": _int(1, 99),
            "stop_type": _enum(StopType),
            "party_id": _id("party"),
            "arrived_at": _timestamp(nullable=True),
            "departed_at": _timestamp(nullable=True),
            "dwell_min": _int(0, nullable=True),
        },
        [
            _scoped_id_matches("stop_id", ids.route_stop_ids),
            _unique_together("shipment_id", "sequence"),
            _rule("timestamps_present_by_stop_type", _stop_timestamps_by_type),
            _not_before("departed_at", "arrived_at", strict=False),
            _rule("dwell_min_matches_timestamps", _dwell_matches),
        ],
    ),
    TableName.CUSTODY_EVENTS: _table(
        TableName.CUSTODY_EVENTS,
        {
            "custody_event_id": _id("custody_event"),
            "shipment_id": _id("shipment"),
            "stop_id": _id("route_stop"),
            "sequence": _int(1, 99),
            "event_type": _enum(CustodyEventType),
            "from_party_id": _id("party"),
            "to_party_id": _id("party"),
            "event_at": _timestamp(),
        },
        [
            _scoped_id_matches("custody_event_id", ids.custody_event_ids),
            _unique_together("shipment_id", "sequence"),
            _embeds_shipment("stop_id"),
            _rule(
                "from_party_differs_from_to_party",
                lambda df: df["from_party_id"] != df["to_party_id"],
            ),
        ],
    ),
    TableName.SHIPMENT_STATUS_EVENTS: _status_events(
        TableName.SHIPMENT_STATUS_EVENTS, GENERATOR_STATUSES
    ),
    TableName.EXCURSION_REPORTS: _table(
        TableName.EXCURSION_REPORTS,
        {
            "report_id": _id("excursion_report"),
            "shipment_id": _id("shipment"),
            "logger_id": _id("logger"),
            "policy_id": _id("policy"),
            "direction": _enum(ExcursionDirection),
            "started_at": _timestamp(),
            "ended_at": _timestamp(),
            "duration_min": _int(1),
            "peak_c": _celsius(),
            "reading_count": _int(1),
            "first_reading_id": _id("reading"),
            "last_reading_id": _id("reading"),
            "breach_type": _enum(BreachType),
        },
        [
            _embeds_shipment("logger_id"),
            _not_before("ended_at", "started_at", strict=False),
            _rule(
                "duration_exceeds_window",
                lambda df: df["duration_min"] > _minutes(df["ended_at"], df["started_at"]),
            ),
            _rule(
                "single_reading_iff_zero_length_window",
                lambda df: (df["reading_count"] == 1) == (df["ended_at"] == df["started_at"]),
            ),
            _reading_id_matches("first_reading_id", "started_at"),
            _reading_id_matches("last_reading_id", "ended_at"),
        ],
    ),
    TableName.EXCURSION_REPORT_READINGS: _table(
        TableName.EXCURSION_REPORT_READINGS,
        {
            "report_id": _id("excursion_report"),
            "reading_id": _id("reading", unique=True),
        },
    ),
    TableName.COMPLIANCE_DECISIONS: _table(
        TableName.COMPLIANCE_DECISIONS,
        {
            "shipment_id": _id("shipment"),
            "applied_policy_id": _id("policy"),
            "policy_source": _enum(PolicySource),
            "status": _enum(ComplianceStatus),
            "reason_codes": _reason_list(),
            "reading_count": _int(0),
            "expected_reading_count": _int(1),
            "coverage_pct": _float(0.0, 100.0),
            "max_gap_min": _int(0),
            "total_excursion_min": _int(0),
            "excursion_report_ids": _id_list(ids.ID_PATTERNS["excursion_report"]),
            "decided_action": _enum(DecidedAction),
        },
        [
            _action_matches_status("decided_action", "status"),
            _status_matches_reasons("status", "reason_codes"),
            _rule("coverage_pct_matches_counts", _coverage_matches_counts),
            _rule(
                "excursion_report_ids_iff_excursion_minutes",
                lambda df: df["excursion_report_ids"].isna() == (df["total_excursion_min"] == 0),
            ),
            _rule(
                "policy_source_matches_mismatch_flag",
                lambda df: (
                    (df["policy_source"] == PolicySource.PRODUCT_OVERRIDE)
                    == df["reason_codes"]
                    .map(lambda r: ReasonCode.POLICY_MISMATCH_DECLARED.value in split_list(r))
                    .astype(bool)
                ),
            ),
        ],
    ),
    TableName.COMPLIANCE_STATUS_EVENTS: _status_events(
        TableName.COMPLIANCE_STATUS_EVENTS, ENGINE_STATUSES
    ),
    TableName.EXPECTED_OUTCOMES: _table(
        TableName.EXPECTED_OUTCOMES,
        {
            "shipment_id": _id("shipment"),
            "scenario": _enum(Scenario),
            "issue_type": _enum(IssueType, nullable=True),
            "expected_status": _enum(ComplianceStatus),
            "expected_action": _enum(DecidedAction),
            "expected_reason_codes": _reason_list(),
            "expected_excursion_count": _int(0),
            "expected_total_excursion_min": _int(0),
            "expected_excursion_windows": _pattern_cell(_WINDOW_LIST),
            "expected_excursion_durations_min": _pattern_cell(_INT_LIST),
        },
        [
            _action_matches_status("expected_action", "expected_status"),
            _status_matches_reasons("expected_status", "expected_reason_codes"),
            _rule("excursion_counts_and_total_consistent", _list_lengths_match),
            _rule("excursion_windows_ordered", _windows_ordered),
        ],
    ),
    TableName.DEVICE_TRUTH: _table(
        TableName.DEVICE_TRUTH,
        {
            "device_id": _id("device"),
            # |offset| <= the device's uncertainty_c (< 1.0); checked against devices by tests.
            "systematic_offset_c": _float(-1.0, 1.0),
        },
    ),
    TableName.ANOMALIES: _table(
        TableName.ANOMALIES,
        {
            "anomaly_id": _id("anomaly"),
            "shipment_id": _id("shipment"),
            "issue_type": _enum(IssueType),
            "description": _text(300),
            "affected_record_ids": _id_list(_ANY_ID),
            "removed_record_ids": _id_list(_ANY_ID),
            "expected_status": _enum(ComplianceStatus),
            "expected_reason_codes": _reason_list(),
            "expected_excursion_durations_min": _pattern_cell(_INT_LIST),
            "idle_min": _int(0, nullable=True),
        },
        [
            _status_matches_reasons("expected_status", "expected_reason_codes"),
            _rule(
                "idle_min_iff_back_to_back_pair",
                lambda df: (
                    df["idle_min"].notna()
                    == (df["issue_type"] == IssueType.EXCURSION_ACROSS_UNRELATED_SHIPMENTS.value)
                ),
            ),
        ],
    ),
}
"""The pandera schema of every table, keyed by :class:`TableName`."""


def validate(table: TableName | str, frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a table against its schema, collecting every failure.

    Args:
        table: Table name (enum member or its string value).
        frame: The table to validate.

    Returns:
        The validated frame (unchanged).

    Raises:
        SchemaValidationError: If the table name is unknown or any check fails. The message
            lists each failing column/check with its number of failing cases.
    """
    try:
        name = TableName(table)
    except ValueError as exc:
        raise SchemaValidationError(f"Unknown table {table!r}") from exc
    try:
        return SCHEMAS[name].validate(frame, lazy=True)
    except pa.errors.SchemaErrors as exc:
        raise SchemaValidationError(
            f"Table {name.value!r} failed validation: {_summarise(exc.failure_cases)}"
        ) from exc


def _summarise(failure_cases: pd.DataFrame, examples: int = 3) -> str:
    """Summarise pandera failure cases, one entry per failing check.

    Column checks read ``column: check (n cases, e.g. [values])``. Table-level rules (reported by
    pandera once per column of each failing row) read ``<table>: check (n rows, e.g. [row index])``.
    """
    table_level = failure_cases["schema_context"] == "DataFrameSchema"
    is_rule = table_level & failure_cases["check"].isin(
        [c.name for schema in SCHEMAS.values() for c in schema.checks]
    )
    parts = []
    for check, group in failure_cases[is_rule].groupby("check", sort=True):
        rows = sorted({str(i) for i in group["index"]})
        parts.append(f"<table>: {check} ({len(rows)} rows, e.g. {rows[:examples]})")
    cases = failure_cases[~is_rule].fillna({"column": "<table>"})
    for (column, check), group in cases.groupby(["column", "check"], sort=True):
        sample = sorted({str(v) for v in group["failure_case"]})[:examples]
        parts.append(f"{column}: {check} ({len(group)} cases, e.g. {sample})")
    return "; ".join(sorted(parts))
