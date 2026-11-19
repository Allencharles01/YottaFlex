# Data Dictionary

Every table, every field, its type, whether it is required, its allowed values, unit, uniqueness
and validation rule. The executable twin of this document is `src/coldchain/schemas.py`; the test
`tests/test_data_dictionary.py` fails if the two disagree on tables, field names, field order,
types or required flags.

## Conventions

| Topic | Convention |
|---|---|
| Names | Tables and columns are `snake_case`. A table's name is also its file stem (`shipments.csv`). |
| IDs | Built only by `src/coldchain/ids.py`; immutable; zero-padded counters. The year segment (`2026`) is the ID-series year, not a business date. |
| Types | `string` · `integer` (64-bit) · `float` (64-bit) · `timestamp` (UTC, whole minutes, written `YYYY-MM-DDTHH:MM:SSZ`) · `date` (calendar date, written `YYYY-MM-DD`). |
| Temperatures | °C floats rounded to 2 decimals. Physical sensor range −100 to 100 °C. |
| Durations | Integer minutes unless the field name says otherwise (`max_hold_hours`, `shelf_life_days`). |
| Multi-valued cells | Items joined with `\|` (no spaces). Reason codes appear once each, in the canonical order of `ReasonCode` (most severe first). Empty list = null. |
| Effective windows | Half-open `[from, to)`: `to` is exclusive; a null `to` is open-ended. Dates are UTC midnight. |
| Nulls | Only fields marked *Required = no* may be null; the rule column says when. |
| Layers | `raw` = generator/injector input (never contains labels) · `compliance` = engine output · `ground_truth` = answer key (never read by the engine). |
| Ordering | Every table is sorted by its primary key before it is written. |

**Where each rule is enforced** (tag at the start of each rule):
**S** = pandera schema (`schemas.py`, single table) · **V** = validation suite (Phase 6, cross-table
or config-dependent) · **E** = detected by the compliance engine (Phase 4). Rules tagged **E** may
be broken on purpose by an injected anomaly; they are therefore not schema rules, so anomalous but
well-formed data still loads.

---

## Master data

### `parties`

**Layer:** raw · **Primary key:** `party_id` · **Written by:** generator

The actors of the brief, represented as data (no logins).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `party_id` | string | yes | `PTY-0001` | — | PK | S: ID format. |
| `party_type` | string | yes | SUPPLIER, CARRIER, WAREHOUSE, RECEIVER, PLANNER, LOGISTICS_ANALYST, OPERATIONS_MANAGER | — | no | S: enum. |
| `name` | string | yes | 1–80 chars | — | yes | S: non-empty, no surrounding whitespace. |
| `city` | string | yes | a `city` in `config/reference/locations.yaml` | — | no | S: 1–40 chars. V: city exists in reference; WAREHOUSE parties are in hub cities, SUPPLIER parties in manufacturing-cluster cities. |
| `active_from` | date | yes | `YYYY-MM-DD` | — | no | V: on or before the date of every shipment, stop, custody or status event that references the party. |

### `threshold_policies`

**Layer:** raw · **Primary key:** `policy_id` · **Written by:** generator (from `config/reference/threshold_policies.yaml`)

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `policy_id` | string | yes | `THR-<CODE>-V<n>` | — | PK | S: equals `THR-{policy_code}-V{version}`. |
| `policy_code` | string | yes | REFRIG_2_8, COOL_8_15, FROZEN, AMBIENT_15_25 | — | with `version` | S: enum. |
| `version` | integer | yes | ≥ 1 | — | with `policy_code` | S: within a code, a later `effective_from` has a higher version. |
| `min_c` | float | yes | 2 dp | °C | no | S: `abs_min_c < min_c < max_c < abs_max_c`. |
| `max_c` | float | yes | 2 dp | °C | no | S: see `min_c`. A reading equal to `max_c` is in range. |
| `abs_min_c` | float | yes | 2 dp | °C | no | S: see `min_c`. |
| `abs_max_c` | float | yes | 2 dp | °C | no | S: see `min_c`. |
| `max_single_excursion_min` | integer | yes | ≥ 1 | min | no | S: ≤ `max_cumulative_excursion_min`. |
| `max_cumulative_excursion_min` | integer | yes | ≥ 1 | min | no | S: ≥ `max_single_excursion_min`. |
| `max_reading_gap_min` | integer | yes | ≥ 1 | min | no | S: positive. |
| `min_coverage_pct` | float | yes | (0, 100], 2 dp | % | no | S: range. |
| `effective_from` | date | yes | `YYYY-MM-DD` | — | no | S: versions of one `policy_code` must not overlap in time. |
| `effective_to` | date | no | `YYYY-MM-DD`; null = open-ended | — | no | S: after `effective_from` (exclusive end). |

### `products`

**Layer:** raw · **Primary key:** `product_id` · **Written by:** generator (from `config/reference/products.yaml`)

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `product_id` | string | yes | `PRD-0001` | — | PK | S: ID format. |
| `name` | string | yes | 1–80 chars | — | yes | S: non-empty, no surrounding whitespace. |
| `category` | string | yes | VACCINE, BIOLOGIC, MEDICINE, FROZEN, AMBIENT_PHARMA | — | no | S: enum. |
| `supplier_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties`, whose `party_type` is SUPPLIER. |
| `policy_code` | string | yes | REFRIG_2_8, COOL_8_15, FROZEN, AMBIENT_15_25 | — | no | S: enum. Logical reference to `threshold_policies.policy_code`; the version is resolved by effective date (see `compliance_rules.md`). |
| `shelf_life_days` | integer | yes | ≥ 1 | days | no | V: every lot's `expires_on − manufactured_on` equals this. |

### `packaging`

**Layer:** raw · **Primary key:** `packaging_id` · **Written by:** generator (from `config/reference/packaging.yaml`)

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `packaging_id` | string | yes | `PKG-0001` | — | PK | S: ID format. |
| `name` | string | yes | 1–80 chars | — | yes | S: non-empty, no surrounding whitespace. |
| `packaging_type` | string | yes | ACTIVE_REEFER, PASSIVE_GEL, DRY_ICE, INSULATED_BOX | — | no | S: enum. |
| `supported_policy_code` | string | yes | REFRIG_2_8, COOL_8_15, FROZEN, AMBIENT_15_25 | — | no | S: enum. |
| `setpoint_c` | float | yes | 2 dp | °C | no | Config load: inside `[min_c, max_c]` of every version of `supported_policy_code`. |
| `max_hold_hours` | float | yes | > 0, 2 dp | hours | no | V: ≥ planned trip duration of every shipment using it. |
| `thermal_response` | float | yes | [0, 1], 2 dp | factor | no | S: range. 0 = perfectly insulated, 1 = follows ambient immediately. |

### `lots`

**Layer:** raw · **Primary key:** `lot_id` · **Written by:** generator

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `lot_id` | string | yes | `LOT-2026-000001` | — | PK | S: ID format. |
| `product_id` | string | yes | `PRD-0001` | — | no | V: FK → `products`. |
| `manufactured_on` | date | yes | `YYYY-MM-DD` | — | no | V: before the departure date of every shipment carrying the lot. |
| `expires_on` | date | yes | `YYYY-MM-DD` | — | no | S: after `manufactured_on`. V: = `manufactured_on + products.shelf_life_days`; after the arrival date of every shipment carrying the lot. |
| `quantity_units` | integer | yes | ≥ 1 | units | no | V: ≥ Σ `shipment_lots.quantity_units` for the lot. |
| `lot_status` | string | yes | RELEASED, ON_HOLD | — | no | S: enum. V: only RELEASED lots are shipped. |

### `devices`

**Layer:** raw · **Primary key:** `device_id` · **Written by:** generator

A physical temperature logger, reused across shipments (DECISIONS.md #1).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `device_id` | string | yes | `DEV-000001` | — | PK | S: ID format. |
| `model` | string | yes | 1–40 chars | — | no | S: non-empty. |
| `serial_number` | string | yes | 1–40 chars | — | yes | S: unique, non-empty. |
| `calibrated_on` | date | yes | `YYYY-MM-DD` | — | no | E: after the assignment start date → CALIBRATION_EXPIRED. |
| `calibration_due_on` | date | yes | `YYYY-MM-DD` | — | no | S: after `calibrated_on`. E: before the assignment end date → CALIBRATION_EXPIRED (valid through the due date inclusive). |
| `uncertainty_c` | float | yes | (0, 1], 2 dp (typically 0.2–0.5) | °C | no | S: range. Used for NEAR_LIMIT_UNCERTAIN. |
| `reading_interval_min` | integer | yes | ≥ 1 | min | no | V: equals `reading_interval_min` in `config/default.yaml` (10). |

---

## Shipments and assignments

### `shipments`

**Layer:** raw · **Primary key:** `shipment_id` · **Written by:** generator

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `shipment_id` | string | yes | `SHP-2026-000001` | — | PK | S: ID format. |
| `supplier_party_id` | string | yes | `PTY-0001` | — | no | S: the four party IDs are distinct. V: FK → `parties` (SUPPLIER); supplier of every product in the shipment. |
| `carrier_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties` (CARRIER). |
| `receiver_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties` (RECEIVER). |
| `planner_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties` (PLANNER); the party that set `declared_policy_id`. |
| `packaging_id` | string | yes | `PKG-0001` | — | no | V: FK → `packaging`; `supported_policy_code` equals the products' `policy_code`. |
| `declared_policy_id` | string | yes | `THR-<CODE>-V<n>` | — | no | V: FK → `threshold_policies`. E: ≠ applied policy → POLICY_MISMATCH_DECLARED (planner input, may be wrong). |
| `planned_departure_at` | timestamp | yes | UTC | — | no | V: inside the config `date_window`. |
| `planned_arrival_at` | timestamp | yes | UTC | — | no | S: after `planned_departure_at`. V: trip duration within config `trip_duration_hours`. |
| `actual_departure_at` | timestamp | yes | UTC | — | no | V: = ORIGIN stop `departed_at`; ≥ logger `assigned_at`. |
| `actual_arrival_at` | timestamp | yes | UTC | — | no | S: after `actual_departure_at`. V: = DESTINATION stop `arrived_at`; ≤ logger `unassigned_at`. |
| `carrier_reported_status` | string | yes | PASS, FAIL | — | no | S: enum. Generator: PASS exactly when the expected action is RELEASE (DECISIONS.md #38). E: contradicts the engine's action → CARRIER_STATUS_CONTRADICTED (an input claim, may be wrong). |

### `shipment_lots`

**Layer:** raw · **Primary key:** (`shipment_id`, `lot_id`) · **Written by:** generator

Bridge table for the N:M relationship between shipments and lots.

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `lot_id` | V: FK → `shipments`; every shipment has ≥ 1 lot; all its lots share one `policy_code` (one container = one temperature class) and one supplier. |
| `lot_id` | string | yes | `LOT-2026-000001` | — | with `shipment_id` | V: FK → `lots`. |
| `quantity_units` | integer | yes | ≥ 1 | units | no | V: Σ per lot ≤ `lots.quantity_units`. |

### `loggers`

**Layer:** raw · **Primary key:** `logger_id` · **Written by:** generator

One device assigned to one shipment (1:1 with `shipments`, DECISIONS.md #1).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `logger_id` | string | yes | `LOG-<shipment-id>-01` | — | PK | S: equals `LOG-{shipment_id}-01`. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | yes | S: unique (1:1). V: FK → `shipments`; every shipment has a logger (E: missing → LOGGER_NOT_ASSIGNED). |
| `device_id` | string | yes | `DEV-000001` | — | no | V: FK → `devices`; one device's assignment windows never overlap. |
| `assigned_at` | timestamp | yes | UTC, on the reading grid | — | no | V: ≤ `actual_departure_at`; minute is a multiple of `reading_interval_min`. |
| `unassigned_at` | timestamp | yes | UTC, on the reading grid | — | no | S: after `assigned_at`. V: ≥ `actual_arrival_at`; on the reading grid. |

---

## Events

### `temperature_readings`

**Layer:** raw · **Primary key:** `reading_id` · **Written by:** generator (vectorised)

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `reading_id` | string | yes | `TEMP-<logger-id>-<YYYYMMDDTHHMM>` | — | PK | S: equals `TEMP-{logger_id}-{recorded_at:%Y%m%dT%H%M}`. |
| `logger_id` | string | yes | `LOG-<shipment-id>-01` | — | with `recorded_at` | V: FK → `loggers`. |
| `device_id` | string | yes | `DEV-000001` | — | no | V: FK → `devices`. E: ≠ the assignment's device → LOGGER_NOT_ASSIGNED; such readings are untrusted. |
| `recorded_at` | timestamp | yes | UTC, whole minutes | — | with `logger_id` | S: (`logger_id`, `recorded_at`) unique. V: inside the assignment window, on the reading grid. |
| `temperature_c` | float | yes | −100 to 100, 2 dp | °C | no | S: range, 2 dp. |

### `route_stops`

**Layer:** raw · **Primary key:** `stop_id` · **Written by:** generator

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `stop_id` | string | yes | `RST-<shipment-id>-<nn>` | — | PK | S: equals `RST-{shipment_id}-{sequence:02d}`. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `sequence` | V: FK → `shipments`. |
| `sequence` | integer | yes | 1–99 | — | with `shipment_id` | V: contiguous 1..n with n ≥ 2; ORIGIN is 1 and DESTINATION is n. |
| `stop_type` | string | yes | ORIGIN, WAREHOUSE, CROSS_DOCK, DESTINATION | — | no | S: enum. |
| `party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties`; ORIGIN = shipment supplier, DESTINATION = receiver, WAREHOUSE/CROSS_DOCK = a WAREHOUSE party. |
| `arrived_at` | timestamp | no | UTC; null for ORIGIN | — | no | S: null exactly when `stop_type` = ORIGIN. V: DESTINATION `arrived_at` = `actual_arrival_at`; stops are chronological. |
| `departed_at` | timestamp | no | UTC; null for DESTINATION | — | no | S: null exactly when `stop_type` = DESTINATION; not before `arrived_at`. V: ORIGIN `departed_at` = `actual_departure_at`. |
| `dwell_min` | integer | no | ≥ 0; null when either timestamp is null | min | no | S: derived = `departed_at − arrived_at`. E: > `long_dwell_threshold_min` → LONG_DWELL. |

### `custody_events`

**Layer:** raw · **Primary key:** `custody_event_id` · **Written by:** generator

The standard chain: PICKUP at ORIGIN (supplier → carrier); at every intermediate stop, a HANDOFF
carrier → warehouse at `arrived_at` and a HANDOFF warehouse → carrier at `departed_at`; DELIVERY at
DESTINATION (carrier → receiver).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `custody_event_id` | string | yes | `CUS-<shipment-id>-<nn>` | — | PK | S: equals `CUS-{shipment_id}-{sequence:02d}`. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `sequence` | V: FK → `shipments`. |
| `stop_id` | string | yes | `RST-<shipment-id>-<nn>` | — | no | S: embeds the same `shipment_id`. V: FK → `route_stops`. |
| `sequence` | integer | yes | 1–99 | — | with `shipment_id` | E: not contiguous 1..n → CUSTODY_GAP. |
| `event_type` | string | yes | PICKUP, HANDOFF, DELIVERY | — | no | S: enum. E: first event not PICKUP from the supplier, or last not DELIVERY to the receiver → CUSTODY_GAP. |
| `from_party_id` | string | yes | `PTY-0001` | — | no | S: ≠ `to_party_id`. V: FK → `parties`. E: ≠ previous event's `to_party_id` → CUSTODY_GAP. |
| `to_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties`. |
| `event_at` | timestamp | yes | UTC | — | no | V: strictly increasing with `sequence`; equals the stop's arrival/departure time. |

### `shipment_status_events`

**Layer:** raw · **Primary key:** `status_event_id` · **Written by:** generator

Append-only lifecycle history (never updated in place).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `status_event_id` | string | yes | `STE-<shipment-id>-<nn>` | — | PK | S: equals `STE-{shipment_id}-{sequence:02d}`. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `sequence` | V: FK → `shipments`. |
| `sequence` | integer | yes | 1–99 | — | with `shipment_id` | V: contiguous 1..n. |
| `status` | string | yes | PLANNED, PICKED_UP, IN_TRANSIT, AT_STOP, DELIVERED | — | no | S: generator statuses only. V: transitions follow the lifecycle below. |
| `event_at` | timestamp | yes | UTC | — | no | V: non-decreasing with `sequence`; PLANNED = `planned_departure_at` − `planning_lead_hours` (config); PICKED_UP = `actual_departure_at`, DELIVERED = `actual_arrival_at`; AT_STOP / IN_TRANSIT = the stop's `arrived_at` / `departed_at`. |
| `actor_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties`; PLANNED → planner, PICKED_UP/IN_TRANSIT → carrier, AT_STOP → the stop's party, DELIVERED → receiver. |

---

## Compliance engine output

### `excursion_reports`

**Layer:** compliance · **Primary key:** `report_id` · **Written by:** compliance engine

One row per excursion (a maximal run of same-direction out-of-range trusted readings within one
logger assignment). IDs are numbered in order of (`shipment_id`, `started_at`, `direction`).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `report_id` | string | yes | `EXCR-2026-000001` | — | PK | S: ID format. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | no | V: FK → `shipments`. |
| `logger_id` | string | yes | `LOG-<shipment-id>-01` | — | no | S: embeds the same `shipment_id`. V: FK → `loggers`. |
| `policy_id` | string | yes | `THR-<CODE>-V<n>` | — | no | V: FK → `threshold_policies`; = the decision's `applied_policy_id`. |
| `direction` | string | yes | HIGH, LOW | — | no | S: enum. |
| `started_at` | timestamp | yes | UTC | — | no | S: `first_reading_id` is the reading at this time. |
| `ended_at` | timestamp | yes | UTC | — | no | S: ≥ `started_at`; `last_reading_id` is the reading at this time. |
| `duration_min` | integer | yes | ≥ 1 | min | no | S: > `ended_at − started_at`. V: = `ended_at − started_at + reading_interval_min`, recomputed from the readings. |
| `peak_c` | float | yes | 2 dp | °C | no | V: max (HIGH) / min (LOW) temperature of the linked readings. |
| `reading_count` | integer | yes | ≥ 1 | readings | no | S: = 1 exactly when `started_at` = `ended_at`. V: = number of linked readings. |
| `first_reading_id` | string | yes | reading ID | — | no | S: = `TEMP-{logger_id}-{started_at}`. V: FK → `temperature_readings`. |
| `last_reading_id` | string | yes | reading ID | — | no | S: = `TEMP-{logger_id}-{ended_at}`. V: FK → `temperature_readings`. |
| `breach_type` | string | yes | WITHIN_ALLOWANCE, SINGLE_DURATION_BREACH, ABSOLUTE_LIMIT_BREACH | — | no | S: enum. V: recomputed per `compliance_rules.md`. |

### `excursion_report_readings`

**Layer:** compliance · **Primary key:** (`report_id`, `reading_id`) · **Written by:** compliance engine

Bridge table and traceability link from a report to its readings (raw readings are never modified).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `report_id` | string | yes | `EXCR-2026-000001` | — | with `reading_id` | V: FK → `excursion_reports`. |
| `reading_id` | string | yes | reading ID | — | yes | S: a reading belongs to at most one report. V: FK → `temperature_readings`; same logger as the report. |

### `compliance_decisions`

**Layer:** compliance · **Primary key:** `shipment_id` · **Written by:** compliance engine

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `shipment_id` | string | yes | `SHP-2026-000001` | — | PK | V: FK → `shipments`; exactly one decision per shipment. |
| `applied_policy_id` | string | yes | `THR-<CODE>-V<n>` | — | no | V: FK → `threshold_policies`; resolved from the products and `actual_departure_at`. |
| `policy_source` | string | yes | DECLARED_CONFIRMED, PRODUCT_OVERRIDE | — | no | S: PRODUCT_OVERRIDE exactly when `reason_codes` contains POLICY_MISMATCH_DECLARED. |
| `status` | string | yes | COMPLIANT, COMPLIANT_WITH_WARNINGS, INCONCLUSIVE, NON_COMPLIANT | — | no | S: = the most severe status triggered by `reason_codes`. |
| `reason_codes` | string | no | `\|`-list of reason codes; null = none | — | no | S: known codes, distinct, canonical order. |
| `reading_count` | integer | yes | ≥ 0 | readings | no | S: ≤ `expected_reading_count`. V: = trusted in-window readings. |
| `expected_reading_count` | integer | yes | ≥ 1 | readings | no | V: = window / `reading_interval_min` + 1. |
| `coverage_pct` | float | yes | [0, 100], 2 dp | % | no | S: = round(100 × `reading_count` / `expected_reading_count`, 2). |
| `max_gap_min` | integer | yes | ≥ 0 | min | no | V: recomputed from the readings (window edges included). |
| `total_excursion_min` | integer | yes | ≥ 0 | min | no | S: 0 exactly when `excursion_report_ids` is null. V: = Σ linked `duration_min`. |
| `excursion_report_ids` | string | no | `\|`-list of `EXCR-…` IDs; null = none | — | no | S: distinct, valid IDs. V: each is a report of this shipment. |
| `decided_action` | string | yes | RELEASE, QUARANTINE, REVIEW | — | no | S: COMPLIANT/COMPLIANT_WITH_WARNINGS → RELEASE, INCONCLUSIVE → REVIEW, NON_COMPLIANT → QUARANTINE. |

### `compliance_status_events`

**Layer:** compliance · **Primary key:** `status_event_id` · **Written by:** compliance engine

The engine's lifecycle events, appended to the shipment's history in a separate file (the raw
history is never overwritten). Same columns as `shipment_status_events`.

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `status_event_id` | string | yes | `STE-<shipment-id>-<nn>` | — | PK | S: equals `STE-{shipment_id}-{sequence:02d}`. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `sequence` | V: FK → `shipments`. |
| `sequence` | integer | yes | 1–99 | — | with `shipment_id` | V: continues the raw history's numbering (last raw + 1, …). |
| `status` | string | yes | UNDER_REVIEW, RELEASED, QUARANTINED | — | no | S: engine statuses only. V: UNDER_REVIEW first, then at most one RELEASED or QUARANTINED. |
| `event_at` | timestamp | yes | UTC | — | no | V: = logger `unassigned_at` (derived, never wall-clock); ≥ DELIVERED `event_at`. |
| `actor_party_id` | string | yes | `PTY-0001` | — | no | V: FK → `parties`; UNDER_REVIEW → a LOGISTICS_ANALYST, RELEASED/QUARANTINED → an OPERATIONS_MANAGER. |

---

## Ground truth (answer key; never read by the engine)

### `expected_outcomes`

**Layer:** ground_truth · **Primary key:** `shipment_id` · **Written by:** generator (natural scenarios), updated by the injector

The expected engine result per shipment, derived from the documented rules (not random).

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `shipment_id` | string | yes | `SHP-2026-000001` | — | PK | V: FK → `shipments`; one row per shipment. |
| `scenario` | string | yes | NORMAL, MINOR_SPIKE_WITHIN_ALLOWANCE, BOUNDARY, EXCURSION_BREACH, LONG_DWELL, CALIBRATION_EXPIRED | — | no | S: enum. |
| `issue_type` | string | no | logger_not_assigned, product_threshold_mismatch, missing_data_silently_passed, custody_gap_not_flagged, excursion_across_unrelated_shipments; null = not injected | — | no | S: enum. V: non-null exactly for shipments in `anomalies`. |
| `expected_status` | string | yes | COMPLIANT, COMPLIANT_WITH_WARNINGS, INCONCLUSIVE, NON_COMPLIANT | — | no | S: = the most severe status of `expected_reason_codes`. |
| `expected_action` | string | yes | RELEASE, QUARANTINE, REVIEW | — | no | S: follows from `expected_status`. |
| `expected_reason_codes` | string | no | `\|`-list of reason codes; null = none | — | no | S: known codes, distinct, canonical order. |
| `expected_excursion_count` | integer | yes | ≥ 0 | excursions | no | S: = number of windows = number of durations. |
| `expected_total_excursion_min` | integer | yes | ≥ 0 | min | no | S: = Σ `expected_excursion_durations_min`. |
| `expected_excursion_windows` | string | no | `\|`-list of `<start>/<end>` (ISO-8601 UTC, first/last reading); null = none | — | no | S: format; each end ≥ start. |
| `expected_excursion_durations_min` | string | no | `\|`-list of integers, aligned with the windows; null = none | min | no | S: format. |

### `device_truth`

**Layer:** ground_truth · **Primary key:** `device_id` · **Written by:** generator

The hidden systematic offset of every physical device, used by the temperature model
(DECISIONS.md #54). It explains why one device reads consistently warm or cold. It is ground
truth only: the compliance engine never reads it, because a real engine cannot know a sensor's
true bias, only its stated `uncertainty_c`.

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `device_id` | string | yes | `DEV-000001` | — | PK | V: FK → `devices`; one row per device. |
| `systematic_offset_c` | float | yes | −1.00 to 1.00, 2 dp | °C | no | S: range, 2 dp. V: \|offset\| ≤ the device's `uncertainty_c`. |

### `anomalies`

**Layer:** ground_truth · **Primary key:** (`anomaly_id`, `shipment_id`) · **Written by:** injector

One row per (injected anomaly, affected shipment): an anomaly that touches two shipments
(`excursion_across_unrelated_shipments`) has two rows sharing one `anomaly_id`.

| Field | Type | Required | Allowed values / format | Unit | Unique | Validation rule |
|---|---|---|---|---|---|---|
| `anomaly_id` | string | yes | `ANOM-000001` | — | with `shipment_id` | S: ID format. |
| `shipment_id` | string | yes | `SHP-2026-000001` | — | with `anomaly_id` | V: FK → `shipments`; anomalies of different types use disjoint shipments. |
| `issue_type` | string | yes | the five issue types (see `expected_outcomes.issue_type`) | — | no | S: enum. V: = `expected_outcomes.issue_type` of the shipment. |
| `description` | string | yes | 1–300 chars | — | no | S: non-empty. |
| `affected_record_ids` | string | no | `\|`-list of existing record IDs (any entity) | — | no | S: valid, distinct IDs. V: every ID exists. |
| `removed_record_ids` | string | no | `\|`-list of IDs deleted by the injection | — | no | S: valid, distinct IDs. V: none of them exists any more. |
| `expected_status` | string | yes | COMPLIANT, COMPLIANT_WITH_WARNINGS, INCONCLUSIVE, NON_COMPLIANT | — | no | S: = most severe status of `expected_reason_codes`. V: = `expected_outcomes.expected_status`. |
| `expected_reason_codes` | string | no | `\|`-list of reason codes | — | no | S: known codes, distinct, canonical order. V: = `expected_outcomes.expected_reason_codes`. |
| `expected_excursion_durations_min` | string | no | `\|`-list of integers | min | no | S: format. V: = `expected_outcomes.expected_excursion_durations_min`. |

---

## Relationships and cardinality

Single-column foreign keys are listed in `schemas.FOREIGN_KEYS`. "Optional" means the child row
may exist without the relationship, or the parent may have zero children.

| Parent | Child (FK column) | Cardinality | Optional? | Notes |
|---|---|---|---|---|
| `parties` | `products.supplier_party_id` | 1 : N | a party may supply 0 products | SUPPLIER parties only. |
| `parties` | `shipments.{supplier,carrier,receiver,planner}_party_id` | 1 : N (×4 roles) | a party may be in 0 shipments | Four separate relationships. |
| `parties` | `route_stops.party_id` | 1 : N | — | |
| `parties` | `custody_events.{from,to}_party_id` | 1 : N (×2) | — | |
| `parties` | `shipment_status_events.actor_party_id`, `compliance_status_events.actor_party_id` | 1 : N | — | |
| `threshold_policies` | `products.policy_code` (logical) | N : M over time, 1 : 1 at any instant | — | A product's code maps to exactly one version at a given time (effective dates). |
| `threshold_policies` | `shipments.declared_policy_id` | 1 : N | — | Planner's claim. |
| `threshold_policies` | `excursion_reports.policy_id`, `compliance_decisions.applied_policy_id` | 1 : N | — | Engine's resolved policy. |
| `products` | `lots.product_id` | 1 : N | a product may have 0 lots | |
| `lots` ↔ `shipments` | `shipment_lots` (bridge) | N : M | a lot may be unshipped; a shipment has ≥ 1 lot | Same `policy_code` within a shipment. |
| `packaging` | `shipments.packaging_id` | 1 : N | — | |
| `shipments` | `loggers.shipment_id` | 1 : 1 | required in valid data | Absence → LOGGER_NOT_ASSIGNED. |
| `devices` | `loggers.device_id` | 1 : N | a device may be unused | Non-overlapping windows per device. |
| `loggers` | `temperature_readings.logger_id` | 1 : N | — | |
| `devices` | `temperature_readings.device_id` | 1 : N | — | Normally the logger's device. |
| `shipments` | `route_stops.shipment_id` | 1 : N (≥ 2) | — | ORIGIN … DESTINATION. |
| `route_stops` | `custody_events.stop_id` | 1 : N (0–2 per stop) | a stop may have no custody event only if one was deleted | |
| `shipments` | `custody_events.shipment_id`, `shipment_status_events.shipment_id` | 1 : N | — | |
| `shipments` | `excursion_reports.shipment_id` | 1 : N | 0 excursions is normal | |
| `excursion_reports` ↔ `temperature_readings` | `excursion_report_readings` (bridge) | 1 : N (a reading is in ≤ 1 report) | most readings are in no report | Traceability link. |
| `shipments` | `compliance_decisions.shipment_id` | 1 : 1 | — | |
| `shipments` | `compliance_status_events.shipment_id` | 1 : N (1–2) | — | |
| `shipments` | `expected_outcomes.shipment_id` | 1 : 1 | — | Ground truth. |
| `devices` | `device_truth.device_id` | 1 : 1 | — | Ground truth (hidden sensor offset). |
| `shipments` | `anomalies.shipment_id` | 1 : N (0 or 1 in practice) | most shipments have none | Ground truth. |

## Effective dates

| Entity | Fields | Meaning |
|---|---|---|
| `threshold_policies` | `effective_from`, `effective_to` | Half-open window `[from, to)` in UTC; null `to` = open-ended; versions of one code never overlap. REFRIG_2_8 V1 is effective until 2026-07-01 and V2 from then on. |
| `parties` | `active_from` | A party may only appear in records dated on/after this date. |
| `lots` | `manufactured_on`, `expires_on` | A lot may only ship between these dates. |
| `devices` | `calibrated_on`, `calibration_due_on` | Calibration is valid from `calibrated_on` through `calibration_due_on` inclusive; the device must be valid for the whole assignment window. |
| `loggers` | `assigned_at`, `unassigned_at` | The assignment window: the only time range in which a reading counts for the shipment. |

## Lifecycle states

**Shipment** (`shipment_status_events` + `compliance_status_events`, append-only):

```
PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → DELIVERED            (generator, raw)
DELIVERED → UNDER_REVIEW → RELEASED | QUARANTINED                 (compliance engine)
```

`IN_TRANSIT → AT_STOP → IN_TRANSIT` repeats once per intermediate stop. A REVIEW decision leaves
the shipment in UNDER_REVIEW (no further event: it waits for a human).

**Lot:** `lot_status` ∈ {RELEASED, ON_HOLD} is a snapshot; ON_HOLD lots are never shipped.

**Device calibration:** valid within its calibration dates, otherwise expired (derived, not stored).

**Threshold policy:** a version is *effective* inside its window and *superseded* afterwards
(derived from the effective dates, not stored).
