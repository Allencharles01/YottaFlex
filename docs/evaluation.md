# Validation and Evaluation

Phase 6 adds two commands:

- `coldchain validate` judges the data on its own. It never reads the answer key.
- `coldchain evaluate` scores the compliance engine against the answer key and explains every
  validation finding with an injected anomaly.

This document defines precisely what each check, each metric and each verdict means. The code
is in `src/coldchain/validation/` and `src/coldchain/evaluation/`. Related decisions are
DECISIONS.md #84–#92.

## 1. Validation (`coldchain validate`)

### 1.1 Inputs, independence and outputs

| `--source` | Reads | Writes |
|---|---|---|
| `raw` (default) | `raw/*.parquet` (13 tables), plus the 4 `compliance/*.parquet` tables when all are present | `validation/validation_summary.{md,json}` |
| `baseline` | `baseline/raw/*.parquet` (the clean pre-injection snapshot); it has no engine output, so the engine-output checks are SKIPPED | `validation/baseline/validation_summary.{md,json}` |

- **Never the ground truth.** No validation module names a ground-truth table or layer. None
  imports the engine, the generator, the injector or the evaluation. `validate` runs with
  `ground_truth/` deleted, and an audit hook records every file it opens
  (`tests/test_validation_isolation.py`).
- **Independent recomputation.** The suite does not reuse the engine. Assignment windows,
  trusted readings, excursion runs, coverage, gaps and applied policies are recomputed in
  `validation/context.py` from the raw tables and `compliance_rules.md`. The only shared code is
  the effective-date primitive `schemas.effective_policy_ids` (DECISIONS.md #35). When the
  engine's output agrees with the recomputed values, that agreement is evidence.
- Tables are read with `check_schema=False`, so a schema violation becomes a finding instead of
  a crash.
- The JSON summary lists the sha256 of every input file (`inputs`). `evaluate` uses these
  checksums to refuse a stale summary (§4).

### 1.2 Checks, findings and tiers

Each check returns PASS, FAIL, ERROR (the check could not run on the data) or SKIPPED (a table
it needs is not in the input). A **finding** is one failing record:

| Field | Meaning |
|---|---|
| `check_id` | e.g. `fk.loggers.device_id`, `rule.custody_events.continuous_chain` |
| `tier` | `INTEGRITY` or `BUSINESS_RULE` |
| `table` | the table the check judges |
| `record_id` | the failing row's primary key (composite keys joined with `/`). A shipment-level problem, such as "no custody events", uses the shipment ID. |
| `shipment_id` | the shipment the record belongs to (null for master data such as parties) |
| `detail` | what is wrong, e.g. `3 of 432 readings missing, largest gap 40 min` |

**Two tiers** (DECISIONS.md #84):

- **INTEGRITY** checks must always pass, on clean and on injected data alike. Any FAIL or
  ERROR makes `validate` exit with status 1.
- **BUSINESS_RULE** checks cover the rules that an injected edge case breaks on purpose. Their
  findings are reported (the check shows FAIL in the summary), but `validate` still exits 0.
  `evaluate` must then explain every one of them (§3).

On the clean baseline both tiers find **nothing**. This holds for the CLI on
`baseline/raw` and also for all 133 checks when the engine runs on the baseline in memory
(`tests/test_evaluation.py`).

### 1.3 Check catalogue

Generic checks, one per table or relationship:

| Check IDs | Category | Rule |
|---|---|---|
| `schema.<table>` | SCHEMA | The table passes its pandera schema (types, ID formats, row rules). |
| `pk.<table>` | PRIMARY_KEY | The primary key is non-null and unique (one batched DuckDB `GROUP BY` query). |
| `enum.<table>` | ENUM | Every categorical column, including `\|`-lists of reason codes, holds allowed values. The allowed values are read from the schema. |
| `fk.<child>.<column>` | FOREIGN_KEY | Every value exists in the parent table: each entry of `schemas.FOREIGN_KEYS` between raw/compliance tables, checked with one batched DuckDB anti-join (`LEFT JOIN … WHERE parent IS NULL`). |
| `dup.<table>.<columns>` | DUPLICATE | Natural keys besides the PK are unique: party, product and packaging names, device serials, `(policy_code, version)`, one logger per shipment, `(logger_id, recorded_at)`, `(shipment_id, sequence)` of stops, custody and both status histories, `(logger_id, started_at)` of reports, and one report per reading. |

Specific integrity checks:

| Check ID | Rule |
|---|---|
| `fk.products.supplier_role`, `fk.shipments.party_roles` | Each party has the role its column implies (SUPPLIER, CARRIER, RECEIVER, PLANNER). |
| `fk.route_stops.party_roles` | ORIGIN is the supplier, DESTINATION is the receiver, and hubs are WAREHOUSE parties. |
| `fk.custody_events.party_roles` | PICKUP goes supplier → carrier at ORIGIN, DELIVERY goes carrier → receiver at DESTINATION, and a hub HANDOFF goes carrier ↔ hub party. |
| `fk.shipment_status_events.actor_roles` | PLANNED → planner, PICKED_UP/IN_TRANSIT → carrier, AT_STOP → that stop's party, DELIVERED → receiver. |
| `fk.compliance_status_events.actor_roles` | UNDER_REVIEW → a LOGISTICS_ANALYST; RELEASED/QUARANTINED → an OPERATIONS_MANAGER. |
| `fk.excursion_reports.decision_policy` | A report uses its decision's applied policy and its shipment's logger. |
| `dup.compliance_status_events.raw_sequence` | No engine event reuses a `(shipment, sequence)` from the raw history. |
| `dates.shipments.trip` | Planned departure is inside the date window. Arrivals come after departures. The planned trip is within `trip_duration_hours`. |
| `dates.shipment_lots.lot_dates` | A lot is made before the departure date and expires after the arrival date (stricter of planned/actual). |
| `dates.parties.active_from` | No party is used before its `active_from`. |
| `dates.loggers.window` | `assigned_at ≤ actual_departure_at`, `unassigned_at ≥ actual_arrival_at`, and both ends lie on the reading grid. |
| `dates.temperature_readings.window_grid` | Every reading is inside its logger window, on the grid. |
| `dates.route_stops.chronological` | Stops are chronological. ORIGIN departs and DESTINATION arrives at the actual times. |
| `dates.custody_events.chronological` | Custody times strictly increase, and each equals its stop's arrival or departure time. |
| `dates.shipment_status_events.chronological` | Status times are non-decreasing and match the trip and stops. PLANNED is within `planning_lead_hours`. |
| `dates.compliance_status_events.timing` | Engine events occur at the window end and never before DELIVERED. |
| `lifecycle.shipment_status_events.transitions` | The raw history is numbered 1..n and follows PLANNED → PICKED_UP → IN_TRANSIT ⇄ AT_STOP → DELIVERED, with one AT_STOP per hub. |
| `lifecycle.compliance_status_events.transitions` | The engine history continues the raw numbering. RELEASE → UNDER_REVIEW, RELEASED. QUARANTINE → UNDER_REVIEW, QUARANTINED. REVIEW → UNDER_REVIEW. |
| `recon.lots.quantity` | Σ shipped units per lot ≤ `quantity_units`. |
| `recon.lots.shelf_life` | `expires_on − manufactured_on = shelf_life_days`. |
| `recon.route_stops.dwell_min` | `dwell_min = departed_at − arrived_at`. |
| `recon.excursion_reports.from_readings` | Every report equals an excursion recomputed from the trusted readings: direction, end, `duration_min = (end − start) + i`, peak, reading count, first and last reading IDs, breach type and policy. No recomputed excursion is missing a report. |
| `recon.excursion_report_readings.links` | Each report links exactly its run's readings, and every out-of-range trusted reading is linked. |
| `recon.compliance_decisions.coverage` | `expected_reading_count`, `reading_count`, `coverage_pct` and `max_gap_min` recomputed from the readings (compliance_rules.md §1.3–1.4). |
| `recon.compliance_decisions.excursion_totals` | `excursion_report_ids` are the shipment's reports, and `total_excursion_min` is their duration sum. |
| `recon.compliance_decisions.evidence_codes` | Reason codes that follow from numbers agree with them: DATA_GAP, LOW_COVERAGE, the three breach codes, EXCURSION_WITHIN_ALLOWANCE, POLICY_MISMATCH_DECLARED, LONG_DWELL and CARRIER_STATUS_CONTRADICTED. |
| `consistency.parties.cities` | Cities are reference cities. Warehouses are in hub cities and suppliers in manufacturing-cluster cities. |
| `consistency.devices.reading_interval` | Every device records at the configured interval. |
| `consistency.shipments.lots` | Every shipment has lots of one policy code and one supplier, and that supplier is the shipment's supplier. |
| `consistency.shipments.packaging` | The packaging supports the class and holds temperature for the planned trip. |
| `consistency.shipment_lots.released` | Only RELEASED lots are shipped. |
| `consistency.route_stops.structure` | Stops are numbered 1..n with n ≥ 2: ORIGIN first, DESTINATION last, hubs in between. |
| `consistency.shipments.one_decision` | Every shipment has a compliance decision. |
| `consistency.compliance_decisions.applied_policy` | The applied policy is the products' version at actual departure, and `policy_source` matches the declaration. |

Business-rule checks:

| Check ID | Rule | Broken on purpose by |
|---|---|---|
| `rule.temperature_readings.assigned_device` | A reading carries its logger assignment's device. | `logger_not_assigned` |
| `rule.loggers.complete_stream` | Each logger window has a reading at every grid instant, from any device. | `missing_data_silently_passed` |
| `rule.custody_events.continuous_chain` | No sequence hole, `from_party = previous to_party`, PICKUP first and DELIVERY last. The finding sits on the event where the chain breaks. | `custody_gap_not_flagged` |
| `rule.route_stops.custody_coverage` | ORIGIN has 1 PICKUP, DESTINATION has 1 DELIVERY, and each hub has 2 HANDOFFs. | `custody_gap_not_flagged` |
| `rule.shipments.declared_policy` | The planner declared the products' version effective at **planned** departure. The natural V1→V2 straddle (DECISIONS.md #26) is therefore not a finding. | `product_threshold_mismatch` |
| `rule.compliance_decisions.carrier_claim` | The carrier's claim agrees with the decided action (PASS ⇔ RELEASE). | every injected type whose outcome is not RELEASE |
| `rule.shipments.has_logger` | Every shipment has a logger assignment. | (none today) |
| `rule.loggers.no_device_overlap` | One device's windows never overlap (DECISIONS.md #1). | (none today) |

`excursion_across_unrelated_shipments` breaks no data rule: it is a trap for the engine and is
evaluated by the metrics in §2.

## 2. Evaluation metrics (`coldchain evaluate`)

`evaluate` reads:
- `ground_truth/expected_outcomes` and `anomalies` (an empty `anomalies` table if nothing was
  injected);
- `compliance/compliance_decisions` and `excursion_reports`;
- `raw/shipments`, `loggers` and `threshold_policies`;
- `validation/validation_summary.json`.

It writes `evaluation/evaluation_report.{md,json}`. A ratio whose denominator is 0 is
**undefined** (`null` / `n/a`): it is never shown as 0 % or 100 %, and it never fails the run.
Ratios are rounded to 4 decimals.

For counts *TP*, *FP* and *FN*:
`precision = TP / (TP + FP)`, `recall = TP / (TP + FN)`, `F1 = 2·TP / (2·TP + FP + FN)`.

### 2.1 Detection per issue type

**Actual positives** of type *T* are the shipments with an `anomalies` row of type *T*. A pair
anomaly contributes both of its shipments.

**Predicted positives** of type *T* are the shipments where *T*'s **signature** fires. A
signature is computed only from the engine's output and the raw tables, never from the answer
key:

| Issue type | Signature |
|---|---|
| `logger_not_assigned` | LOGGER_NOT_ASSIGNED |
| `product_threshold_mismatch` | POLICY_MISMATCH_DECLARED **and** the declared policy's `policy_code` ≠ the applied one's. A version-only mismatch is the natural planning case of DECISIONS.md #26. |
| `missing_data_silently_passed` | DATA_GAP **and** CARRIER_STATUS_CONTRADICTED, **without** LOGGER_NOT_ASSIGNED. The untrusted stream of a wrong-device shipment also leaves a gap. |
| `custody_gap_not_flagged` | CUSTODY_GAP **and** CARRIER_STATUS_CONTRADICTED |
| `excursion_across_unrelated_shipments` | Both shipments of a pair (A, B) where all of these hold: B is the device's next assignment after A; A has an excursion ending at its `unassigned_at` and B has one starting at its `assigned_at`; both run in the same direction; and **both are WITHIN_ALLOWANCE**. A device-level merge of such a pair would invent a breach. Two edge excursions that already breach on their own change no conclusion when merged, so they are not the trap (DECISIONS.md #88). |

A **detection** (true positive) is a shipment that is both an actual and a predicted positive of
*T*, **and** whose engine outcome is exactly the anomaly row's expected outcome:
- `status = expected_status`;
- the set of reason codes equals `expected_reason_codes`;
- the sorted list of its excursion durations equals `expected_excursion_durations_min`.

So *TP* counts the correct detections, *FP* = predicted − *TP*, and *FN* = actual − *TP*. A
shipment that is flagged but has the wrong outcome counts against both precision and recall.

### 2.2 Detection per reason code

This is computed over all shipments. For each code *c*, a shipment is an actual positive when
`expected_outcomes.expected_reason_codes` lists *c*, and a predicted positive when the engine's
`reason_codes` does. Every agreement is a true positive.

### 2.3 Status confusion matrix

Rows are the `expected_status` and columns the engine's `status`, for every shipment in
`expected_outcomes`. A shipment without a decision is counted in a `NO_DECISION` column.
**Status accuracy** = diagonal / shipments.

### 2.4 Excursion duration exact-match rate

The expected excursions come from `expected_excursion_windows` and
`expected_excursion_durations_min`. The engine's come from `excursion_reports`.

- An excursion is identified by (`shipment_id`, `started_at`, `ended_at`).
- A pair is a **window match** when all three are equal, and an **exact match** when
  `duration_min` is equal too.

```
exact_match_rate = exact matches / (expected + engine − window matches)
```

The denominator is the number of distinct excursions on either side, so a missed excursion, a
spurious report and a wrong duration all lower the rate. The report also lists the counts:
missed, spurious, duration mismatches.

### 2.5 Accuracy per scenario

Every shipment belongs to one **case**: `injected:<issue_type>` for an injected shipment, and
otherwise its natural `scenario`. A shipment is **fully correct** when all of these match:
- status;
- action;
- reason-code set;
- every excursion (start, end, duration).

**Accuracy** = fully correct / shipments in the case. The status accuracy per case is reported
too.

## 3. Validation findings vs anomalies

A finding is **explained** when its `shipment_id` is a target of an anomaly **and** its check is
listed for that anomaly's issue type:

| Issue type | Checks it is expected to break |
|---|---|
| `logger_not_assigned` | `rule.temperature_readings.assigned_device`, `rule.compliance_decisions.carrier_claim` |
| `product_threshold_mismatch` | `rule.shipments.declared_policy`, `rule.compliance_decisions.carrier_claim` |
| `missing_data_silently_passed` | `rule.loggers.complete_stream`, `rule.compliance_decisions.carrier_claim` |
| `custody_gap_not_flagged` | `rule.custody_events.continuous_chain`, `rule.route_stops.custody_coverage`, `rule.compliance_decisions.carrier_claim` |
| `excursion_across_unrelated_shipments` | none (a trap for the engine, not a data defect) |

An INTEGRITY finding is therefore **never** explained. A business-rule finding on a natural
shipment, or on an anomaly target but from a check its type does not list, is unexplained too.
The report prints **"Unexplained validation findings: N"** and lists each one.

**Anomalies surfaced by validation** = anomaly rows for which every listed check produced a
finding on their shipment, divided by the rows whose type lists at least one check. This is the
converse property: every injected defect really shows up in the integrity data.

## 4. Verdicts and exit codes

| Command | Exits with 1 when |
|---|---|
| `validate` | any INTEGRITY check is FAIL or ERROR |
| `evaluate` | at **every** scale: any unexplained validation finding, or any validation check that errored |
| `evaluate` | additionally, at a **strict** scale (`evaluation.strict_scales`, default `[starter, full]`, DECISIONS #95): any **defined** metric below 100 %. This covers precision/recall/F1 per issue type and per reason code, status accuracy, the excursion exact-match rate, accuracy per case and the surfaced rate. |

`evaluate` also refuses to run (exit 1) in three cases:
- `validation/validation_summary.json` is missing;
- the summary is not of the raw data at this scale;
- any of its input files has changed since it was written (sha256 mismatch: "stale").

The pipeline order is therefore `generate → inject → check → validate → evaluate`.

## 5. Evaluation history

- **First full-scale run (in memory):** `excursion_across_unrelated_shipments` scored
  **96.15 % precision**. Every other metric was already 100 %.
- **Cause:** two `product_threshold_mismatch` shipments, SHP-2026-002244 → SHP-2026-002593, ran
  back to back on device DEV-000769. Each was a single whole-trip breach. They met at the
  hand-over, so the first version of the pair signature, "excursions on consecutive
  assignments of one device that meet at the hand-over", fired on them. That signature was
  broader than the trap it is meant to detect.
- **Fix:** the trap, as documented in the anomaly catalogue, is two excursions that are each
  **within allowance** but would look like one breach if they were merged per device. Merging
  two breaches changes no conclusion, so it is not the trap. The signature now requires
  `WITHIN_ALLOWANCE` on both sides (DECISIONS #88). This corrects the signature to match the
  documented definition of the trap; it was not tuned to the data.
- **After the fix:** every metric is 100 % at both starter and full scale, and starter results
  did not change.

## 6. Results (default seed)

| Scale | Validation | Evaluation |
|---|---|---|
| starter, baseline (CLI, `--source baseline`) | 97 PASS, 0 FAIL, 36 SKIPPED (engine output), **0 findings** | — |
| starter, baseline + engine (in memory) | 133 PASS, **0 findings** | — |
| starter, injected (CLI) | 0 integrity findings; 84 business-rule findings on 4 shipments | PASS: every metric 100 %, 84/84 findings explained, **unexplained 0**, 4/4 anomalies surfaced, 50/50 shipments fully correct |
| full, baseline + engine (in memory) | 133 PASS, **0 findings** (1,163,109 readings) | — |
| full, injected (in memory) | 0 integrity findings; 13,160 business-rule findings on 200 shipments | PASS: 5,000/5,000 shipments fully correct, every metric 100 %, 13,160/13,160 findings explained, 200/200 anomalies surfaced |
| full, injected (CLI, on disk, Phase 7) | 0 integrity findings; 13,160 business-rule findings on 200 shipments (1,162,819 readings) | PASS (strict, DECISIONS #95): 5,000/5,000 shipments fully correct, every metric 100 %, 13,160/13,160 findings explained, unexplained 0, 200/200 anomalies surfaced; P/R = 1.0 for all 5 issue types |
