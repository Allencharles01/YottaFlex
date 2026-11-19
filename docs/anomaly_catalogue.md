# Anomaly Catalogue

The five deliberately injected edge cases (PROJECT_PLAN.md §7.2), and what the compliance engine
must conclude for each. Phase 1 wrote this as the design; Phase 5 implemented the injectors
(`src/coldchain/inject/`), updated each entry to the design as built, and added one real example
per type. The examples come from the starter-scale output with the default seed:
`coldchain generate --scale starter && coldchain inject --scale starter`.

## How injection works

- Injection runs **after** generation (`coldchain inject`) on valid baseline data. The first run
  copies the clean generator output byte for byte to `output/<scale>/baseline/{raw,ground_truth}/`.
  Every run starts from that snapshot, so re-running gives the same result, and a diff of
  `raw/` against `baseline/raw/` shows exactly which records changed (DECISIONS.md #67).
- Targets are drawn from shipments whose natural scenario is **NORMAL** and whose expected status
  is **COMPLIANT** (DECISIONS.md #56). That way the expected outcome is caused by the injection
  alone. Different issue types use **disjoint** shipments, picked by the seeded `inject.targets`
  stream. Each rate in `config/default.yaml` is a share of shipments (about 1 % each, so 25
  two-shipment pairs at full scale), with at least one anomaly of each type at starter scale
  (DECISIONS.md #68).
- An injector modifies **only** the records of its target shipments. Every other row stays
  identical (row-hash test), and surviving IDs are never renumbered.
- Injected data is always **well formed** (BACKLOG #2, DECISIONS.md #78). Every ID, FK and schema
  rule holds, so the engine never stops with a `ComplianceError`; it flags a broken *business*
  rule instead.
- `ground_truth/anomalies.{parquet,csv,json}` gets **one row per affected shipment**, so the
  two-shipment case has two rows sharing one `anomaly_id` (DECISIONS.md #8). The injected
  shipments' rows in `ground_truth/expected_outcomes` (`issue_type`, expected status, reasons
  and excursions) are re-derived from the rules in `compliance_rules.md`, using the facts the
  injector built in, never by running the engine (DECISIONS.md #70).
- `affected_record_ids` lists existing records the anomaly involves. A logger ID stands for all of
  its readings. `removed_record_ids` lists deleted records (DECISIONS.md #79).
- Labels live only in `ground_truth/`. Raw tables never gain a label column (enforced by the
  strict schemas), and the engine never reads `ground_truth/` or `baseline/` (enforced by tests).
- Injection never changes `carrier_reported_status`. Every target was PASS (baseline PASS ⇔
  RELEASE) and stays PASS, so CARRIER_STATUS_CONTRADICTED appears exactly when the injected
  outcome is not RELEASE (DECISIONS.md #71).

## Summary

| issue_type | Records changed | Engine must flag | Expected status | Example (starter) |
|---|---|---|---|---|
| `logger_not_assigned` | `temperature_readings.device_id` of every reading of the target logger | LOGGER_NOT_ASSIGNED, DATA_GAP, LOW_COVERAGE, CARRIER_STATUS_CONTRADICTED | INCONCLUSIVE | ANOM-000001 · SHP-2026-000019 |
| `product_threshold_mismatch` | `shipments.declared_policy_id`, plus every reading moved into the declared class | SINGLE_DURATION_BREACH, CUMULATIVE_DURATION_BREACH, POLICY_MISMATCH_DECLARED, CARRIER_STATUS_CONTRADICTED | NON_COMPLIANT | ANOM-000002 · SHP-2026-000028 |
| `missing_data_silently_passed` | one block of `temperature_readings` deleted; `carrier_reported_status` stays PASS | DATA_GAP (+ LOW_COVERAGE if coverage < 95 %), CARRIER_STATUS_CONTRADICTED | INCONCLUSIVE | ANOM-000003 · SHP-2026-000003 |
| `custody_gap_not_flagged` | one middle HANDOFF `custody_events` row deleted | CUSTODY_GAP, CARRIER_STATUS_CONTRADICTED | INCONCLUSIVE | ANOM-000004 · SHP-2026-000036 |
| `excursion_across_unrelated_shipments` | two back-to-back shipments of one device: shaped readings at the end of A and the start of B | EXCURSION_WITHIN_ALLOWANCE on each, with separate durations | COMPLIANT_WITH_WARNINGS (each) | ANOM-000005 · SHP-2026-000024 + SHP-2026-000026 |

## 1. `logger_not_assigned`

- **Injection:** for one target shipment, every reading's `device_id` becomes a different device,
  drawn at random among the devices that have **no** assignment overlapping the logger window.
  The `logger_id` (and so every reading ID) and the temperatures stay the same, so the readings
  still claim to belong to the shipment (DECISIONS.md #72).
- **Why it matters:** the readings cannot be attributed to the device that was assigned and
  calibrated for this shipment.
- **Engine:** LOGGER_NOT_ASSIGNED. The readings are untrusted and excluded (compliance_rules.md
  §1.2), so trusted coverage collapses to 0 %: DATA_GAP (gap = window + 2 *i*) and LOW_COVERAGE
  follow. The carrier reported PASS, so CARRIER_STATUS_CONTRADICTED is added too. No excursion
  reports.
- **Expected:** INCONCLUSIVE → REVIEW; `expected_excursion_count = 0`.
- **Ground truth:** `affected_record_ids` = the logger ID (all of its readings) and the device ID
  used.
- **Example:** `ANOM-000001`, shipment `SHP-2026-000019`. All 76 readings of
  `LOG-SHP-2026-000019-01` carry `DEV-000010` instead of the assigned `DEV-000017`. Engine:
  coverage 0.00 %, max gap 770 min, INCONCLUSIVE with
  `LOGGER_NOT_ASSIGNED|DATA_GAP|LOW_COVERAGE|CARRIER_STATUS_CONTRADICTED`.

## 2. `product_threshold_mismatch`

- **Injection:** the planner's `declared_policy_id` becomes a *different temperature class*: that
  class's version effective at `planned_departure_at`. The **whole trip** is then held inside
  that class. Every reading is moved with `clip(t − setpoint + centre, low, high)` into the band
  that is both inside the declared policy's safe band `[min + u + m, max − u − m]` and inside one
  of the product policy's excursion bands, `[max + u + m, abs_max − m]` or
  `[abs_min + m, min − u − m]` (*u* = device uncertainty, *m* = `limit_margin_c`). The stream
  keeps its natural shape. FROZEN products have no other class to overlap with, so they are
  never targeted (DECISIONS.md #73).
- **Why it matters:** a checker that trusted the declaration would see a complete stream inside
  its limits, with no reading even near them, and would **release** a product that spent the
  whole trip outside its own range. (A test relabels the products with the declared class and
  runs the real engine: it releases the shipment.)
- **Change from the Phase 1 design:** a reshaped *block* alone cannot pass under the declared
  class, because the rest of the trip would be out of range there. So the whole trip is moved.
- **Engine:** resolves the correct policy from the product and the departure date, adds
  POLICY_MISMATCH_DECLARED (`policy_source = PRODUCT_OVERRIDE`), and decides under the correct
  policy. The whole window is one excursion (`duration = window + i`): SINGLE_DURATION_BREACH
  and CUMULATIVE_DURATION_BREACH, never ABSOLUTE_LIMIT_BREACH or NEAR_LIMIT_UNCERTAIN.
  CARRIER_STATUS_CONTRADICTED is added because the carrier reported PASS.
- **Expected:** NON_COMPLIANT → QUARANTINE; one excursion whose duration comes from the correct
  policy's rules.
- **Ground truth:** `affected_record_ids` = shipment ID, new declared policy ID, and the logger ID
  (all of its readings were reshaped).
- **Example:** `ANOM-000002`, shipment `SHP-2026-000028`. The product class is AMBIENT_15_25
  (applied `THR-AMBIENT_15_25-V1`), but the planner declared `THR-COOL_8_15-V1`. The 84 readings
  lie at 12.16–12.90 °C: inside 8–15 °C, below 15 °C. Engine: excursion report
  `EXCR-2026-000006` (LOW, 840 min, SINGLE_DURATION_BREACH), NON_COMPLIANT with
  `SINGLE_DURATION_BREACH|CUMULATIVE_DURATION_BREACH|POLICY_MISMATCH_DECLARED|CARRIER_STATUS_CONTRADICTED`.

## 3. `missing_data_silently_passed`

- **Injection:** delete one contiguous block of
  `floor(max_reading_gap_min / i) + U{injection.missing_extra_readings}` readings (3–9 at the
  defaults, so the gap is ≥ 40 min), placed strictly inside the window, and leave
  `carrier_reported_status = PASS` (an intended exception to DECISIONS.md #20/#27;
  DECISIONS.md #74).
- **Why it matters:** "no out-of-range readings" is not proof of compliance when data is
  missing.
- **Engine:** DATA_GAP (gap = (*m* + 1) × *i*); LOW_COVERAGE if the deletion pushes coverage below
  95 % (6 of 50 at full scale); CARRIER_STATUS_CONTRADICTED (PASS vs REVIEW).
- **Expected:** INCONCLUSIVE → REVIEW.
- **Ground truth:** `removed_record_ids` = the deleted reading IDs; `affected_record_ids` = the
  readings just before and after the gap.
- **Example:** `ANOM-000003`, shipment `SHP-2026-000003`. Readings
  `TEMP-LOG-SHP-2026-000003-01-20260122T1100`, `…T1110` and `…T1120` were deleted, between
  `…T1050` and `…T1130`. Engine: max gap 40 min, coverage 99.31 %, INCONCLUSIVE with
  `DATA_GAP|CARRIER_STATUS_CONTRADICTED`.

## 4. `custody_gap_not_flagged`

- **Injection:** on a shipment with at least one intermediate stop, delete one HANDOFF custody
  event chosen at random. The chain breaks: the previous event's `to_party_id` no longer equals
  the next event's `from_party_id`, and the sequence numbers skip one (survivors are never
  renumbered; DECISIONS.md #75).
- **Why it matters:** an unaccounted custody interval means nobody can vouch for the goods
  during it.
- **Engine:** CUSTODY_GAP; CARRIER_STATUS_CONTRADICTED (PASS vs REVIEW).
- **Expected:** INCONCLUSIVE → REVIEW.
- **Ground truth:** `removed_record_ids` = the deleted custody event ID; `affected_record_ids` =
  the events on either side of it.
- **Example:** `ANOM-000004`, shipment `SHP-2026-000036`. `CUS-SHP-2026-000036-05` (HANDOFF
  `PTY-0012` → `PTY-0008`) was deleted, so sequence 04 is followed by 06. Engine: INCONCLUSIVE
  with `CUSTODY_GAP|CARRIER_STATUS_CONTRADICTED`.

## 5. `excursion_across_unrelated_shipments`

- **Injection:** pick two shipments A and B that are **consecutive assignments of the same
  device**: B is that device's very next assignment, so the windows never overlap. Among the
  eligible pairs, the one with the **shortest idle time** between A's `unassigned_at` and B's
  `assigned_at` is taken first (the device goes straight from one trip to the next), ties
  broken by the seeded rank. No shipment is in two pairs (DECISIONS.md #83). With *n* = ⌊`max_single_excursion_min` / *i*⌋ for each shipment's applied
  policy, A's last *k*_A and B's first *k*_B readings are shaped as HIGH excursions (the
  Phase 3 hump and ramp), with 1 ≤ *k* ≤ *n* for each and *k*_A + *k*_B > the larger *n*
  (DECISIONS.md #76).
- **Why it matters:** a naive engine that groups readings by *device* sees one run spanning both
  shipments. That run is longer than the allowance, so it reports a false breach and an inflated
  duration.
- **Engine:** computes excursions per logger assignment window only (compliance_rules.md §1.5).
  A gets an excursion ending at its last reading, and B gets one starting at its first reading.
  Each is within its own allowance.
- **Expected:** EXCURSION_WITHIN_ALLOWANCE → COMPLIANT_WITH_WARNINGS for each. The carrier claim
  (PASS) agrees with RELEASE, so there is no contradiction. The two `anomalies` rows share one
  `anomaly_id`, and each carries its own `expected_excursion_durations_min`.
- **Ground truth:** `affected_record_ids` = the shared device ID and that shipment's reshaped
  reading IDs (excursion and ramp). `idle_min` = the device's idle time between the two
  assignments, the same on both rows; it is null for every other issue type. Starter: 3,750 min
  (the shortest eligible pair, since 20 devices spread over ten months idle for days); full
  scale (25 pairs, in memory): min 60, median 270, max 440 min.
- **Example:** `ANOM-000005`, device `DEV-000017`, A = `SHP-2026-000024` (COOL_8_15, max 15 °C,
  20-min allowance), then B = `SHP-2026-000026` (REFRIG_2_8 V1, max 8 °C, 30-min allowance),
  `idle_min` = 3,750. A's last reading (2026-04-28T09:00Z, 19.05 °C) and B's first three
  (2026-04-30T23:30Z–23:50Z, up to 9.88 °C) are above their `max_c`. Engine:
  `EXCR-2026-000004` = 10 min on A and `EXCR-2026-000005` = 30 min on B, both
  WITHIN_ALLOWANCE. A device-level merge would see one 4-reading (40 min) run, beyond either
  allowance.
