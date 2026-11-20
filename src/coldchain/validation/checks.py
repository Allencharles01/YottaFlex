"""The validation checks: PK, FK, enums, schema, duplicates, dates, lifecycle, reconciliation.

Every check is a pure function of a :class:`~coldchain.validation.context.ValidationContext`
that returns an :class:`Outcome`: how many records it examined and one *finding* per failing
record (``record_id``, ``shipment_id``, ``detail``). A shipment-level problem uses the shipment
ID as its record. The :data:`CHECKS` registry gives each check an ID, a category, a tier, its
main table, the tables it needs and a one-line description.

Two tiers (DECISIONS.md #84):

* **INTEGRITY** checks must always pass, on clean and on injected data alike;
* **BUSINESS_RULE** checks cover the rules an injected edge case may break on purpose (device
  attribution, stream completeness, custody continuity, the planner's declaration, the carrier
  claim). On the clean baseline they find nothing; on injected data every finding must be
  explained by ``coldchain evaluate``.

The suite never imports the compliance engine: every derived value is recomputed in
:mod:`coldchain.validation.context`.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import numpy as np
import pandas as pd
import pandera.pandas as pa

from coldchain import ids
from coldchain.enums import (
    BreachType,
    CarrierReportedStatus,
    CheckCategory,
    CheckTier,
    CustodyEventType,
    DecidedAction,
    ExcursionDirection,
    Layer,
    LotStatus,
    PartyType,
    PolicySource,
    ReasonCode,
    ShipmentStatus,
    StopType,
    TableName,
)
from coldchain.schemas import (
    FOREIGN_KEYS,
    LIST_SEPARATOR,
    PRIMARY_KEYS,
    SCHEMAS,
    TABLE_LAYERS,
    TIMESTAMP_DTYPE,
)
from coldchain.validation.context import (
    INTERMEDIATE_STOP_TYPES,
    MINUTE,
    ValidationContext,
    minutes_between,
    padded_max_gaps,
)

T = TableName

FINDING_FIELDS: tuple[str, ...] = ("record_id", "shipment_id", "detail")
"""Columns of the findings frame a check returns."""

VALIDATED_TABLES: tuple[TableName, ...] = tuple(
    t for t in TableName if TABLE_LAYERS[t] in (Layer.RAW, Layer.COMPLIANCE)
)
"""Every table the suite may judge: the raw input and the engine's output."""

RULE_HAS_LOGGER = "rule.shipments.has_logger"
RULE_DEVICE_OVERLAP = "rule.loggers.no_device_overlap"
RULE_ASSIGNED_DEVICE = "rule.temperature_readings.assigned_device"
RULE_COMPLETE_STREAM = "rule.loggers.complete_stream"
RULE_CUSTODY_CHAIN = "rule.custody_events.continuous_chain"
RULE_CUSTODY_COVERAGE = "rule.route_stops.custody_coverage"
RULE_DECLARED_POLICY = "rule.shipments.declared_policy"
RULE_CARRIER_CLAIM = "rule.compliance_decisions.carrier_claim"

_RAW_TRANSITIONS = frozenset(
    {
        (ShipmentStatus.PLANNED.value, ShipmentStatus.PICKED_UP.value),
        (ShipmentStatus.PICKED_UP.value, ShipmentStatus.IN_TRANSIT.value),
        (ShipmentStatus.IN_TRANSIT.value, ShipmentStatus.AT_STOP.value),
        (ShipmentStatus.AT_STOP.value, ShipmentStatus.IN_TRANSIT.value),
        (ShipmentStatus.IN_TRANSIT.value, ShipmentStatus.DELIVERED.value),
    }
)
"""Legal consecutive pairs of the generator's lifecycle (data_dictionary.md, Lifecycle states)."""

_ENGINE_EVENTS: dict[str, str] = {
    DecidedAction.RELEASE.value: "UNDER_REVIEW>RELEASED",
    DecidedAction.QUARANTINE.value: "UNDER_REVIEW>QUARANTINED",
    DecidedAction.REVIEW.value: "UNDER_REVIEW",
}
"""The engine's appended statuses per decided action (compliance_rules.md §4)."""


# --------------------------------------------------------------------------------------------
# Framework
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What one check found.

    Attributes:
        checked: Number of records (rows, shipments, ...) the check examined.
        findings: One row per failing record, with the columns :data:`FINDING_FIELDS`.
    """

    checked: int
    findings: pd.DataFrame


@dataclass(frozen=True)
class Check:
    """A registered validation check.

    Attributes:
        check_id: Stable dotted ID, e.g. ``fk.loggers.device_id``.
        category: What the check looks at.
        tier: INTEGRITY (must pass) or BUSINESS_RULE (findings must be explained).
        table: The table its findings refer to.
        needs: Tables that must be present for the check to run (else it is SKIPPED).
        description: One-line statement of the rule.
        run: The check itself.
    """

    check_id: str
    category: CheckCategory
    tier: CheckTier
    table: TableName
    needs: tuple[TableName, ...]
    description: str
    run: Callable[[ValidationContext], Outcome]


def empty_findings() -> pd.DataFrame:
    """Return an empty findings frame."""
    return pd.DataFrame({name: pd.Series(dtype=object) for name in FINDING_FIELDS})


def record_ids(table: TableName, frame: pd.DataFrame) -> pd.Series:
    """Return each row's primary key as text (composite keys joined with ``/``)."""
    keys = list(PRIMARY_KEYS[table])
    text = frame[keys[0]].astype(str)
    for key in keys[1:]:
        text = text + "/" + frame[key].astype(str)
    return text


def shipment_of(frame: pd.DataFrame) -> pd.Series:
    """Return the shipment each row belongs to (missing for master data)."""
    if "shipment_id" in frame.columns:
        return frame["shipment_id"].astype(object)
    for column in ("logger_id", "reading_id"):
        if column in frame.columns:
            return ids.embedded_shipment_ids(frame[column]).astype(object)
    return pd.Series(None, index=frame.index, dtype=object)


def findings_frame(records: Iterable, shipments: Iterable, details: Iterable) -> pd.DataFrame:
    """Build a findings frame from aligned record IDs, shipment IDs and details."""
    frame = pd.DataFrame(
        {
            "record_id": pd.Series(list(records), dtype=object),
            "shipment_id": pd.Series(list(shipments), dtype=object),
            "detail": pd.Series(list(details), dtype=object),
        }
    )
    return frame.where(frame.notna(), None)


def found(
    table: TableName, frame: pd.DataFrame, mask: pd.Series, detail: str | pd.Series
) -> pd.DataFrame:
    """Findings for the rows of ``frame`` (a ``table`` frame) where ``mask`` is true."""
    mask = mask.fillna(False).astype(bool)
    failing = frame[mask]
    details = detail[mask] if isinstance(detail, pd.Series) else [detail] * len(failing)
    return findings_frame(record_ids(table, failing), shipment_of(failing), details)


def problems(index: pd.Index, pairs: list[tuple[str, pd.Series]]) -> tuple[pd.Series, pd.Series]:
    """Combine several failure masks into one mask and a ``; ``-joined detail per row.

    Args:
        index: Index of the rows the masks are aligned with.
        pairs: ``(label, failure mask)`` per rule; missing values count as "no failure".

    Returns:
        The combined mask, and the detail (empty for passing rows).
    """
    labels = [label for label, _ in pairs]
    masks = np.column_stack(
        [mask.reindex(index).fillna(False).to_numpy(dtype=bool) for _, mask in pairs]
    )
    failing = masks.any(axis=1)
    detail = np.full(len(index), "", dtype=object)
    detail[failing] = [
        "; ".join(label for label, hit in zip(labels, row, strict=True) if hit)
        for row in masks[failing]
    ]
    return pd.Series(failing, index=index), pd.Series(detail, index=index)


def _has_code(reason_codes: pd.Series, code: ReasonCode) -> pd.Series:
    """Whether each ``|``-list of reason codes contains ``code``."""
    return (
        reason_codes.fillna("")
        .str.split(LIST_SEPARATOR, regex=False)
        .map(lambda items: code.value in items)
    )


def _epoch_minutes(values: pd.Series) -> pd.Series:
    """Whole minutes since the Unix epoch (for reading-grid checks)."""
    return (values - pd.Timestamp(0, tz="UTC")) // MINUTE


def _utc_midnight(dates: pd.Series) -> pd.Series:
    """Convert ``datetime.date`` values to UTC-midnight timestamps."""
    return pd.to_datetime(dates).dt.tz_localize("UTC")


# --------------------------------------------------------------------------------------------
# Generic structural checks (one per table / relationship)
# --------------------------------------------------------------------------------------------


def _schema_check(table: TableName) -> Callable[[ValidationContext], Outcome]:
    def run(ctx: ValidationContext) -> Outcome:
        frame = ctx.tables[table]
        try:
            SCHEMAS[table].validate(frame, lazy=True)
        except pa.errors.SchemaErrors as exc:
            cases = exc.failure_cases
        else:
            return Outcome(len(frame), empty_findings())
        position = frame.index.get_indexer(pd.to_numeric(cases["index"], errors="coerce"))
        located = position >= 0
        records = record_ids(table, frame).to_numpy()
        shipments = shipment_of(frame).to_numpy()
        detail = cases["column"].fillna("<table>").astype(str) + ": " + cases["check"].astype(str)
        result = findings_frame(
            np.where(located, records[position], table.value),
            np.where(located, shipments[position], None),
            detail,
        )
        return Outcome(len(frame), result.drop_duplicates(ignore_index=True))

    return run


def _quoted(columns: Iterable[str]) -> list[str]:
    return [f'"{column}"' for column in columns]


def duplicate_primary_keys(ctx: ValidationContext) -> pd.DataFrame:
    """Every duplicated primary key of every present table, from one batched DuckDB query.

    Returns:
        ``table``, ``key`` (the key as text, composite parts joined with ``/``) and ``n``.
    """
    if "primary_keys" not in ctx.memo:
        parts = []
        for table in VALIDATED_TABLES:
            if ctx.has(table):
                keys = _quoted(PRIMARY_KEYS[table])
                key_text = " || '/' || ".join(keys)
                not_null = " AND ".join(f"{key} IS NOT NULL" for key in keys)
                parts.append(
                    f"SELECT '{table.value}' AS \"table\", {key_text} AS key, count(*) AS n "
                    f'FROM "{table.value}" WHERE {not_null} '
                    f"GROUP BY {', '.join(keys)} HAVING count(*) > 1"
                )
        ctx.memo["primary_keys"] = ctx.sql(" UNION ALL ".join(parts))
    return ctx.memo["primary_keys"]


def missing_foreign_keys(ctx: ValidationContext) -> pd.DataFrame:
    """Every dangling foreign-key value, from one batched DuckDB anti-join query.

    Returns:
        ``child``, ``column`` and ``value`` (each distinct value without a parent row).
    """
    if "foreign_keys" not in ctx.memo:
        parts = [
            f"SELECT DISTINCT '{child.value}' AS child, '{column}' AS \"column\", "
            f'c."{column}" AS value FROM "{child.value}" c '
            f'LEFT JOIN "{parent.value}" p ON c."{column}" = p."{parent_column}" '
            f'WHERE c."{column}" IS NOT NULL AND p."{parent_column}" IS NULL'
            for child, column, parent, parent_column in FOREIGN_KEYS
            if ctx.has(child, parent) and {child, parent} <= set(VALIDATED_TABLES)
        ]
        ctx.memo["foreign_keys"] = ctx.sql(" UNION ALL ".join(parts))
    return ctx.memo["foreign_keys"]


def _primary_key_check(table: TableName) -> Callable[[ValidationContext], Outcome]:
    keys = list(PRIMARY_KEYS[table])

    def run(ctx: ValidationContext) -> Outcome:
        frame = ctx.tables[table]
        duplicated = duplicate_primary_keys(ctx)
        counts = duplicated[duplicated["table"] == table.value].set_index("key")["n"]
        n = record_ids(table, frame).map(counts)
        nulls = frame[keys].isna().any(axis=1)
        detail = pd.Series("null primary key", index=frame.index, dtype=object)
        is_dup = n.notna() & ~nulls
        detail[is_dup] = "primary key appears " + n[is_dup].astype("int64").astype(str) + "x"
        return Outcome(len(frame), found(table, frame, nulls | is_dup, detail))

    return run


def _foreign_key_check(
    child: TableName, column: str, parent: TableName, parent_column: str
) -> Callable[[ValidationContext], Outcome]:
    def run(ctx: ValidationContext) -> Outcome:
        dangling = missing_foreign_keys(ctx)
        missing = dangling.loc[
            (dangling["child"] == child.value) & (dangling["column"] == column), "value"
        ]
        frame = ctx.tables[child]
        mask = frame[column].isin(missing)
        detail = f"{column} not in {parent.value}.{parent_column}: " + frame[column].astype(str)
        return Outcome(int(frame[column].notna().sum()), found(child, frame, mask, detail))

    return run


def enum_columns(table: TableName) -> dict[str, tuple[frozenset[str], bool]]:
    """Allowed values of every categorical column, read from the table's schema.

    Args:
        table: The table.

    Returns:
        Column → (allowed values, whether the cell is a ``|``-list).
    """
    result: dict[str, tuple[frozenset[str], bool]] = {}
    for name, column in SCHEMAS[table].columns.items():
        role = (column.metadata or {}).get("role")
        if role == "enum":
            allowed = next(
                check.statistics["allowed_values"]
                for check in column.checks
                if "allowed_values" in (check.statistics or {})
            )
            result[name] = (frozenset(allowed), False)
        elif role == "enum_list":
            result[name] = (frozenset(code.value for code in ReasonCode), True)
    return result


def _enum_check(table: TableName) -> Callable[[ValidationContext], Outcome]:
    columns = enum_columns(table)

    def run(ctx: ValidationContext) -> Outcome:
        frame = ctx.tables[table]
        pairs = []
        for name, (allowed, is_list) in columns.items():
            values = frame[name]
            if is_list:
                bad = (
                    values.dropna()
                    .str.split(LIST_SEPARATOR, regex=False)
                    .map(lambda items, ok=allowed: not set(items) <= ok)
                )
            else:
                bad = values.notna() & ~values.isin(allowed)
            pairs.append((f"{name} not an allowed value", bad))
        mask, detail = problems(frame.index, pairs)
        return Outcome(len(frame), found(table, frame, mask, detail))

    return run


def _duplicate_check(table: TableName, columns: tuple[str, ...]) -> Callable:
    def run(ctx: ValidationContext) -> Outcome:
        frame = ctx.tables[table]
        keys = frame[list(columns)]
        complete = keys.notna().all(axis=1)
        mask = complete & keys.duplicated(keep=False)
        detail = f"duplicate ({', '.join(columns)})"
        return Outcome(int(complete.sum()), found(table, frame, mask, detail))

    return run


# --------------------------------------------------------------------------------------------
# Master data and shipments
# --------------------------------------------------------------------------------------------


def check_party_cities(ctx: ValidationContext) -> Outcome:
    """Parties are in reference cities; warehouses in hubs; suppliers in manufacturing cities."""
    parties = ctx.tables[T.PARTIES]
    limits = ctx.limits
    mask, detail = problems(
        parties.index,
        [
            ("city not in the reference locations", ~parties["city"].isin(limits.cities)),
            (
                "WAREHOUSE party outside a hub city",
                (parties["party_type"] == PartyType.WAREHOUSE)
                & ~parties["city"].isin(limits.hub_cities),
            ),
            (
                "SUPPLIER party outside a manufacturing-cluster city",
                (parties["party_type"] == PartyType.SUPPLIER)
                & ~parties["city"].isin(limits.manufacturing_cities),
            ),
        ],
    )
    return Outcome(len(parties), found(T.PARTIES, parties, mask, detail))


def check_device_interval(ctx: ValidationContext) -> Outcome:
    """Every device records at the configured reading interval."""
    devices = ctx.tables[T.DEVICES]
    expected = ctx.limits.reading_interval_min
    mask = devices["reading_interval_min"] != expected
    detail = f"reading_interval_min differs from the config ({expected})"
    return Outcome(len(devices), found(T.DEVICES, devices, mask, detail))


def check_product_supplier_role(ctx: ValidationContext) -> Outcome:
    """A product's supplier is a SUPPLIER party."""
    products = ctx.tables[T.PRODUCTS]
    types = ctx.tables[T.PARTIES].set_index("party_id")["party_type"]
    role = products["supplier_party_id"].map(types)
    mask = role.notna() & (role != PartyType.SUPPLIER)
    return Outcome(len(products), found(T.PRODUCTS, products, mask, "supplier is not a SUPPLIER"))


def check_shipment_party_roles(ctx: ValidationContext) -> Outcome:
    """A shipment's supplier, carrier, receiver and planner parties have those types."""
    shipments = ctx.tables[T.SHIPMENTS]
    types = ctx.tables[T.PARTIES].set_index("party_id")["party_type"]
    pairs = []
    for column, party_type in (
        ("supplier_party_id", PartyType.SUPPLIER),
        ("carrier_party_id", PartyType.CARRIER),
        ("receiver_party_id", PartyType.RECEIVER),
        ("planner_party_id", PartyType.PLANNER),
    ):
        role = shipments[column].map(types)
        pairs.append((f"{column} is not a {party_type.value}", role.notna() & (role != party_type)))
    mask, detail = problems(shipments.index, pairs)
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, detail))


def check_shipment_trip_dates(ctx: ValidationContext) -> Outcome:
    """Planned departure in the date window; arrivals after departures; trip within range."""
    shipments = ctx.tables[T.SHIPMENTS]
    limits = ctx.limits
    departure = shipments["planned_departure_at"]
    hours = minutes_between(shipments["planned_arrival_at"], departure) / 60.0
    low, high = limits.trip_hours
    mask, detail = problems(
        shipments.index,
        [
            (
                "planned_departure_at outside the date window",
                (departure < limits.window_start) | (departure >= limits.window_end),
            ),
            ("planned arrival not after planned departure", hours <= 0),
            (f"planned trip outside [{low:g}, {high:g}] h", (hours < low) | (hours > high)),
            (
                "actual arrival not after actual departure",
                shipments["actual_arrival_at"] <= shipments["actual_departure_at"],
            ),
        ],
    )
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, detail))


def check_shipment_lots(ctx: ValidationContext) -> Outcome:
    """Every shipment has lots of one policy code and one supplier, the shipment's supplier."""
    shipments = ctx.tables[T.SHIPMENTS]
    classes = shipments[["shipment_id", "supplier_party_id"]].merge(
        ctx.shipment_classes, on="shipment_id", how="left"
    )
    classes.index = shipments.index
    mask, detail = problems(
        shipments.index,
        [
            ("no lots", classes["code_count"].isna()),
            ("lots of more than one policy code", classes["code_count"] > 1),
            ("lots of more than one supplier", classes["supplier_count"] > 1),
            (
                "products' supplier differs from the shipment supplier",
                classes["code_count"].notna()
                & (classes["product_supplier_id"] != classes["supplier_party_id"]),
            ),
        ],
    )
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, detail))


def check_shipment_packaging(ctx: ValidationContext) -> Outcome:
    """Packaging supports the shipment's class and holds temperature for the planned trip."""
    shipments = ctx.tables[T.SHIPMENTS]
    frame = (
        shipments[["shipment_id", "packaging_id", "planned_departure_at", "planned_arrival_at"]]
        .merge(ctx.applied_policies[["shipment_id", "policy_code"]], on="shipment_id", how="left")
        .merge(
            ctx.tables[T.PACKAGING][["packaging_id", "supported_policy_code", "max_hold_hours"]],
            on="packaging_id",
            how="left",
        )
    )
    frame.index = shipments.index
    hours = minutes_between(frame["planned_arrival_at"], frame["planned_departure_at"]) / 60.0
    mask, detail = problems(
        shipments.index,
        [
            (
                "packaging does not support the products' policy code",
                frame["policy_code"].notna()
                & frame["supported_policy_code"].notna()
                & (frame["supported_policy_code"] != frame["policy_code"]),
            ),
            (
                "planned trip longer than the packaging's max_hold_hours",
                hours > frame["max_hold_hours"],
            ),
        ],
    )
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, detail))


def check_released_lots(ctx: ValidationContext) -> Outcome:
    """Only RELEASED lots are shipped."""
    links = ctx.tables[T.SHIPMENT_LOTS]
    status = links["lot_id"].map(ctx.tables[T.LOTS].set_index("lot_id")["lot_status"])
    mask = status.notna() & (status != LotStatus.RELEASED)
    return Outcome(len(links), found(T.SHIPMENT_LOTS, links, mask, "shipped lot is not RELEASED"))


def check_lot_dates(ctx: ValidationContext) -> Outcome:
    """A lot is made before the departure date and expires after the arrival date it ships in.

    The stricter of planned and actual times is used, as in the generator (DECISIONS.md #34).
    """
    links = ctx.tables[T.SHIPMENT_LOTS]
    shipments = ctx.tables[T.SHIPMENTS]
    frame = links[["shipment_id", "lot_id"]].merge(
        ctx.tables[T.LOTS][["lot_id", "manufactured_on", "expires_on"]], on="lot_id", how="left"
    )
    frame = frame.merge(shipments, on="shipment_id", how="left")
    frame.index = links.index
    ship_from = frame[["planned_departure_at", "actual_departure_at"]].min(axis=1).dt.floor("D")
    ship_to = frame[["planned_arrival_at", "actual_arrival_at"]].max(axis=1).dt.floor("D")
    mask, detail = problems(
        links.index,
        [
            (
                "lot made on/after the departure date",
                _utc_midnight(frame["manufactured_on"]) >= ship_from,
            ),
            (
                "lot expires on/before the arrival date",
                _utc_midnight(frame["expires_on"]) <= ship_to,
            ),
        ],
    )
    return Outcome(len(links), found(T.SHIPMENT_LOTS, links, mask, detail))


def check_lot_quantities(ctx: ValidationContext) -> Outcome:
    """The units shipped from a lot never exceed the lot's quantity."""
    lots = ctx.tables[T.LOTS]
    shipped = ctx.tables[T.SHIPMENT_LOTS].groupby("lot_id")["quantity_units"].sum()
    total = lots["lot_id"].map(shipped).fillna(0)
    mask = total > lots["quantity_units"]
    detail = "shipped " + total.astype("int64").astype(str) + " > quantity_units"
    return Outcome(len(lots), found(T.LOTS, lots, mask, detail))


def check_lot_shelf_life(ctx: ValidationContext) -> Outcome:
    """``expires_on - manufactured_on`` equals the product's ``shelf_life_days``."""
    lots = ctx.tables[T.LOTS]
    shelf = lots["product_id"].map(
        ctx.tables[T.PRODUCTS].set_index("product_id")["shelf_life_days"]
    )
    days = (_utc_midnight(lots["expires_on"]) - _utc_midnight(lots["manufactured_on"])).dt.days
    mask = shelf.notna() & (days != shelf)
    return Outcome(len(lots), found(T.LOTS, lots, mask, "expiry does not match shelf_life_days"))


def check_party_active_dates(ctx: ValidationContext) -> Outcome:
    """No party is used (shipment, stop, custody or status event) before its ``active_from``."""
    shipments = ctx.tables[T.SHIPMENTS]
    uses = [
        pd.DataFrame({"party_id": shipments[c], "at": shipments["planned_departure_at"]})
        for c in ("supplier_party_id", "carrier_party_id", "receiver_party_id", "planner_party_id")
    ]
    stops = ctx.tables[T.ROUTE_STOPS]
    uses.append(
        pd.DataFrame(
            {"party_id": stops["party_id"], "at": stops["arrived_at"].fillna(stops["departed_at"])}
        )
    )
    custody = ctx.tables[T.CUSTODY_EVENTS]
    for column in ("from_party_id", "to_party_id"):
        uses.append(pd.DataFrame({"party_id": custody[column], "at": custody["event_at"]}))
    for table in (T.SHIPMENT_STATUS_EVENTS, T.COMPLIANCE_STATUS_EVENTS):
        if ctx.has(table):
            events = ctx.tables[table]
            uses.append(
                pd.DataFrame({"party_id": events["actor_party_id"], "at": events["event_at"]})
            )
    first_use = pd.concat(uses, ignore_index=True).groupby("party_id")["at"].min()
    parties = ctx.tables[T.PARTIES]
    first = parties["party_id"].map(first_use)
    mask = first.notna() & (first.dt.floor("D") < _utc_midnight(parties["active_from"]))
    detail = "used on " + first.dt.strftime("%Y-%m-%d").fillna("") + ", before active_from"
    return Outcome(len(parties), found(T.PARTIES, parties, mask, detail))


def check_declared_policy(ctx: ValidationContext) -> Outcome:
    """The planner declared the products' policy version effective at planned departure."""
    shipments = ctx.tables[T.SHIPMENTS]
    planned = shipments["shipment_id"].map(
        ctx.applied_policies.set_index("shipment_id")["planned_policy_id"]
    )
    mask = planned.notna() & (shipments["declared_policy_id"] != planned)
    detail = "declared " + shipments["declared_policy_id"] + ", products need " + planned.fillna("")
    return Outcome(int(planned.notna().sum()), found(T.SHIPMENTS, shipments, mask, detail))


# --------------------------------------------------------------------------------------------
# Loggers and readings
# --------------------------------------------------------------------------------------------


def _loggers_with_trip(ctx: ValidationContext) -> pd.DataFrame:
    """Loggers joined with their shipment's actual times and their device's interval."""
    loggers = ctx.tables[T.LOGGERS]
    frame = loggers.merge(
        ctx.tables[T.SHIPMENTS][["shipment_id", "actual_departure_at", "actual_arrival_at"]],
        on="shipment_id",
        how="left",
    ).merge(
        ctx.tables[T.DEVICES][["device_id", "reading_interval_min"]].drop_duplicates("device_id"),
        on="device_id",
        how="left",
    )
    frame.index = loggers.index
    return frame


def check_logger_windows(ctx: ValidationContext) -> Outcome:
    """The assignment covers the actual trip, and both ends lie on the reading grid."""
    loggers = ctx.tables[T.LOGGERS]
    frame = _loggers_with_trip(ctx)
    interval = frame["reading_interval_min"]
    mask, detail = problems(
        loggers.index,
        [
            (
                "assigned after actual departure",
                frame["assigned_at"] > frame["actual_departure_at"],
            ),
            (
                "unassigned before actual arrival",
                frame["unassigned_at"] < frame["actual_arrival_at"],
            ),
            (
                "assigned_at off the reading grid",
                _epoch_minutes(frame["assigned_at"]) % interval != 0,
            ),
            (
                "unassigned_at off the reading grid",
                _epoch_minutes(frame["unassigned_at"]) % interval != 0,
            ),
        ],
    )
    return Outcome(len(loggers), found(T.LOGGERS, loggers, mask, detail))


def check_device_overlap(ctx: ValidationContext) -> Outcome:
    """One device's assignment windows never overlap or share a reading instant."""
    loggers = ctx.tables[T.LOGGERS].sort_values(["device_id", "assigned_at"], kind="stable")
    previous_end = loggers.groupby("device_id")["unassigned_at"].shift()
    mask = previous_end.notna() & (loggers["assigned_at"] <= previous_end)
    detail = (
        "overlaps the device's previous assignment (ends "
        + previous_end.dt.strftime("%Y-%m-%dT%H:%MZ").fillna("")
        + ")"
    )
    return Outcome(len(loggers), found(T.LOGGERS, loggers, mask, detail))


def check_shipment_has_logger(ctx: ValidationContext) -> Outcome:
    """Every shipment has a logger assignment."""
    shipments = ctx.tables[T.SHIPMENTS]
    mask = ~shipments["shipment_id"].isin(ctx.tables[T.LOGGERS]["shipment_id"])
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, "no logger assignment"))


def check_reading_window(ctx: ValidationContext) -> Outcome:
    """Every reading lies inside its logger's window, on the reading grid."""
    readings = ctx.tables[T.TEMPERATURE_READINGS]
    windows = _loggers_with_trip(ctx)[
        ["logger_id", "assigned_at", "unassigned_at", "reading_interval_min"]
    ].drop_duplicates("logger_id")
    frame = readings[["logger_id", "recorded_at"]].merge(windows, on="logger_id", how="left")
    frame.index = readings.index
    offset = minutes_between(frame["recorded_at"], frame["assigned_at"])
    mask, detail = problems(
        readings.index,
        [
            (
                "outside the logger window",
                (frame["recorded_at"] < frame["assigned_at"])
                | (frame["recorded_at"] > frame["unassigned_at"]),
            ),
            ("off the reading grid", offset % frame["reading_interval_min"] != 0),
        ],
    )
    return Outcome(len(readings), found(T.TEMPERATURE_READINGS, readings, mask, detail))


def check_assigned_device(ctx: ValidationContext) -> Outcome:
    """A reading carries the device of its logger assignment."""
    readings = ctx.tables[T.TEMPERATURE_READINGS]
    assigned = readings["logger_id"].map(
        ctx.tables[T.LOGGERS].drop_duplicates("logger_id").set_index("logger_id")["device_id"]
    )
    mask = assigned.notna() & (readings["device_id"] != assigned)
    detail = "recorded by " + readings["device_id"] + ", assigned " + assigned.fillna("")
    return Outcome(
        int(assigned.notna().sum()), found(T.TEMPERATURE_READINGS, readings, mask, detail)
    )


def check_complete_stream(ctx: ValidationContext) -> Outcome:
    """Every logger window has one reading at every grid instant (any device)."""
    loggers = ctx.tables[T.LOGGERS]
    if loggers.empty:
        return Outcome(0, empty_findings())
    frame = _loggers_with_trip(ctx)
    interval = frame["reading_interval_min"].fillna(ctx.limits.reading_interval_min)
    expected = minutes_between(frame["unassigned_at"], frame["assigned_at"]) // interval + 1
    readings = ctx.tables[T.TEMPERATURE_READINGS]
    present = readings.merge(loggers[["logger_id", "assigned_at", "unassigned_at"]], on="logger_id")
    present = present[
        (present["recorded_at"] >= present["assigned_at"])
        & (present["recorded_at"] <= present["unassigned_at"])
    ]
    counts = loggers["logger_id"].map(present.groupby("logger_id").size()).fillna(0)
    windows = pd.DataFrame(
        {
            "logger_id": loggers["logger_id"],
            "start": loggers["assigned_at"],
            "end": loggers["unassigned_at"],
            "interval_min": interval.astype("int64"),
        }
    )
    gaps = loggers["logger_id"].map(padded_max_gaps(present, windows, key="logger_id"))
    missing = (expected - counts).astype("int64")
    mask = missing > 0
    detail = (
        missing.astype(str)
        + " of "
        + expected.astype("int64").astype(str)
        + " readings missing, largest gap "
        + gaps.astype(str)
        + " min"
    )
    return Outcome(len(loggers), found(T.LOGGERS, loggers, mask, detail))


# --------------------------------------------------------------------------------------------
# Route stops, custody and status history
# --------------------------------------------------------------------------------------------


def _ordered(frame: pd.DataFrame) -> pd.DataFrame:
    """Sort a shipment-scoped frame by (shipment, sequence), keeping its index."""
    return frame.sort_values(["shipment_id", "sequence"], kind="stable")


def check_route_structure(ctx: ValidationContext) -> Outcome:
    """Stops are numbered 1..n (n ≥ 2): ORIGIN first, DESTINATION last, hubs in between."""
    stops = _ordered(ctx.tables[T.ROUTE_STOPS])
    group = stops.groupby("shipment_id")
    position = group.cumcount()
    count = group["sequence"].transform("size")
    first, last = position == 0, position == count - 1
    is_type = stops["stop_type"]
    mask, detail = problems(
        stops.index,
        [
            ("sequence not contiguous from 1", stops["sequence"] != position + 1),
            ("fewer than two stops", count < 2),
            ("first stop is not ORIGIN", first & (is_type != StopType.ORIGIN)),
            ("last stop is not DESTINATION", last & ~first & (is_type != StopType.DESTINATION)),
            (
                "intermediate stop is not WAREHOUSE/CROSS_DOCK",
                ~first & ~last & ~is_type.isin(INTERMEDIATE_STOP_TYPES),
            ),
        ],
    )
    result = found(T.ROUTE_STOPS, stops, mask, detail)
    shipments = ctx.tables[T.SHIPMENTS]
    without = shipments[~shipments["shipment_id"].isin(stops["shipment_id"])]
    extra = findings_frame(
        without["shipment_id"], without["shipment_id"], ["no route stops"] * len(without)
    )
    return Outcome(len(stops), pd.concat([result, extra], ignore_index=True))


def check_stop_roles(ctx: ValidationContext) -> Outcome:
    """ORIGIN is the supplier, DESTINATION the receiver, hubs are WAREHOUSE parties."""
    stops = ctx.tables[T.ROUTE_STOPS]
    frame = stops.merge(
        ctx.tables[T.SHIPMENTS][["shipment_id", "supplier_party_id", "receiver_party_id"]],
        on="shipment_id",
        how="left",
    )
    frame.index = stops.index
    party_type = stops["party_id"].map(ctx.tables[T.PARTIES].set_index("party_id")["party_type"])
    mask, detail = problems(
        stops.index,
        [
            (
                "ORIGIN party is not the shipment supplier",
                (stops["stop_type"] == StopType.ORIGIN)
                & (stops["party_id"] != frame["supplier_party_id"]),
            ),
            (
                "DESTINATION party is not the shipment receiver",
                (stops["stop_type"] == StopType.DESTINATION)
                & (stops["party_id"] != frame["receiver_party_id"]),
            ),
            (
                "hub party is not a WAREHOUSE",
                stops["stop_type"].isin(INTERMEDIATE_STOP_TYPES)
                & party_type.notna()
                & (party_type != PartyType.WAREHOUSE),
            ),
        ],
    )
    return Outcome(len(stops), found(T.ROUTE_STOPS, stops, mask, detail))


def check_route_dates(ctx: ValidationContext) -> Outcome:
    """Stops are chronological; ORIGIN departs and DESTINATION arrives at the actual times."""
    stops = _ordered(ctx.tables[T.ROUTE_STOPS])
    frame = stops.merge(
        ctx.tables[T.SHIPMENTS][["shipment_id", "actual_departure_at", "actual_arrival_at"]],
        on="shipment_id",
        how="left",
    )
    frame.index = stops.index
    previous_departure = stops.groupby("shipment_id")["departed_at"].shift()
    mask, detail = problems(
        stops.index,
        [
            ("arrives before the previous stop departs", stops["arrived_at"] < previous_departure),
            ("departs before it arrives", stops["departed_at"] < stops["arrived_at"]),
            (
                "ORIGIN departed_at differs from actual_departure_at",
                (stops["stop_type"] == StopType.ORIGIN)
                & (stops["departed_at"] != frame["actual_departure_at"]),
            ),
            (
                "DESTINATION arrived_at differs from actual_arrival_at",
                (stops["stop_type"] == StopType.DESTINATION)
                & (stops["arrived_at"] != frame["actual_arrival_at"]),
            ),
        ],
    )
    return Outcome(len(stops), found(T.ROUTE_STOPS, stops, mask, detail))


def check_dwell(ctx: ValidationContext) -> Outcome:
    """``dwell_min`` = ``departed_at - arrived_at``, null when either is null."""
    stops = ctx.tables[T.ROUTE_STOPS]
    both = stops["arrived_at"].notna() & stops["departed_at"].notna()
    expected = minutes_between(stops["departed_at"], stops["arrived_at"])
    dwell = stops["dwell_min"].astype("Float64")
    wrong = pd.Series(
        np.where(both, (dwell != expected).fillna(True), dwell.notna()), index=stops.index
    )
    return Outcome(len(stops), found(T.ROUTE_STOPS, stops, wrong, "dwell_min does not reconcile"))


def _custody_with_context(ctx: ValidationContext) -> pd.DataFrame:
    """Custody events joined with their stop and their shipment's parties."""
    events = ctx.tables[T.CUSTODY_EVENTS]
    frame = events.merge(
        ctx.tables[T.ROUTE_STOPS][
            ["stop_id", "stop_type", "party_id", "arrived_at", "departed_at"]
        ],
        on="stop_id",
        how="left",
    ).merge(
        ctx.tables[T.SHIPMENTS][
            ["shipment_id", "supplier_party_id", "carrier_party_id", "receiver_party_id"]
        ],
        on="shipment_id",
        how="left",
    )
    frame.index = events.index
    return frame


def _custody_kinds(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """Masks for the four custody event kinds: pickup, delivery, hub in, hub out."""
    kind = frame["event_type"]
    handoff = kind == CustodyEventType.HANDOFF
    return {
        "pickup": kind == CustodyEventType.PICKUP,
        "delivery": kind == CustodyEventType.DELIVERY,
        "inbound": handoff & (frame["to_party_id"] == frame["party_id"]),
        "outbound": handoff & (frame["from_party_id"] == frame["party_id"]),
    }


def check_custody_roles(ctx: ValidationContext) -> Outcome:
    """Each custody event moves the goods between the parties its type and stop imply."""
    events = ctx.tables[T.CUSTODY_EVENTS]
    frame = _custody_with_context(ctx)
    kinds = _custody_kinds(frame)
    from_, to = frame["from_party_id"], frame["to_party_id"]
    carrier = frame["carrier_party_id"]
    ok = (
        (
            kinds["pickup"]
            & (frame["stop_type"] == StopType.ORIGIN)
            & (from_ == frame["supplier_party_id"])
            & (to == carrier)
        )
        | (
            kinds["delivery"]
            & (frame["stop_type"] == StopType.DESTINATION)
            & (from_ == carrier)
            & (to == frame["receiver_party_id"])
        )
        | (
            frame["stop_type"].isin(INTERMEDIATE_STOP_TYPES)
            & ((kinds["inbound"] & (from_ == carrier)) | (kinds["outbound"] & (to == carrier)))
        )
    )
    detail = "parties do not match the event type and stop"
    return Outcome(len(events), found(T.CUSTODY_EVENTS, events, ~ok, detail))


def check_custody_dates(ctx: ValidationContext) -> Outcome:
    """Custody times increase with sequence and equal the stop's departure/arrival time."""
    events = ctx.tables[T.CUSTODY_EVENTS]
    frame = _custody_with_context(ctx)
    kinds = _custody_kinds(frame)
    expected = (
        frame["departed_at"]
        .where(kinds["pickup"] | kinds["outbound"])
        .fillna(frame["arrived_at"].where(kinds["delivery"] | kinds["inbound"]))
    )
    ordered = _ordered(events)
    previous = ordered.groupby("shipment_id")["event_at"].shift().reindex(events.index)
    mask, detail = problems(
        events.index,
        [
            ("not after the previous custody event", events["event_at"] <= previous),
            ("event_at differs from the stop's time", events["event_at"] != expected),
        ],
    )
    return Outcome(len(events), found(T.CUSTODY_EVENTS, events, mask, detail))


def check_custody_chain(ctx: ValidationContext) -> Outcome:
    """The custody chain is unbroken (compliance_rules.md §1.8).

    Sequences run 1..n without a hole, each event starts where the previous one ended, the
    first is a PICKUP from the supplier and the last a DELIVERY to the receiver. The finding is
    recorded on the event where the chain breaks.
    """
    events = _ordered(_custody_with_context(ctx))
    group = events.groupby("shipment_id")
    position = group.cumcount()
    previous_sequence = group["sequence"].shift()
    previous_to = group["to_party_id"].shift()
    first = position == 0
    last = position == group["sequence"].transform("size") - 1
    mask, detail = problems(
        events.index,
        [
            ("first sequence is not 1", first & (events["sequence"] != 1)),
            ("sequence skips a number", ~first & (events["sequence"] != previous_sequence + 1)),
            (
                "from_party differs from the previous to_party",
                ~first & (events["from_party_id"] != previous_to),
            ),
            (
                "first event is not a PICKUP from the supplier",
                first
                & (
                    (events["event_type"] != CustodyEventType.PICKUP)
                    | (events["from_party_id"] != events["supplier_party_id"])
                ),
            ),
            (
                "last event is not a DELIVERY to the receiver",
                last
                & (
                    (events["event_type"] != CustodyEventType.DELIVERY)
                    | (events["to_party_id"] != events["receiver_party_id"])
                ),
            ),
        ],
    )
    result = found(T.CUSTODY_EVENTS, events, mask, detail)
    shipments = ctx.tables[T.SHIPMENTS]
    without = shipments[~shipments["shipment_id"].isin(events["shipment_id"])]
    extra = findings_frame(
        without["shipment_id"], without["shipment_id"], ["no custody events"] * len(without)
    )
    return Outcome(len(events), pd.concat([result, extra], ignore_index=True))


def check_custody_coverage(ctx: ValidationContext) -> Outcome:
    """ORIGIN has one PICKUP, DESTINATION one DELIVERY, every hub two HANDOFFs."""
    stops = ctx.tables[T.ROUTE_STOPS]
    events = ctx.tables[T.CUSTODY_EVENTS]
    counts = events.groupby(["stop_id", "event_type"]).size().unstack(fill_value=0)
    for kind in CustodyEventType:
        if kind.value not in counts.columns:
            counts[kind.value] = 0
    per_stop = counts.reindex(stops["stop_id"], fill_value=0)
    per_stop.index = stops.index
    total = per_stop.sum(axis=1)
    kind = stops["stop_type"]
    ok = np.select(
        [kind == StopType.ORIGIN, kind == StopType.DESTINATION],
        [
            (per_stop[CustodyEventType.PICKUP.value] == 1) & (total == 1),
            (per_stop[CustodyEventType.DELIVERY.value] == 1) & (total == 1),
        ],
        default=(per_stop[CustodyEventType.HANDOFF.value] == 2) & (total == 2),
    )
    detail = (
        kind
        + " stop has "
        + total.astype(str)
        + " custody event(s): "
        + per_stop.apply(
            lambda row: ", ".join(f"{k} {int(n)}" for k, n in row.items() if n), axis=1
        ).where(~pd.Series(ok, index=stops.index), "")
    )
    mask = pd.Series(~ok, index=stops.index)
    return Outcome(len(stops), found(T.ROUTE_STOPS, stops, mask, detail))


def _status_expectations(ctx: ValidationContext) -> pd.DataFrame:
    """Raw status events with their expected time window and actor (DECISIONS.md #47)."""
    events = _ordered(ctx.tables[T.SHIPMENT_STATUS_EVENTS])
    frame = events.merge(ctx.tables[T.SHIPMENTS], on="shipment_id", how="left")
    frame.index = events.index
    nth = events.groupby(["shipment_id", "status"]).cumcount()
    stops = ctx.intermediate_stops.set_index(["shipment_id", "rank"])
    at_key = pd.MultiIndex.from_arrays([events["shipment_id"], nth])
    transit_key = pd.MultiIndex.from_arrays([events["shipment_id"], nth - 1])
    stop_arrival = pd.Series(stops["arrived_at"].reindex(at_key).to_numpy(), index=events.index)
    stop_party = pd.Series(stops["party_id"].reindex(at_key).to_numpy(), index=events.index)
    previous_departure = pd.Series(
        stops["departed_at"].reindex(transit_key).to_numpy(), index=events.index
    )
    status = events["status"]
    at_stop = status == ShipmentStatus.AT_STOP
    in_transit = status == ShipmentStatus.IN_TRANSIT
    expected_at = (
        frame["actual_departure_at"]
        .where((status == ShipmentStatus.PICKED_UP) | (in_transit & (nth == 0)))
        .fillna(frame["actual_arrival_at"].where(status == ShipmentStatus.DELIVERED))
        .fillna(stop_arrival.where(at_stop))
        .fillna(previous_departure.where(in_transit & (nth > 0)))
    )
    actor = (
        frame["planner_party_id"]
        .where(status == ShipmentStatus.PLANNED)
        .fillna(frame["carrier_party_id"].where((status == ShipmentStatus.PICKED_UP) | in_transit))
        .fillna(stop_party.where(at_stop))
        .fillna(frame["receiver_party_id"].where(status == ShipmentStatus.DELIVERED))
    )
    lead_hours = minutes_between(frame["planned_departure_at"], events["event_at"]) / 60.0
    return events.assign(expected_at=expected_at, expected_actor=actor, lead_hours=lead_hours)


def check_status_dates(ctx: ValidationContext) -> Outcome:
    """Status times are non-decreasing and match the trip; PLANNED is within the planning lead."""
    events = ctx.tables[T.SHIPMENT_STATUS_EVENTS]
    frame = _status_expectations(ctx)
    previous = frame.groupby("shipment_id")["event_at"].shift()
    low, high = ctx.limits.planning_lead_hours
    planned = frame["status"] == ShipmentStatus.PLANNED
    mask, detail = problems(
        events.index,
        [
            ("before the previous status event", frame["event_at"] < previous),
            (
                f"PLANNED not {low:g}-{high:g} h before planned departure",
                planned & ((frame["lead_hours"] < low) | (frame["lead_hours"] > high)),
            ),
            (
                "event_at differs from the trip/stop time",
                ~planned & (frame["event_at"] != frame["expected_at"]),
            ),
        ],
    )
    return Outcome(len(events), found(T.SHIPMENT_STATUS_EVENTS, events, mask, detail))


def check_status_actors(ctx: ValidationContext) -> Outcome:
    """Each status event's actor has the role its status implies (DECISIONS.md #47).

    PLANNED → planner, PICKED_UP/IN_TRANSIT → carrier, AT_STOP → the stop's party,
    DELIVERED → receiver.
    """
    events = ctx.tables[T.SHIPMENT_STATUS_EVENTS]
    frame = _status_expectations(ctx)
    mask = frame["actor_party_id"] != frame["expected_actor"]
    detail = "actor is not the expected party " + frame["expected_actor"].fillna("(none)")
    return Outcome(len(events), found(T.SHIPMENT_STATUS_EVENTS, events, mask, detail))


def check_status_transitions(ctx: ValidationContext) -> Outcome:
    """The raw status history follows the generator lifecycle, numbered 1..n.

    PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → DELIVERED, with one AT_STOP per hub.
    """
    events = _ordered(ctx.tables[T.SHIPMENT_STATUS_EVENTS])
    group = events.groupby("shipment_id")
    position = group.cumcount()
    previous = group["status"].shift()
    first = position == 0
    last = position == group["sequence"].transform("size") - 1
    pairs = pd.Series(list(zip(previous, events["status"], strict=True)), index=events.index)
    hubs = ctx.intermediate_stops.groupby("shipment_id").size()
    at_stops = events["status"].eq(ShipmentStatus.AT_STOP).groupby(events["shipment_id"]).sum()
    wrong_count = events["shipment_id"].map(at_stops) != events["shipment_id"].map(hubs).fillna(0)
    mask, detail = problems(
        events.index,
        [
            ("sequence not contiguous from 1", events["sequence"] != position + 1),
            (
                "history does not start with PLANNED",
                first & (events["status"] != ShipmentStatus.PLANNED),
            ),
            ("illegal transition", ~first & ~pairs.isin(_RAW_TRANSITIONS)),
            (
                "history does not end with DELIVERED",
                last & (events["status"] != ShipmentStatus.DELIVERED),
            ),
            ("AT_STOP count differs from the number of hubs", last & wrong_count),
        ],
    )
    result = found(T.SHIPMENT_STATUS_EVENTS, events, mask, detail)
    shipments = ctx.tables[T.SHIPMENTS]
    without = shipments[~shipments["shipment_id"].isin(events["shipment_id"])]
    extra = findings_frame(
        without["shipment_id"], without["shipment_id"], ["no status history"] * len(without)
    )
    return Outcome(len(events), pd.concat([result, extra], ignore_index=True))


def check_engine_transitions(ctx: ValidationContext) -> Outcome:
    """Engine history continues the raw numbering and matches the decided action (§4)."""
    events = _ordered(ctx.tables[T.COMPLIANCE_STATUS_EVENTS])
    raw_last = ctx.tables[T.SHIPMENT_STATUS_EVENTS].groupby("shipment_id")["sequence"].max()
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS].set_index("shipment_id")["decided_action"]
    position = events.groupby("shipment_id").cumcount()
    start = events["shipment_id"].map(raw_last).fillna(0) + 1
    statuses = events.groupby("shipment_id")["status"].agg(">".join)
    expected = decisions.map(_ENGINE_EVENTS)
    wrong_path = events["shipment_id"].map(statuses) != events["shipment_id"].map(expected)
    mask, detail = problems(
        events.index,
        [
            ("sequence does not continue the raw history", events["sequence"] != start + position),
            ("statuses do not match the decided action", (position == 0) & wrong_path),
        ],
    )
    result = found(T.COMPLIANCE_STATUS_EVENTS, events, mask, detail)
    missing = sorted(set(decisions.index) - set(events["shipment_id"]))
    extra = findings_frame(missing, missing, ["no engine status events"] * len(missing))
    return Outcome(len(events), pd.concat([result, extra], ignore_index=True))


def check_engine_status_dates(ctx: ValidationContext) -> Outcome:
    """Engine events happen at the window end (DECISIONS.md #18), not before DELIVERED."""
    events = ctx.tables[T.COMPLIANCE_STATUS_EVENTS]
    window_end = events["shipment_id"].map(ctx.windows.set_index("shipment_id")["end"])
    raw = ctx.tables[T.SHIPMENT_STATUS_EVENTS]
    delivered = (
        raw[raw["status"] == ShipmentStatus.DELIVERED].groupby("shipment_id")["event_at"].max()
    )
    mask, detail = problems(
        events.index,
        [
            ("event_at differs from the window end", events["event_at"] != window_end),
            ("before DELIVERED", events["event_at"] < events["shipment_id"].map(delivered)),
        ],
    )
    return Outcome(len(events), found(T.COMPLIANCE_STATUS_EVENTS, events, mask, detail))


def check_engine_actors(ctx: ValidationContext) -> Outcome:
    """UNDER_REVIEW by a LOGISTICS_ANALYST; RELEASED/QUARANTINED by an OPERATIONS_MANAGER."""
    events = ctx.tables[T.COMPLIANCE_STATUS_EVENTS]
    types = events["actor_party_id"].map(ctx.tables[T.PARTIES].set_index("party_id")["party_type"])
    expected = np.where(
        events["status"] == ShipmentStatus.UNDER_REVIEW,
        PartyType.LOGISTICS_ANALYST.value,
        PartyType.OPERATIONS_MANAGER.value,
    )
    mask = types.notna() & (types != expected)
    return Outcome(
        len(events), found(T.COMPLIANCE_STATUS_EVENTS, events, mask, "actor has the wrong role")
    )


def check_history_sequences(ctx: ValidationContext) -> Outcome:
    """No engine status event reuses a (shipment, sequence) of the raw history."""
    events = ctx.tables[T.COMPLIANCE_STATUS_EVENTS]
    raw = ctx.tables[T.SHIPMENT_STATUS_EVENTS]
    taken = pd.MultiIndex.from_frame(raw[["shipment_id", "sequence"]])
    keys = pd.MultiIndex.from_frame(events[["shipment_id", "sequence"]])
    mask = pd.Series(keys.isin(taken), index=events.index)
    detail = "(shipment_id, sequence) already used by the raw history"
    return Outcome(len(events), found(T.COMPLIANCE_STATUS_EVENTS, events, mask, detail))


# --------------------------------------------------------------------------------------------
# Engine output reconciled against the readings
# --------------------------------------------------------------------------------------------


def recomputed_excursions(ctx: ValidationContext) -> pd.DataFrame:
    """One row per excursion recomputed from the trusted readings (compliance_rules.md §1.5).

    Returns:
        ``logger_id``, ``started_at``, ``shipment_id``, ``policy_id``, ``direction``,
        ``ended_at``, ``duration_min``, ``peak_c``, ``reading_count``, ``first_reading_id``,
        ``last_reading_id``, ``breach_type``.
    """
    runs = ctx.excursion_runs
    if runs.empty:
        text = pd.Series(dtype="str")
        return pd.DataFrame(
            {
                "logger_id": text,
                "started_at": pd.Series(dtype=TIMESTAMP_DTYPE),
                **{name: text for name in ("shipment_id", "policy_id", "direction")},
                "ended_at": pd.Series(dtype=TIMESTAMP_DTYPE),
                "reading_count": pd.Series(dtype="int64"),
                "duration_min": pd.Series(dtype="int64"),
                "peak_c": pd.Series(dtype="float64"),
                **{name: text for name in ("first_reading_id", "last_reading_id", "breach_type")},
            }
        )
    beyond = (runs["temperature_c"] > runs["abs_max_c"]) | (
        runs["temperature_c"] < runs["abs_min_c"]
    )
    grouped = runs.assign(beyond=beyond).groupby(["logger_id", "run_start"], sort=True)
    frame = grouped.agg(
        shipment_id=("shipment_id", "first"),
        policy_id=("applied_policy_id", "first"),
        direction=("direction", "first"),
        ended_at=("recorded_at", "max"),
        reading_count=("reading_id", "size"),
        high_peak=("temperature_c", "max"),
        low_peak=("temperature_c", "min"),
        absolute=("beyond", "any"),
        interval_min=("interval_min", "first"),
        single_min=("max_single_excursion_min", "first"),
    ).reset_index()
    frame = frame.rename(columns={"run_start": "started_at"})
    duration = (
        minutes_between(frame["ended_at"], frame["started_at"]) + frame["interval_min"]
    ).round()
    frame["duration_min"] = duration.astype("int64")
    frame["peak_c"] = (
        frame["high_peak"]
        .where(frame["direction"] == ExcursionDirection.HIGH, frame["low_peak"])
        .round(2)
    )
    frame["first_reading_id"] = ids.reading_ids(frame["logger_id"], frame["started_at"])
    frame["last_reading_id"] = ids.reading_ids(frame["logger_id"], frame["ended_at"])
    frame["breach_type"] = np.where(
        frame["absolute"],
        BreachType.ABSOLUTE_LIMIT_BREACH.value,
        np.where(
            frame["duration_min"] > frame["single_min"],
            BreachType.SINGLE_DURATION_BREACH.value,
            BreachType.WITHIN_ALLOWANCE.value,
        ),
    )
    return frame.drop(columns=["high_peak", "low_peak", "absolute", "interval_min", "single_min"])


_REPORT_FIELDS = (
    "shipment_id",
    "policy_id",
    "direction",
    "ended_at",
    "duration_min",
    "peak_c",
    "reading_count",
    "first_reading_id",
    "last_reading_id",
    "breach_type",
)


def check_excursion_reports(ctx: ValidationContext) -> Outcome:
    """Each report equals an excursion recomputed from the readings, and none is missing."""
    reports = ctx.tables[T.EXCURSION_REPORTS]
    runs = recomputed_excursions(ctx)
    merged = reports.merge(
        runs, on=["logger_id", "started_at"], how="outer", suffixes=("", "_run"), indicator=True
    )
    both = merged["_merge"] == "both"
    pairs = [("no excursion recomputed at started_at", merged["_merge"] == "left_only")]
    for name in _REPORT_FIELDS:
        report, run = merged[name], merged[f"{name}_run"]
        differs = (report - run).abs() > 1e-9 if name == "peak_c" else report != run
        pairs.append((f"{name} does not reconcile", both & differs))
    mask, detail = problems(merged.index, pairs)
    known = merged[mask & merged["report_id"].notna()]
    result = findings_frame(
        known["report_id"], known["shipment_id"], detail[mask & merged["report_id"].notna()]
    )
    unreported = merged[merged["_merge"] == "right_only"]
    extra = findings_frame(
        unreported["first_reading_id_run"],
        unreported["shipment_id_run"],
        ["excursion recomputed from the readings has no report"] * len(unreported),
    )
    return Outcome(len(reports), pd.concat([result, extra], ignore_index=True))


def check_excursion_links(ctx: ValidationContext) -> Outcome:
    """Each report links exactly the readings of its excursion, and every such reading is linked."""
    bridge = ctx.tables[T.EXCURSION_REPORT_READINGS]
    reports = ctx.tables[T.EXCURSION_REPORTS][["report_id", "logger_id", "started_at"]]
    runs = ctx.excursion_runs[["reading_id", "logger_id", "run_start"]]
    frame = bridge.merge(reports, on="report_id", how="left").merge(
        runs, on="reading_id", how="left", suffixes=("", "_run")
    )
    frame.index = bridge.index
    mask, detail = problems(
        bridge.index,
        [
            ("reading is not an out-of-range trusted reading", frame["run_start"].isna()),
            (
                "reading belongs to another excursion",
                frame["run_start"].notna()
                & (
                    (frame["logger_id_run"] != frame["logger_id"])
                    | (frame["run_start"] != frame["started_at"])
                ),
            ),
        ],
    )
    result = found(T.EXCURSION_REPORT_READINGS, bridge, mask, detail)
    unlinked = runs[~runs["reading_id"].isin(bridge["reading_id"])]
    extra = findings_frame(
        unlinked["reading_id"],
        ids.embedded_shipment_ids(unlinked["reading_id"]),
        ["out-of-range trusted reading linked to no report"] * len(unlinked),
    )
    return Outcome(len(bridge), pd.concat([result, extra], ignore_index=True))


def check_decision_policy(ctx: ValidationContext) -> Outcome:
    """The applied policy is the products' version at actual departure; the source matches."""
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS]
    applied = decisions["shipment_id"].map(
        ctx.applied_policies.set_index("shipment_id")["applied_policy_id"]
    )
    declared = decisions["shipment_id"].map(
        ctx.tables[T.SHIPMENTS].set_index("shipment_id")["declared_policy_id"]
    )
    source = np.where(
        declared == decisions["applied_policy_id"],
        PolicySource.DECLARED_CONFIRMED.value,
        PolicySource.PRODUCT_OVERRIDE.value,
    )
    mask, detail = problems(
        decisions.index,
        [
            (
                "applied_policy_id is not the products' effective version",
                decisions["applied_policy_id"] != applied,
            ),
            ("policy_source does not match the declaration", decisions["policy_source"] != source),
        ],
    )
    return Outcome(len(decisions), found(T.COMPLIANCE_DECISIONS, decisions, mask, detail))


def check_decision_coverage(ctx: ValidationContext) -> Outcome:
    """Reading counts, coverage and the largest gap reconcile with the readings (§1.3, §1.4)."""
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS]
    windows = ctx.windows
    trusted = ctx.trusted_readings
    span = minutes_between(windows["end"], windows["start"])
    expected = (span // windows["interval_min"] + 1).astype("int64")
    by_shipment = pd.DataFrame(
        {
            "expected": expected.to_numpy(),
            "count": windows["shipment_id"]
            .map(trusted.groupby("shipment_id").size())
            .fillna(0)
            .to_numpy(),
        },
        index=windows["shipment_id"],
    )
    by_shipment["coverage"] = (100.0 * by_shipment["count"] / by_shipment["expected"]).round(2)
    by_shipment["gap"] = padded_max_gaps(trusted, windows).reindex(by_shipment.index)
    facts = by_shipment.reindex(decisions["shipment_id"])
    facts.index = decisions.index
    mask, detail = problems(
        decisions.index,
        [
            (
                "expected_reading_count does not reconcile",
                decisions["expected_reading_count"] != facts["expected"],
            ),
            ("reading_count does not reconcile", decisions["reading_count"] != facts["count"]),
            (
                "coverage_pct does not reconcile",
                (decisions["coverage_pct"] - facts["coverage"]).abs() > 1e-9,
            ),
            ("max_gap_min does not reconcile", decisions["max_gap_min"] != facts["gap"]),
        ],
    )
    return Outcome(len(decisions), found(T.COMPLIANCE_DECISIONS, decisions, mask, detail))


def check_decision_excursions(ctx: ValidationContext) -> Outcome:
    """``excursion_report_ids`` lists the shipment's reports; the total is their duration sum."""
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS]
    reports = ctx.tables[T.EXCURSION_REPORTS].sort_values("report_id", kind="stable")
    grouped = reports.groupby("shipment_id")
    listed = (
        decisions["shipment_id"]
        .map(grouped["report_id"].agg(LIST_SEPARATOR.join))
        .astype(object)
        .fillna("")
    )
    total = decisions["shipment_id"].map(grouped["duration_min"].sum()).fillna(0)
    mask, detail = problems(
        decisions.index,
        [
            (
                "excursion_report_ids differ from the shipment's reports",
                decisions["excursion_report_ids"].fillna("") != listed,
            ),
            (
                "total_excursion_min differs from the reports' durations",
                decisions["total_excursion_min"] != total,
            ),
        ],
    )
    return Outcome(len(decisions), found(T.COMPLIANCE_DECISIONS, decisions, mask, detail))


def check_decision_evidence(ctx: ValidationContext) -> Outcome:
    """Reason codes that follow from numbers agree with them (gaps, coverage, breaches, ...)."""
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS]
    policies = ctx.tables[T.THRESHOLD_POLICIES].set_index("policy_id")
    policy = policies.reindex(decisions["applied_policy_id"])
    policy.index = decisions.index
    reports = ctx.tables[T.EXCURSION_REPORTS]
    breaches = reports.groupby(["shipment_id", "breach_type"]).size().unstack(fill_value=0)
    shipments = ctx.tables[T.SHIPMENTS].set_index("shipment_id")
    longest_dwell = ctx.tables[T.ROUTE_STOPS].groupby("shipment_id")["dwell_min"].max()
    ship = decisions["shipment_id"]

    def any_breach(kind: BreachType) -> pd.Series:
        column = breaches[kind.value] if kind.value in breaches.columns else pd.Series(dtype=float)
        return ship.map(column).fillna(0) > 0

    claim_pass = ship.map(shipments["carrier_reported_status"]) == CarrierReportedStatus.PASS
    evidence = {
        ReasonCode.ABSOLUTE_LIMIT_BREACH: any_breach(BreachType.ABSOLUTE_LIMIT_BREACH),
        ReasonCode.SINGLE_DURATION_BREACH: any_breach(BreachType.SINGLE_DURATION_BREACH),
        ReasonCode.CUMULATIVE_DURATION_BREACH: (
            decisions["total_excursion_min"] > policy["max_cumulative_excursion_min"]
        ),
        ReasonCode.DATA_GAP: decisions["max_gap_min"] > policy["max_reading_gap_min"],
        ReasonCode.LOW_COVERAGE: decisions["coverage_pct"] < policy["min_coverage_pct"],
        ReasonCode.EXCURSION_WITHIN_ALLOWANCE: any_breach(BreachType.WITHIN_ALLOWANCE),
        ReasonCode.POLICY_MISMATCH_DECLARED: (
            ship.map(shipments["declared_policy_id"]) != decisions["applied_policy_id"]
        ),
        ReasonCode.LONG_DWELL: ship.map(longest_dwell).fillna(0)
        > ctx.limits.long_dwell_threshold_min,
        ReasonCode.CARRIER_STATUS_CONTRADICTED: (
            claim_pass != (decisions["decided_action"] == DecidedAction.RELEASE)
        ),
    }
    pairs = [
        (
            f"{code.value} does not match the evidence",
            _has_code(decisions["reason_codes"], code) != expected,
        )
        for code, expected in evidence.items()
    ]
    mask, detail = problems(decisions.index, pairs)
    return Outcome(len(decisions), found(T.COMPLIANCE_DECISIONS, decisions, mask, detail))


def check_decision_per_shipment(ctx: ValidationContext) -> Outcome:
    """Every shipment has exactly one compliance decision."""
    shipments = ctx.tables[T.SHIPMENTS]
    mask = ~shipments["shipment_id"].isin(ctx.tables[T.COMPLIANCE_DECISIONS]["shipment_id"])
    return Outcome(len(shipments), found(T.SHIPMENTS, shipments, mask, "no compliance decision"))


def check_report_policy(ctx: ValidationContext) -> Outcome:
    """An excursion report uses its shipment's applied policy and its shipment's logger."""
    reports = ctx.tables[T.EXCURSION_REPORTS]
    applied = reports["shipment_id"].map(
        ctx.tables[T.COMPLIANCE_DECISIONS].set_index("shipment_id")["applied_policy_id"]
    )
    logger = reports["shipment_id"].map(
        ctx.tables[T.LOGGERS].drop_duplicates("shipment_id").set_index("shipment_id")["logger_id"]
    )
    mask, detail = problems(
        reports.index,
        [
            (
                "policy_id differs from the decision's applied policy",
                reports["policy_id"] != applied,
            ),
            ("logger_id is not the shipment's logger", reports["logger_id"] != logger),
        ],
    )
    return Outcome(len(reports), found(T.EXCURSION_REPORTS, reports, mask, detail))


def check_carrier_claim(ctx: ValidationContext) -> Outcome:
    """The carrier's PASS/FAIL claim agrees with the decided action (PASS ⇔ RELEASE)."""
    decisions = ctx.tables[T.COMPLIANCE_DECISIONS]
    claim = decisions["shipment_id"].map(
        ctx.tables[T.SHIPMENTS].set_index("shipment_id")["carrier_reported_status"]
    )
    release = decisions["decided_action"] == DecidedAction.RELEASE
    mask = claim.notna() & ((claim == CarrierReportedStatus.PASS) != release)
    detail = "carrier reported " + claim.fillna("") + ", decision " + decisions["decided_action"]
    return Outcome(len(decisions), found(T.COMPLIANCE_DECISIONS, decisions, mask, detail))


# --------------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------------

_DUPLICATE_KEYS: tuple[tuple[TableName, tuple[str, ...]], ...] = (
    (T.PARTIES, ("name",)),
    (T.THRESHOLD_POLICIES, ("policy_code", "version")),
    (T.PRODUCTS, ("name",)),
    (T.PACKAGING, ("name",)),
    (T.DEVICES, ("serial_number",)),
    (T.LOGGERS, ("shipment_id",)),
    (T.TEMPERATURE_READINGS, ("logger_id", "recorded_at")),
    (T.ROUTE_STOPS, ("shipment_id", "sequence")),
    (T.CUSTODY_EVENTS, ("shipment_id", "sequence")),
    (T.SHIPMENT_STATUS_EVENTS, ("shipment_id", "sequence")),
    (T.COMPLIANCE_STATUS_EVENTS, ("shipment_id", "sequence")),
    (T.EXCURSION_REPORTS, ("logger_id", "started_at")),
    (T.EXCURSION_REPORT_READINGS, ("reading_id",)),
)
"""Natural keys that must be unique besides the primary key."""

_I, _B = CheckTier.INTEGRITY, CheckTier.BUSINESS_RULE
_C = CheckCategory

_SPECIFIC: tuple[
    tuple[str, CheckCategory, CheckTier, TableName, tuple[TableName, ...], Callable], ...
] = (
    # Foreign keys beyond single columns (party roles, links into the engine's output).
    (
        "fk.products.supplier_role",
        _C.FOREIGN_KEY,
        _I,
        T.PRODUCTS,
        (T.PRODUCTS, T.PARTIES),
        check_product_supplier_role,
    ),
    (
        "fk.shipments.party_roles",
        _C.FOREIGN_KEY,
        _I,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.PARTIES),
        check_shipment_party_roles,
    ),
    (
        "fk.route_stops.party_roles",
        _C.FOREIGN_KEY,
        _I,
        T.ROUTE_STOPS,
        (T.ROUTE_STOPS, T.SHIPMENTS, T.PARTIES),
        check_stop_roles,
    ),
    (
        "fk.custody_events.party_roles",
        _C.FOREIGN_KEY,
        _I,
        T.CUSTODY_EVENTS,
        (T.CUSTODY_EVENTS, T.ROUTE_STOPS, T.SHIPMENTS),
        check_custody_roles,
    ),
    (
        "fk.shipment_status_events.actor_roles",
        _C.FOREIGN_KEY,
        _I,
        T.SHIPMENT_STATUS_EVENTS,
        (T.SHIPMENT_STATUS_EVENTS, T.SHIPMENTS, T.ROUTE_STOPS),
        check_status_actors,
    ),
    (
        "fk.compliance_status_events.actor_roles",
        _C.FOREIGN_KEY,
        _I,
        T.COMPLIANCE_STATUS_EVENTS,
        (T.COMPLIANCE_STATUS_EVENTS, T.PARTIES),
        check_engine_actors,
    ),
    (
        "fk.excursion_reports.decision_policy",
        _C.FOREIGN_KEY,
        _I,
        T.EXCURSION_REPORTS,
        (T.EXCURSION_REPORTS, T.COMPLIANCE_DECISIONS, T.LOGGERS),
        check_report_policy,
    ),
    # Duplicates across the two halves of the status history.
    (
        "dup.compliance_status_events.raw_sequence",
        _C.DUPLICATE,
        _I,
        T.COMPLIANCE_STATUS_EVENTS,
        (T.COMPLIANCE_STATUS_EVENTS, T.SHIPMENT_STATUS_EVENTS),
        check_history_sequences,
    ),
    # Date ordering.
    (
        "dates.shipments.trip",
        _C.DATE_ORDER,
        _I,
        T.SHIPMENTS,
        (T.SHIPMENTS,),
        check_shipment_trip_dates,
    ),
    (
        "dates.shipment_lots.lot_dates",
        _C.DATE_ORDER,
        _I,
        T.SHIPMENT_LOTS,
        (T.SHIPMENT_LOTS, T.LOTS, T.SHIPMENTS),
        check_lot_dates,
    ),
    (
        "dates.parties.active_from",
        _C.DATE_ORDER,
        _I,
        T.PARTIES,
        (T.PARTIES, T.SHIPMENTS, T.ROUTE_STOPS, T.CUSTODY_EVENTS),
        check_party_active_dates,
    ),
    (
        "dates.loggers.window",
        _C.DATE_ORDER,
        _I,
        T.LOGGERS,
        (T.LOGGERS, T.SHIPMENTS, T.DEVICES),
        check_logger_windows,
    ),
    (
        "dates.temperature_readings.window_grid",
        _C.DATE_ORDER,
        _I,
        T.TEMPERATURE_READINGS,
        (T.TEMPERATURE_READINGS, T.LOGGERS, T.SHIPMENTS, T.DEVICES),
        check_reading_window,
    ),
    (
        "dates.route_stops.chronological",
        _C.DATE_ORDER,
        _I,
        T.ROUTE_STOPS,
        (T.ROUTE_STOPS, T.SHIPMENTS),
        check_route_dates,
    ),
    (
        "dates.custody_events.chronological",
        _C.DATE_ORDER,
        _I,
        T.CUSTODY_EVENTS,
        (T.CUSTODY_EVENTS, T.ROUTE_STOPS, T.SHIPMENTS),
        check_custody_dates,
    ),
    (
        "dates.shipment_status_events.chronological",
        _C.DATE_ORDER,
        _I,
        T.SHIPMENT_STATUS_EVENTS,
        (T.SHIPMENT_STATUS_EVENTS, T.SHIPMENTS, T.ROUTE_STOPS),
        check_status_dates,
    ),
    (
        "dates.compliance_status_events.timing",
        _C.DATE_ORDER,
        _I,
        T.COMPLIANCE_STATUS_EVENTS,
        (T.COMPLIANCE_STATUS_EVENTS, T.SHIPMENT_STATUS_EVENTS, T.SHIPMENTS, T.LOGGERS, T.DEVICES),
        check_engine_status_dates,
    ),
    # Lifecycle.
    (
        "lifecycle.shipment_status_events.transitions",
        _C.LIFECYCLE,
        _I,
        T.SHIPMENT_STATUS_EVENTS,
        (T.SHIPMENT_STATUS_EVENTS, T.SHIPMENTS, T.ROUTE_STOPS),
        check_status_transitions,
    ),
    (
        "lifecycle.compliance_status_events.transitions",
        _C.LIFECYCLE,
        _I,
        T.COMPLIANCE_STATUS_EVENTS,
        (T.COMPLIANCE_STATUS_EVENTS, T.SHIPMENT_STATUS_EVENTS, T.COMPLIANCE_DECISIONS),
        check_engine_transitions,
    ),
    # Arithmetic reconciliation.
    (
        "recon.lots.quantity",
        _C.RECONCILIATION,
        _I,
        T.LOTS,
        (T.LOTS, T.SHIPMENT_LOTS),
        check_lot_quantities,
    ),
    (
        "recon.lots.shelf_life",
        _C.RECONCILIATION,
        _I,
        T.LOTS,
        (T.LOTS, T.PRODUCTS),
        check_lot_shelf_life,
    ),
    (
        "recon.route_stops.dwell_min",
        _C.RECONCILIATION,
        _I,
        T.ROUTE_STOPS,
        (T.ROUTE_STOPS,),
        check_dwell,
    ),
    (
        "recon.excursion_reports.from_readings",
        _C.RECONCILIATION,
        _I,
        T.EXCURSION_REPORTS,
        (
            T.EXCURSION_REPORTS,
            T.TEMPERATURE_READINGS,
            T.LOGGERS,
            T.DEVICES,
            T.SHIPMENTS,
            T.SHIPMENT_LOTS,
            T.LOTS,
            T.PRODUCTS,
            T.THRESHOLD_POLICIES,
        ),
        check_excursion_reports,
    ),
    (
        "recon.excursion_report_readings.links",
        _C.RECONCILIATION,
        _I,
        T.EXCURSION_REPORT_READINGS,
        (
            T.EXCURSION_REPORT_READINGS,
            T.EXCURSION_REPORTS,
            T.TEMPERATURE_READINGS,
            T.LOGGERS,
            T.DEVICES,
            T.SHIPMENTS,
            T.SHIPMENT_LOTS,
            T.LOTS,
            T.PRODUCTS,
            T.THRESHOLD_POLICIES,
        ),
        check_excursion_links,
    ),
    (
        "recon.compliance_decisions.coverage",
        _C.RECONCILIATION,
        _I,
        T.COMPLIANCE_DECISIONS,
        (T.COMPLIANCE_DECISIONS, T.TEMPERATURE_READINGS, T.LOGGERS, T.DEVICES, T.SHIPMENTS),
        check_decision_coverage,
    ),
    (
        "recon.compliance_decisions.excursion_totals",
        _C.RECONCILIATION,
        _I,
        T.COMPLIANCE_DECISIONS,
        (T.COMPLIANCE_DECISIONS, T.EXCURSION_REPORTS),
        check_decision_excursions,
    ),
    (
        "recon.compliance_decisions.evidence_codes",
        _C.RECONCILIATION,
        _I,
        T.COMPLIANCE_DECISIONS,
        (
            T.COMPLIANCE_DECISIONS,
            T.EXCURSION_REPORTS,
            T.THRESHOLD_POLICIES,
            T.SHIPMENTS,
            T.ROUTE_STOPS,
        ),
        check_decision_evidence,
    ),
    # Cross-table consistency.
    ("consistency.parties.cities", _C.CONSISTENCY, _I, T.PARTIES, (T.PARTIES,), check_party_cities),
    (
        "consistency.devices.reading_interval",
        _C.CONSISTENCY,
        _I,
        T.DEVICES,
        (T.DEVICES,),
        check_device_interval,
    ),
    (
        "consistency.shipments.lots",
        _C.CONSISTENCY,
        _I,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.SHIPMENT_LOTS, T.LOTS, T.PRODUCTS),
        check_shipment_lots,
    ),
    (
        "consistency.shipments.packaging",
        _C.CONSISTENCY,
        _I,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.PACKAGING, T.SHIPMENT_LOTS, T.LOTS, T.PRODUCTS, T.THRESHOLD_POLICIES),
        check_shipment_packaging,
    ),
    (
        "consistency.shipment_lots.released",
        _C.CONSISTENCY,
        _I,
        T.SHIPMENT_LOTS,
        (T.SHIPMENT_LOTS, T.LOTS),
        check_released_lots,
    ),
    (
        "consistency.route_stops.structure",
        _C.CONSISTENCY,
        _I,
        T.ROUTE_STOPS,
        (T.ROUTE_STOPS, T.SHIPMENTS),
        check_route_structure,
    ),
    (
        "consistency.shipments.one_decision",
        _C.CONSISTENCY,
        _I,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.COMPLIANCE_DECISIONS),
        check_decision_per_shipment,
    ),
    (
        "consistency.compliance_decisions.applied_policy",
        _C.CONSISTENCY,
        _I,
        T.COMPLIANCE_DECISIONS,
        (
            T.COMPLIANCE_DECISIONS,
            T.SHIPMENTS,
            T.SHIPMENT_LOTS,
            T.LOTS,
            T.PRODUCTS,
            T.THRESHOLD_POLICIES,
        ),
        check_decision_policy,
    ),
    # Business rules an injected edge case may break on purpose (findings must be explained).
    (
        RULE_HAS_LOGGER,
        _C.CONSISTENCY,
        _B,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.LOGGERS),
        check_shipment_has_logger,
    ),
    (RULE_DEVICE_OVERLAP, _C.DATE_ORDER, _B, T.LOGGERS, (T.LOGGERS,), check_device_overlap),
    (
        RULE_ASSIGNED_DEVICE,
        _C.CONSISTENCY,
        _B,
        T.TEMPERATURE_READINGS,
        (T.TEMPERATURE_READINGS, T.LOGGERS),
        check_assigned_device,
    ),
    (
        RULE_COMPLETE_STREAM,
        _C.RECONCILIATION,
        _B,
        T.LOGGERS,
        (T.LOGGERS, T.TEMPERATURE_READINGS, T.SHIPMENTS, T.DEVICES),
        check_complete_stream,
    ),
    (
        RULE_CUSTODY_CHAIN,
        _C.LIFECYCLE,
        _B,
        T.CUSTODY_EVENTS,
        (T.CUSTODY_EVENTS, T.ROUTE_STOPS, T.SHIPMENTS),
        check_custody_chain,
    ),
    (
        RULE_CUSTODY_COVERAGE,
        _C.CONSISTENCY,
        _B,
        T.ROUTE_STOPS,
        (T.ROUTE_STOPS, T.CUSTODY_EVENTS),
        check_custody_coverage,
    ),
    (
        RULE_DECLARED_POLICY,
        _C.CONSISTENCY,
        _B,
        T.SHIPMENTS,
        (T.SHIPMENTS, T.SHIPMENT_LOTS, T.LOTS, T.PRODUCTS, T.THRESHOLD_POLICIES),
        check_declared_policy,
    ),
    (
        RULE_CARRIER_CLAIM,
        _C.CONSISTENCY,
        _B,
        T.COMPLIANCE_DECISIONS,
        (T.COMPLIANCE_DECISIONS, T.SHIPMENTS),
        check_carrier_claim,
    ),
)


def _first_line(func: Callable) -> str:
    """The first line of a function's docstring."""
    return (func.__doc__ or "").strip().splitlines()[0]


def build_checks() -> tuple[Check, ...]:
    """Assemble the registry: generic per-table checks first, then the specific ones.

    Returns:
        Every check, in report order (sorted by category, then ID).
    """
    checks: list[Check] = []
    for table in VALIDATED_TABLES:
        checks.append(
            Check(
                f"schema.{table.value}",
                _C.SCHEMA,
                _I,
                table,
                (table,),
                "table passes its pandera schema (types, formats, row rules)",
                _schema_check(table),
            )
        )
        checks.append(
            Check(
                f"pk.{table.value}",
                _C.PRIMARY_KEY,
                _I,
                table,
                (table,),
                f"primary key ({', '.join(PRIMARY_KEYS[table])}) is non-null and unique",
                _primary_key_check(table),
            )
        )
        if enum_columns(table):
            checks.append(
                Check(
                    f"enum.{table.value}",
                    _C.ENUM,
                    _I,
                    table,
                    (table,),
                    f"allowed values in {', '.join(enum_columns(table))}",
                    _enum_check(table),
                )
            )
    for child, column, parent, parent_column in FOREIGN_KEYS:
        if child in VALIDATED_TABLES and parent in VALIDATED_TABLES:
            checks.append(
                Check(
                    f"fk.{child.value}.{column}",
                    _C.FOREIGN_KEY,
                    _I,
                    child,
                    (child, parent),
                    f"{column} → {parent.value}.{parent_column} (DuckDB anti-join)",
                    _foreign_key_check(child, column, parent, parent_column),
                )
            )
    for table, columns in _DUPLICATE_KEYS:
        checks.append(
            Check(
                f"dup.{table.value}.{'_'.join(columns)}",
                _C.DUPLICATE,
                _I,
                table,
                (table,),
                f"({', '.join(columns)}) is unique",
                _duplicate_check(table, columns),
            )
        )
    for check_id, category, tier, table, needs, func in _SPECIFIC:
        checks.append(Check(check_id, category, tier, table, needs, _first_line(func), func))
    order = {category: rank for rank, category in enumerate(CheckCategory)}
    return tuple(sorted(checks, key=lambda c: (c.tier != _I, order[c.category], c.check_id)))


CHECKS: tuple[Check, ...] = build_checks()
"""Every validation check, in report order (integrity first, then business rules)."""

BUSINESS_RULE_CHECKS: frozenset[str] = frozenset(c.check_id for c in CHECKS if c.tier is _B)
"""IDs of the business-rule checks."""
