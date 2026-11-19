# MASTER PROMPT — YottaFlex Cold Chain Compliance Project

You are acting as a **Senior Software Engineer** working with me on this project. Read this entire prompt before doing anything.

---

## 0. How we will work (READ FIRST — these rules are mandatory)

1. **Working directory:** everything lives inside `/home/allencharles/Documents/YottaFlex`. Never create, modify or delete files outside this folder.
2. **Save this plan first:** before writing any code, save this entire prompt verbatim to `docs/PROJECT_PLAN.md`. It is the single source of truth for the whole project. In later sessions I will say *"Proceed with Phase N per docs/PROJECT_PLAN.md"*.
3. **One phase at a time.** Only implement the phase I ask for. Do not start the next phase early, and do not "pre-build" future phases.
4. **Think before coding.** At the start of each phase, give me a short plan (files to create/change, key design decisions, risks). Then implement it.
5. **End of every phase, you must:**
   - Run `ruff check .`, `ruff format --check .` and `pytest`, and fix anything that fails.
   - Run the phase's demo command(s) yourself and show me the key output.
   - Print a **Phase Summary**: what was built, files created/changed, design decisions, known limitations.
   - Print the **User Acceptance Test (UAT)** steps for that phase (listed in this plan), so I can test it myself.
   - Propose a git commit message.
   - Print a **Phase Status Report** using the exact template in section 0.1, as the very last thing in your reply, inside one fenced code block so I can copy it in one go.
   - Then **STOP and wait** for me to reply `Phase N approved`. After approval, make the git commit.
6. **If something in the plan is ambiguous or contradictory,** stop and ask me. Don't guess silently. Record every decision we make in `docs/DECISIONS.md` (date, decision, reason).
7. **Develop on `starter` scale.** Only run `full` scale when the phase says so.
8. **Never edit generated output by hand.** All data comes from code.
9. **External review loop.** I will copy your Phase Status Report to an external reviewer, run the tests they give me, and only approve the phase once the reviewer gives the green light. If I paste back review feedback or fix requests, apply them, re-run the checks, and print an updated Phase Status Report (with `Revision: N` incremented) before waiting again.

### 0.1 Phase Status Report template (use exactly this structure, every phase)

```
=== YOTTAFLEX PHASE STATUS REPORT ===
Phase: <number> — <name>
Revision: <1, 2, 3...>
Status: <READY FOR REVIEW | BLOCKED>
Date: <YYYY-MM-DD>

1. SCOPE COMPLETED
- <bullet per deliverable from the plan, marked [x] done or [ ] not done>

2. FILES CREATED / MODIFIED
- <path> — <created|modified> — <one-line purpose>

3. KEY DESIGN DECISIONS
- <decision> — <reason> (also logged in docs/DECISIONS.md: yes/no)

4. DEVIATIONS FROM PLAN
- <what differs from docs/PROJECT_PLAN.md and why, or "None">

5. AUTOMATED CHECKS
- ruff check: <PASS/FAIL>
- ruff format --check: <PASS/FAIL>
- pytest: <X passed, Y failed, Z skipped> | coverage: <N%>
- Test files: <list>

6. DEMO COMMAND OUTPUT (summary)
- Command(s) run: <exact commands>
- Key results: <row counts per table, timings, notable numbers>

7. KNOWN ISSUES / LIMITATIONS
- <issue or "None">

8. OPEN QUESTIONS FOR REVIEWER
- <question or "None">

9. ENVIRONMENT
- Python: <version> | OS: <from `uname -sr`> | Package version: <x.y.z>
- Git: <branch> @ <short commit hash or "uncommitted">

10. USER ACCEPTANCE TEST (UAT) STEPS
- <numbered, copy-pasteable commands with the expected result of each>

11. PROPOSED COMMIT MESSAGE
- <message>
=== END OF REPORT ===
```

---

## 1. Engineering principles (apply to every line of code)

- **Readability:** clean, simple, self-explanatory code. Clear names over clever tricks. Docstrings on every public function/class (Google style). Type hints everywhere.
- **Maintainability:** small modules with one responsibility. Configuration lives in `config/`, not hard-coded.
- **Scalability:** the same code handles 50 or 5,000 shipments. Vectorise heavy work (numpy/pandas). No Python loops over individual temperature readings.
- **Simplicity / DRY:** no over-engineering, no premature abstraction, no duplicated logic. One place for ID formatting, one place for schemas, one place for config loading.
- **Modularity:** pure functions where possible (input DataFrames → output DataFrames). I/O only at the edges (`io.py` / CLI).
- **Best practices:** PEP 8, `pyproject.toml`, `src/` layout, `logging` (no `print` in library code), pathlib, enums for allowed values.
- **Performance:** efficient but readable. Full scale (5,000 shipments, ~700k+ readings) must run in a few minutes on a laptop.
- **Error handling:** validate config on load, with clear error messages. Fail fast on broken invariants, with custom exceptions in `errors.py`. Handle empty inputs and edge cases explicitly.
- **Consistency:** the same naming, formatting and patterns across all modules. snake_case columns. UTC ISO-8601 timestamps. Temperatures in °C as floats rounded to 2 decimals.
- **Testing:** every module gets pytest tests, including boundary cases.

---

## 2. Project brief (what we are building)

The original brief is saved at `docs/brief.md` (I will place it there). In summary:

**Goal:** prove that temperature-sensitive shipments stayed within product and route requirements, and manage deviations.

**What we build:** a **reproducible Python data pipeline** (not a live tracking app, and no logins) that:
1. **Generates** realistic synthetic master data and event data (products, lots, shipments, packaging, loggers, temperature readings every 10 minutes, custody handoffs, route stops).
2. **Injects** deliberate, labelled anomalies (edge cases) and records them in a separate ground-truth answer key.
3. **Checks compliance**: a compliance engine that, *without seeing the ground truth*, decides per shipment whether it was compliant, and traces every conclusion to specific readings and the specific policy.
4. **Validates** data integrity (PKs, FKs, enums, date ordering, reconciliation, lifecycle transitions, duplicates).
5. **Evaluates** the compliance engine against the ground truth (precision/recall per anomaly type).
6. **Exports** CSV / JSON / SQL, plus README, data dictionary, manifest, validation summary.
7. **Proves reproducibility**: same seed → identical row counts and checksums.
8. (Optional final phase) a **Streamlit viewer**: read-only, no authentication.

**Actors** (they appear as *data*, i.e. parties in the tables, not as app users): Supplier, Planner, Carrier/Warehouse operator, Logistics analyst, Operations manager.

**Baseline volume:** 5,000 shipments, 1,000 logger devices, readings every 10 minutes. Default seed: `20261022`.

**Acceptance criteria:** the compliance report traces each conclusion to readings and policy; gaps are flagged; excursion duration is reproducible; lot and shipment links remain intact.

---

## 3. Tech stack

| Purpose | Tool |
|---|---|
| Language | Python 3.12 (project venv; system Python 3.15 is NOT used) |
| Packaging | `pyproject.toml`, `src/` layout, editable install in `.venv` |
| Data | pandas, numpy (`np.random.Generator` only) |
| Columnar I/O | pyarrow (intermediate Parquet) |
| Schema validation | pandera |
| Config | PyYAML + pydantic (typed config model) |
| SQL / join checks | duckdb |
| CLI | typer |
| Tests | pytest, pytest-cov |
| Lint/format | ruff |
| Dashboard (Phase 9 only) | streamlit, plotly |

The OS is Fedora Linux. The system Python is 3.15, which is too new for reliable wheels of pandas/pyarrow/duckdb/pandera. So this project uses **Python 3.12** in a venv: `python3.12 -m venv .venv` and `pip install -e ".[dev]"`. Set `requires-python = ">=3.11,<3.14"` in pyproject.toml. Always run commands inside the activated `.venv`.

---

## 4. Target folder structure

```
YottaFlex/
├── CLAUDE.md                     # Short rules summary for future sessions
├── README.md
├── pyproject.toml
├── .gitignore                    # .venv, output/, __pycache__, .pytest_cache, *.parquet
├── config/
│   ├── default.yaml              # seed, scales, scenario weights, injection rates
│   └── reference/
│       ├── threshold_policies.yaml
│       ├── products.yaml         # product catalogue templates
│       ├── packaging.yaml
│       └── locations.yaml        # Indian cities/hubs used for routes
├── docs/
│   ├── brief.md
│   ├── PROJECT_PLAN.md           # this prompt
│   ├── DECISIONS.md
│   ├── data_dictionary.md
│   ├── er_diagram.md             # Mermaid ER diagram
│   ├── compliance_rules.md       # decision table + formulas
│   └── anomaly_catalogue.md      # each injected edge case explained
├── src/coldchain/
│   ├── __init__.py
│   ├── cli.py                    # typer entry point: `coldchain ...`
│   ├── config.py                 # load + validate config (pydantic)
│   ├── errors.py                 # custom exceptions
│   ├── ids.py                    # ALL ID formatting lives here
│   ├── enums.py                  # all allowed values
│   ├── schemas.py                # pandera schemas for every table
│   ├── rng.py                    # seeded, per-stage RNG streams
│   ├── io.py                     # read/write tables (deterministic ordering)
│   ├── generate/
│   │   ├── __init__.py
│   │   ├── master.py             # parties, products, policies, packaging, lots, devices
│   │   ├── shipments.py          # shipments, shipment_lots, route stops, logger assignments
│   │   ├── events.py             # custody events, status events
│   │   ├── readings.py           # temperature readings (vectorised)
│   │   └── scenarios.py          # natural scenario assignment (normal, spike, dwell...)
│   ├── inject/
│   │   ├── __init__.py
│   │   └── anomalies.py          # the 5 edge-case injectors + ground truth rows
│   ├── compliance/
│   │   ├── __init__.py
│   │   ├── policy.py             # policy resolution (effective dates, product match)
│   │   ├── excursions.py         # excursion detection + duration
│   │   ├── gaps.py               # reading gaps, coverage
│   │   ├── custody.py            # custody chain checks
│   │   ├── logger_check.py       # logger assignment/calibration checks
│   │   └── report.py             # shipment decisions + excursion reports + traces
│   ├── validation/
│   │   ├── __init__.py
│   │   └── checks.py             # PK/FK/enum/date/reconciliation/lifecycle/duplicates
│   ├── evaluation/
│   │   ├── __init__.py
│   │   └── metrics.py            # compare engine output vs ground truth
│   └── export/
│       ├── __init__.py
│       ├── exporters.py          # CSV / JSON / SQL
│       ├── manifest.py           # row counts, sha256, seed, command
│       └── reproducibility.py    # run twice, compare
├── tests/
│   ├── conftest.py               # shared starter-scale fixtures
│   └── test_*.py                 # one test file per module
├── app/                          # Phase 9 only
│   └── streamlit_app.py
└── output/                       # generated, git-ignored
    └── <scale>/
        ├── raw/                  # input tables (no labels!)
        ├── ground_truth/
        ├── compliance/
        ├── validation/
        ├── evaluation/
        └── export/{csv,json,sql}/
```

Create folders and files only in the phase that needs them. Don't create empty placeholder modules for future phases.

---

## 5. Data model (design contract)

### 5.1 ID conventions (immutable, zero-padded; implemented ONLY in `ids.py`)

| Entity | Format | Example |
|---|---|---|
| Party | `PTY-0001` | PTY-0042 |
| Product | `PRD-0001` | PRD-0007 |
| ThresholdPolicy | `THR-<CODE>-V<n>` | THR-REFRIG_2_8-V1 |
| Packaging | `PKG-0001` | PKG-0003 |
| Lot | `LOT-2026-000001` | LOT-2026-000123 |
| Device (physical logger) | `DEV-000001` | DEV-000981 |
| Shipment | `SHP-2026-000001` | SHP-2026-004999 |
| Logger (assignment) | `LOG-<shipment-id>-01` | LOG-SHP-2026-000001-01 |
| TemperatureReading | `TEMP-<logger-id>-<YYYYMMDDTHHMM>` | TEMP-LOG-SHP-2026-000001-01-20260310T0910 |
| RouteStop | `RST-<shipment-id>-<nn>` | RST-SHP-2026-000001-02 |
| CustodyEvent | `CUS-<shipment-id>-<nn>` | CUS-SHP-2026-000001-03 |
| ShipmentStatusEvent | `STE-<shipment-id>-<nn>` | STE-SHP-2026-000001-04 |
| ExcursionReport | `EXCR-2026-000001` | EXCR-2026-000057 |
| Ground-truth anomaly | `ANOM-000001` | ANOM-000014 |

**Resolved brief contradiction (record it in DECISIONS.md):** the brief asks for 1,000 loggers but 5,000 shipments, and uses `LOG-<shipment-id>-01`. So:
- **Device** = a physical logger (1,000 of them, reused across shipments, holding calibration data).
- **Logger** = the assignment of one device to one shipment (5,000 rows, one per shipment, using the brief's ID format).
- A device may never have two assignments with overlapping time windows (except where an anomaly is deliberately injected).

### 5.2 Entities

Required by the brief: Product, Lot, Shipment, Packaging, Logger, TemperatureReading, CustodyEvent, RouteStop, ThresholdPolicy, ExcursionReport.
Supporting tables: Party, Device, ShipmentLot (bridge), ShipmentStatusEvent, ExcursionReportReading (bridge), ComplianceDecision.

Minimum fields (refine in Phase 1; every field needs a type, required/optional, allowed values, unit, uniqueness and validation rule in the data dictionary):

- **Party**: party_id (PK), party_type {SUPPLIER, CARRIER, WAREHOUSE, RECEIVER}, name, city, active_from.
- **ThresholdPolicy**: policy_id (PK), policy_code, version, min_c, max_c, abs_min_c, abs_max_c, max_single_excursion_min, max_cumulative_excursion_min, max_reading_gap_min, min_coverage_pct, effective_from, effective_to (nullable = open-ended). Rule: versions of the same policy_code must not overlap in time.
- **Product**: product_id (PK), name, category {VACCINE, BIOLOGIC, MEDICINE, FROZEN, AMBIENT_PHARMA}, supplier_party_id (FK Party), policy_code.
- **Lot**: lot_id (PK), product_id (FK), manufactured_on, expires_on (> manufactured_on), quantity_units (>0), lot_status {RELEASED, ON_HOLD}.
- **Packaging**: packaging_id (PK), packaging_type {ACTIVE_REEFER, PASSIVE_GEL, DRY_ICE, INSULATED_BOX}, supported_policy_code, setpoint_c, max_hold_hours, thermal_response (a 0–1 factor for how fast the inside temperature follows the ambient temperature).
- **Device**: device_id (PK), model, serial_number (unique), calibrated_on, calibration_due_on, uncertainty_c (e.g. 0.2–0.5), reading_interval_min = 10.
- **Shipment**: shipment_id (PK), supplier_party_id, carrier_party_id, receiver_party_id (FKs), packaging_id (FK), declared_policy_id (FK, set by the planner and may be wrong; see anomalies), planned_departure_at, planned_arrival_at, actual_departure_at, actual_arrival_at, carrier_reported_status {PASS, FAIL} (an input claim from the carrier, which may be wrong).
- **ShipmentLot** (bridge, N:M): shipment_id + lot_id (composite PK), quantity_units. Rule: all lots in one shipment share the same policy_code (one container = one temperature class). Sum of shipped quantity per lot ≤ lot quantity.
- **Logger** (assignment): logger_id (PK), shipment_id (FK, unique → 1:1 with Shipment), device_id (FK), assigned_at, unassigned_at.
- **TemperatureReading**: reading_id (PK), logger_id (FK), device_id (FK), recorded_at, temperature_c. Unique on (logger_id, recorded_at).
- **RouteStop**: stop_id (PK), shipment_id (FK), sequence (1..n), stop_type {ORIGIN, WAREHOUSE, CROSS_DOCK, DESTINATION}, party_id (FK), arrived_at, departed_at, dwell_min (derived = departed_at − arrived_at).
- **CustodyEvent**: custody_event_id (PK), shipment_id (FK), stop_id (FK), sequence, event_type {PICKUP, HANDOFF, DELIVERY}, from_party_id, to_party_id, event_at. Rule: the chain is continuous (event k's to_party = event k+1's from_party), and the timestamps increase.
- **ShipmentStatusEvent** (append-only history): status_event_id (PK), shipment_id, status, event_at, actor_party_id. Lifecycle: `PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → DELIVERED` (generator); then `UNDER_REVIEW → RELEASED | QUARANTINED` (appended by the compliance engine into a separate output file; raw input is never overwritten).
- **ExcursionReport**: report_id (PK), shipment_id, logger_id, policy_id, direction {HIGH, LOW}, started_at, ended_at, duration_min, peak_c, reading_count, first_reading_id, last_reading_id, breach_type {WITHIN_ALLOWANCE, SINGLE_DURATION_BREACH, ABSOLUTE_LIMIT_BREACH}.
- **ExcursionReportReading** (bridge): report_id + reading_id. This is the traceability link.
- **ComplianceDecision**: shipment_id (PK), applied_policy_id, policy_source, status, reason_codes (pipe-separated), coverage_pct, max_gap_min, total_excursion_min, excursion_report_ids, decided_action {RELEASE, QUARANTINE, REVIEW}.

### 5.3 Reference threshold policies (V1, effective 2026-01-01)

| policy_code | min_c | max_c | abs_min_c | abs_max_c | max single (min) | max cumulative (min) |
|---|---|---|---|---|---|---|
| REFRIG_2_8 | 2 | 8 | 0 | 12 | 30 | 60 |
| COOL_8_15 | 8 | 15 | 5 | 20 | 20 | 60 |
| FROZEN | -24 | -18 | -30 | -15 | 30 | 60 |
| AMBIENT_15_25 | 15 | 25 | 10 | 30 | 60 | 120 |

All policies: max_reading_gap_min = 30, min_coverage_pct = 95.
Add **one V2 version** of REFRIG_2_8 effective 2026-07-01 (e.g. max single excursion 20 min), so that effective-date policy resolution is genuinely tested.

---

## 6. Compliance rules (the decision table; document in `docs/compliance_rules.md`)

Definitions:
- **Applied policy** = the policy whose policy_code matches the **product(s)** in the shipment and whose effective window contains `actual_departure_at`. The planner's `declared_policy_id` is checked against it but is never trusted on its own.
- **Out-of-range reading** = `temperature_c > max_c` or `< min_c`.
- **Excursion** = a maximal run of consecutive out-of-range readings (same direction) *within one logger assignment, inside the shipment's assignment window*.
- **Excursion duration (min)** = `(last_reading_at − first_reading_at) + reading_interval_min`. Document this formula; it must be deterministic.
- **Gap** = the difference between consecutive readings > `reading_interval_min`. A gap breaches the policy when it exceeds `max_reading_gap_min`.
- **Coverage %** = actual readings / expected readings over the assignment window.
- **Calibration uncertainty** = a reading within ± `uncertainty_c` of a limit gets the warning `NEAR_LIMIT_UNCERTAIN`.

Status decision (evaluate in this order; collect *all* reason codes, and the final status is the most severe):

| Severity | Status | Triggered by (reason codes) | Action |
|---|---|---|---|
| 4 | NON_COMPLIANT | ABSOLUTE_LIMIT_BREACH, SINGLE_DURATION_BREACH, CUMULATIVE_DURATION_BREACH | QUARANTINE |
| 3 | INCONCLUSIVE | LOGGER_NOT_ASSIGNED, DATA_GAP, LOW_COVERAGE, CUSTODY_GAP, CALIBRATION_EXPIRED | REVIEW |
| 2 | COMPLIANT_WITH_WARNINGS | EXCURSION_WITHIN_ALLOWANCE, NEAR_LIMIT_UNCERTAIN, POLICY_MISMATCH_DECLARED, LONG_DWELL | RELEASE |
| 1 | COMPLIANT | (none) | RELEASE |

Also record `CARRIER_STATUS_CONTRADICTED` whenever the engine's result contradicts `carrier_reported_status` (e.g. the carrier said PASS but the data shows a gap).

Every ComplianceDecision must be traceable: applied_policy_id + reason codes + excursion report IDs → ExcursionReportReading → reading IDs.

---

## 7. Scenarios and anomalies

### 7.1 Natural scenarios (Phase 3, generated as valid data; weights in config)
NORMAL ~80%, MINOR_SPIKE_WITHIN_ALLOWANCE ~6%, BOUNDARY ~3% (e.g. exactly at max_c, or an excursion of exactly the allowed duration), EXCURSION_BREACH ~5%, LONG_DWELL ~4% (warming during a long stop), CALIBRATION_EXPIRED ~2% (device past its due date).

The temperature model: packaging setpoint + small Gaussian noise + slow drift + dwell-time warming (scaled by `thermal_response`) + deliberately shaped excursion windows for scenarios that need them. Every reading uses the device's systematic offset (within `uncertainty_c`).

The generator knows which scenario each shipment received, and writes the **expected outcome** (expected status + expected reason codes + expected excursion windows/durations) to `ground_truth/expected_outcomes.csv`, derived from the documented rules, not assigned at random.

### 7.2 Injected edge cases (Phase 5; ~1% of shipments each, on disjoint shipments, rates in config)

| issue_type | How it's injected | What the engine must do |
|---|---|---|
| `logger_not_assigned` | Readings for the shipment come from a device that isn't the one in its Logger assignment (or has no assignment for that window) | Flag LOGGER_NOT_ASSIGNED → INCONCLUSIVE |
| `product_threshold_mismatch` | The planner's `declared_policy_id` is set to the wrong temperature class (e.g. AMBIENT for a vaccine). The readings would "pass" under the wrong policy but fail or warn under the correct one | Resolve the correct policy from the product; flag POLICY_MISMATCH_DECLARED; decide using the correct policy |
| `missing_data_silently_passed` | Remove a block of readings longer than max_reading_gap_min, while `carrier_reported_status` stays PASS | Flag DATA_GAP (+ LOW_COVERAGE if applicable) and CARRIER_STATUS_CONTRADICTED → INCONCLUSIVE |
| `custody_gap_not_flagged` | Delete a middle HANDOFF event so the custody chain breaks (to_party ≠ next from_party) | Flag CUSTODY_GAP → INCONCLUSIVE |
| `excursion_across_unrelated_shipments` | Two shipments reuse the same device back to back: shipment A ends warm and shipment B starts warm. A naive "group by device" approach would merge them into one long excursion | Compute excursions per logger assignment window only; the durations must match the ground truth for each shipment separately |

Each injection writes one row to `ground_truth/anomalies.csv`: anomaly_id, issue_type, shipment_id(s), affected record IDs, description, expected_reason_codes, expected_status, and the expected excursion durations where relevant. Injection must update `expected_outcomes.csv` consistently.

**The ground truth must never be read by the compliance engine.** Enforce this with a test.

---

## 8. Configuration (`config/default.yaml`)

```yaml
seed: 20261022
scales:
  starter: { shipments: 50,   devices: 20 }
  full:    { shipments: 5000, devices: 1000 }
reading_interval_min: 10
date_window: { start: "2026-01-01", end: "2026-10-31" }
trip_duration_hours: { min: 4, max: 72 }
scenario_weights: { NORMAL: 0.80, MINOR_SPIKE_WITHIN_ALLOWANCE: 0.06, BOUNDARY: 0.03,
                    EXCURSION_BREACH: 0.05, LONG_DWELL: 0.04, CALIBRATION_EXPIRED: 0.02 }
injection_rates: { logger_not_assigned: 0.01, product_threshold_mismatch: 0.01,
                   missing_data_silently_passed: 0.01, custody_gap_not_flagged: 0.01,
                   excursion_across_unrelated_shipments: 0.01 }
long_dwell_threshold_min: 120
```

Every stage gets its own RNG stream from `np.random.SeedSequence(seed).spawn(...)`, keyed by a fixed stage name, so that changing one stage doesn't change the random numbers used by the others.

Determinism rules: sort every table by its PK before writing, use fixed float rounding, never use wall-clock time inside the data, and keep all timestamps in UTC.

---

## 9. CLI (built up gradually across phases)

```
coldchain generate --scale starter [--seed N]     # Phases 2–3
coldchain inject   --scale starter                 # Phase 5
coldchain check    --scale starter                 # Phase 4
coldchain validate --scale starter                 # Phase 6
coldchain evaluate --scale starter                 # Phase 6
coldchain export   --scale starter                 # Phase 7
coldchain reproduce --scale starter                # Phase 8
coldchain run-all  --scale starter                 # Phase 8 (whole pipeline)
```

Each command logs what it did, row counts and its output location, and exits non-zero on failure.

---

## 10. PHASES

### Phase 0 — Project setup and scaffolding
**Build:**
- Save this prompt to `docs/PROJECT_PLAN.md`. Create `docs/DECISIONS.md` (with the device/logger decision as entry #1) and a short `CLAUDE.md` summarising the rules in sections 0, 1 and 5.1.
- Create `pyproject.toml` (with dependencies + a `[dev]` extra, ruff config, pytest config, and a `coldchain` console script), `.gitignore`, a minimal `README.md`, `config/default.yaml`, and the `src/coldchain/` package containing `config.py`, `errors.py`, `ids.py`, `enums.py`, `rng.py` and a `cli.py` with a `version` command.
- Run `git init` if needed.
- Tests: config loads and validates; an invalid config raises a clear error; every ID function produces the exact formats in 5.1; the RNG streams are deterministic.

**UAT (user):**
```bash
cd /home/allencharles/Documents/YottaFlex
python3.12 -m venv .venv && source .venv/bin/activate
python --version   # must show 3.12.x
pip install -e ".[dev]"
coldchain version
pytest -v
ruff check .
```
Expected: the version prints, all tests pass, ruff reports no issues. Open `config/default.yaml` and check that the seed is 20261022.

---

### Phase 1 — Design: data dictionary, ER diagram, schemas, rules
**Build:**
- `docs/data_dictionary.md`: every table and every field (type, required, allowed values, unit, unique, validation rule), plus cardinalities, optional relationships, effective dates and lifecycle states.
- `docs/er_diagram.md`: a Mermaid `erDiagram` covering all tables and relationships.
- `docs/compliance_rules.md` (section 6) and `docs/anomaly_catalogue.md` (section 7.2).
- `config/reference/*.yaml` (policies including V2, product templates, packaging, Indian locations/hubs).
- `src/coldchain/schemas.py`: pandera schemas for every table, matching the data dictionary exactly.
- Tests: the schemas accept a valid hand-made sample row and reject bad enums, nulls in required fields and malformed IDs. Overlapping policy versions are rejected.

**UAT (user):**
- Open `docs/er_diagram.md` in VS Code's Markdown preview (`Ctrl+Shift+V`) and confirm the diagram renders and the relationships make sense.
- Read `docs/compliance_rules.md` and confirm you agree with the decision table.
- Run `pytest -v`. All tests should pass.

---

### Phase 2 — Master data generator
**Build:** `generate/master.py` and `generate/shipments.py`, plus `io.py`. Generate, in dependency order: Party → ThresholdPolicy → Product → Packaging → Lot → Device → Shipment → ShipmentLot → RouteStop → Logger. Respect every rule in 5.2 (one temperature class per shipment, lot quantities reconcile, no overlapping device assignments, compatible packaging, planned/actual times ordered). Write the results to `output/<scale>/raw/` as Parquet + CSV. `coldchain generate` runs the master stage for now.
**Tests:** every table passes its schema; all FKs resolve; PKs are unique; no device has overlapping assignments; lot quantities reconcile; the same seed produces identical tables.

**UAT (user):**
```bash
coldchain generate --scale starter
ls output/starter/raw/
```
Open `shipments.csv` and `loggers.csv`. Check that there are 50 shipments, that each has exactly one `LOG-SHP-2026-0000xx-01`, that the IDs look right, and that arrival times come after departure times. Run the command again and confirm the files are identical (`sha256sum output/starter/raw/*.csv`).

---

### Phase 3 — Event data: readings, custody, status history, natural scenarios
**Build:** `generate/scenarios.py`, `generate/readings.py` (vectorised), `generate/events.py`. Readings every 10 minutes across each assignment window, using the temperature model from 7.1. Custody events follow the route stops, with continuous party handoffs. Status events follow the lifecycle. Write `ground_truth/expected_outcomes.csv` (scenario → expected status/reasons/excursion windows). `coldchain generate` now runs master + events.
**Tests:** reading IDs match the format; readings fall within the assignment windows at exact 10-minute steps; there are no duplicate (logger_id, recorded_at) pairs; custody chains are continuous; status transitions are legal; every scenario type appears at starter scale (force at least one of each); NORMAL shipments never leave their range.

**UAT (user):**
```bash
coldchain generate --scale starter
python -c "import pandas as pd; r=pd.read_csv('output/starter/raw/temperature_readings.csv'); print(len(r)); print(r.head())"
```
Then ask Claude to write a one-off script, `scripts/plot_shipment.py SHP-2026-000001`, that saves a PNG of one shipment's readings with the threshold band drawn on it. Open the PNG and check that a NORMAL shipment stays inside the band and an EXCURSION_BREACH shipment visibly goes outside it.

---

### Phase 4 — Compliance engine
**Build:** the `compliance/` modules and `coldchain check`. Implement section 6 exactly: policy resolution (product + effective date), excursion detection per logger assignment, gaps/coverage, custody chain, logger/calibration checks, the status decision table, ExcursionReport + ExcursionReportReading + ComplianceDecision, and the appended UNDER_REVIEW → RELEASED/QUARANTINED status events. Write to `output/<scale>/compliance/`. The engine reads only `raw/`.
**Tests:** hand-built unit fixtures for each rule, including boundaries: an excursion of exactly 30 minutes under a 30-minute allowance is WITHIN_ALLOWANCE and 40 minutes is a breach; a reading exactly at max_c is not out of range; a gap of exactly 30 minutes is not a breach; V1 vs V2 policy selection around 2026-07-01. Add a test proving the engine never opens `ground_truth/`.

**UAT (user):**
```bash
coldchain generate --scale starter
coldchain check --scale starter
```
Open `compliance/compliance_decisions.csv` and pick one NON_COMPLIANT shipment. Follow its `excursion_report_ids` in `excursion_reports.csv`, then into `excursion_report_readings.csv`, then into the actual readings. Confirm that the temperatures really were out of range for the stated duration. Then compare the statuses with `ground_truth/expected_outcomes.csv`. They should match for the natural scenarios.

---

### Phase 5 — Edge-case injection and ground truth
**Build:** `inject/anomalies.py` and `coldchain inject`. Implement the 5 injectors from 7.2. Injection runs after generation, on disjoint shipments chosen by the seeded RNG (at least one of each type at starter scale). It writes `ground_truth/anomalies.csv` + `.json` and updates `expected_outcomes.csv`. It only modifies the records it targets. Write `docs/anomaly_catalogue.md` with one real example ID per type.
**Tests:** each injector changes only its target shipments (compare row hashes before and after for all other shipments); every anomaly row references existing IDs; no label column leaks into `raw/` tables; injection is deterministic.

**UAT (user):**
```bash
coldchain generate --scale starter
coldchain inject --scale starter
coldchain check --scale starter
```
Open `ground_truth/anomalies.csv`. For each of the 5 issue types, take its shipment_id and find it in `compliance/compliance_decisions.csv`. Confirm that the engine flagged the expected reason code. For `excursion_across_unrelated_shipments`, confirm that the two shipments have separate, correct excursion durations.

---

### Phase 6 — Validation suite and evaluation metrics
**Build:** `validation/checks.py` (`coldchain validate`), covering PK uniqueness, FK integrity (using DuckDB joins), enums, date ordering, arithmetic reconciliation (lot quantities, dwell_min, excursion durations recomputed from the readings), lifecycle transitions and duplicates. Output `validation/validation_summary.md` + `.json`, with PASS/FAIL per check and counts. Deliberate anomalies should show up as findings explained by the ground truth, not as unexplained failures. Also build `evaluation/metrics.py` (`coldchain evaluate`): precision/recall/F1 per issue type, a status confusion matrix against expected_outcomes, and an exact-match rate for excursion durations. Output `evaluation/evaluation_report.md` + `.json`.
**Tests:** validators catch deliberately broken toy tables; metric functions are correct on tiny hand-calculated examples.

**UAT (user):**
```bash
coldchain validate --scale starter
coldchain evaluate --scale starter
```
Open both reports. Expected: all integrity checks pass (or fail only where explained by injected anomalies), and recall is close to 100% for every issue type. If any type is below 100%, ask Claude to explain why before approving.

---

### Phase 7 — Full-scale run and exports
**Build:** `export/exporters.py` and `export/manifest.py` (`coldchain export`), producing CSV per table, JSON per table (JSON Lines for readings), `sql/schema.sql` (DDL with PKs/FKs) and `sql/seed.sql` (batched multi-row INSERTs, in dependency order). Verify that the SQL loads into a fresh DuckDB/SQLite database with all FKs satisfied. `manifest.json` holds the seed, scale, command, package version, and the row count + sha256 per file. Profile and optimise the full run.
**Tests:** a starter-scale export round-trips (load the SQL and compare row counts with the CSVs).

**UAT (user):**
```bash
time (coldchain generate --scale full && coldchain inject --scale full && coldchain check --scale full && coldchain validate --scale full && coldchain evaluate --scale full && coldchain export --scale full)
cat output/full/export/manifest.json
```
Expected: it finishes in a few minutes, with 5,000 shipments and 1,000 devices in the manifest and a large readings count. The validation and evaluation reports look healthy at full scale.

---

### Phase 8 — Reproducibility, `run-all` and documentation
**Build:** `export/reproducibility.py` (`coldchain reproduce`). This runs the full pipeline twice with the same seed into two temporary directories, compares the row counts and sha256 for every file, and writes `reproducibility_check.md` (a table of file / rows run1 / rows run2 / checksum match). Add `coldchain run-all`. Finalise the `README.md`: what the project is, architecture diagram (Mermaid), quick start, CLI reference, outputs explained, how to trace a decision, design decisions (link to DECISIONS.md), assumptions and limitations, and data provenance ("100% synthetic; no external data used").
**Tests:** a reproduce test at starter scale.

**UAT (user):**
```bash
coldchain reproduce --scale starter
coldchain run-all --scale starter
```
Expected: every checksum matches. Then follow the README from scratch in a fresh clone (`git clone . /tmp/yf-test && cd /tmp/yf-test`) and confirm it works by following the instructions alone.

---

### Phase 9 (optional) — Streamlit viewer (read-only, no login)
**Build:** `app/streamlit_app.py` (multi-page), which reads only the existing output files:
1. **Overview**: KPI tiles (shipments, % compliant, non-compliant, inconclusive, excursions) and a status breakdown chart.
2. **Shipment Explorer**: search a shipment and see its lots/products, packaging, device and calibration, route stops, custody timeline and status history.
3. **Temperature Chart**: a plotly line chart of the readings with a green policy band, absolute limits, shaded excursion windows and gaps marked.
4. **Excursion Reports**: a filterable table, where each report links to its readings and policy.
5. **Custody & Data Gaps**: shipments with custody or logger gaps.
6. **Data Quality**: the validation summary, evaluation metrics and ground truth vs detected results.
Add a scale selector in the sidebar. No authentication.

**UAT (user):**
```bash
pip install -e ".[dashboard]"
streamlit run app/streamlit_app.py
```
Click through every page. Pick a NON_COMPLIANT shipment and check that the chart shows the red excursion and that the excursion report matches it.

---

## 11. Definition of done (whole project)
- [ ] All the brief's entities exist, with PKs/FKs, documented cardinalities, effective dates and lifecycle states
- [ ] Baseline volume: 5,000 shipments, 1,000 devices, 10-minute readings
- [ ] All 5 edge cases are injected, labelled and detected
- [ ] Ground truth is separate and never read by the engine
- [ ] Every compliance decision traces to its readings and policy
- [ ] Validation summary, evaluation report and manifest are generated automatically
- [ ] Same seed → identical checksums (proven)
- [ ] README, data dictionary, ER diagram, decisions log and generator command are documented
- [ ] ruff is clean and pytest is green

---

**Start now with Phase 0 only.** First give me your plan for Phase 0, then implement it, run the checks, show the Phase Summary and UAT steps, end with the Phase Status Report (section 0.1), and wait for `Phase 0 approved`.
