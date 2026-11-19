# Anomaly Catalogue

The five deliberately injected edge cases (PROJECT_PLAN.md §7.2), and what the compliance engine
must conclude for each. This document is the **design** (Phase 1). Phase 5 implements the
injectors and adds one real example ID per type, taken from the starter-scale output.

## How injection works

- Injection runs **after** generation (`coldchain inject`) on valid baseline data.
- Targets are picked by the seeded `inject` RNG stream. Different issue types use **disjoint**
  shipments, at about 1 % of shipments each (`injection_rates` in `config/default.yaml`), with at
  least one of each type at starter scale.
- Targets are drawn from shipments whose natural scenario is **NORMAL**. That way the expected
  outcome is caused by the injection alone, and the expected codes are easy to state.
- An injector modifies **only** the records of its target shipments. Every other row stays
  byte-identical, and a Phase 5 test compares row hashes to prove it.
- Each injection appends rows to `ground_truth/anomalies.csv` (+ `.json`): **one row per affected
  shipment**, so the two-shipment case has two rows sharing one `anomaly_id` (DECISIONS.md #8). It
  also updates those shipments' rows in `ground_truth/expected_outcomes.csv` (`issue_type`,
  expected status, reasons and excursions), recomputed from the rules in `compliance_rules.md`.
- Labels live only in `ground_truth/`. Raw tables never gain a label column (enforced by the
  strict schemas), and the engine never reads `ground_truth/` (enforced by a Phase 4 test).
- In baseline data the carrier's claim agrees with the rules (PASS ⇔ RELEASE), so
  CARRIER_STATUS_CONTRADICTED only appears through injection.

## Summary

| issue_type | Records changed | Engine must flag | Expected status |
|---|---|---|---|
| `logger_not_assigned` | `temperature_readings.device_id` of the target shipment | LOGGER_NOT_ASSIGNED (+ DATA_GAP, LOW_COVERAGE, CARRIER_STATUS_CONTRADICTED) | INCONCLUSIVE |
| `product_threshold_mismatch` | `shipments.declared_policy_id`, plus a shaped block of readings | POLICY_MISMATCH_DECLARED + the breach under the correct policy | NON_COMPLIANT (or COMPLIANT_WITH_WARNINGS) |
| `missing_data_silently_passed` | a block of `temperature_readings` deleted; `carrier_reported_status` = PASS | DATA_GAP (+ LOW_COVERAGE if applicable) + CARRIER_STATUS_CONTRADICTED | INCONCLUSIVE |
| `custody_gap_not_flagged` | one middle HANDOFF `custody_events` row deleted | CUSTODY_GAP + CARRIER_STATUS_CONTRADICTED | INCONCLUSIVE |
| `excursion_across_unrelated_shipments` | two shipments: device reassignment back to back, plus shaped readings at the end of A and the start of B | each shipment's own excursions, with separate durations | per shipment (from the rules) |

## 1. `logger_not_assigned`

- **Injection:** for one target shipment, set `device_id` on its readings to a different device
  (one that has no assignment covering that window). The `logger_id` stays the same, so the
  readings still claim to belong to the shipment.
- **Why it matters:** the readings cannot be attributed to the device that was assigned and
  calibrated for this shipment.
- **Engine:** LOGGER_NOT_ASSIGNED. The readings are untrusted and excluded (compliance_rules.md
  §1.2), so trusted coverage collapses: DATA_GAP and LOW_COVERAGE follow. The carrier reported
  PASS, so CARRIER_STATUS_CONTRADICTED is added too. No excursion reports.
- **Expected:** INCONCLUSIVE → REVIEW; `expected_excursion_count = 0`.
- **Ground truth:** `affected_record_ids` = the logger ID and the device ID used.
- **Example (Phase 5):** _to be filled from starter output._

## 2. `product_threshold_mismatch`

- **Injection:** the planner's `declared_policy_id` is set to a *different temperature class*
  from the product's own class (e.g. COOL_8_15 for a REFRIG_2_8 vaccine). The injector also
  reshapes a block of readings so that they pass under the declared policy but break the correct
  one (e.g. a run at 9–10 °C: inside 8–15, above 8).
- **Why it matters:** an engine that trusts the declaration would release a damaged product.
- **Engine:** resolves the correct policy from the product and the departure date, adds
  POLICY_MISMATCH_DECLARED (`policy_source = PRODUCT_OVERRIDE`), and decides under the correct
  policy, typically SINGLE_DURATION_BREACH. CARRIER_STATUS_CONTRADICTED is added if the carrier
  reported PASS.
- **Expected:** NON_COMPLIANT → QUARANTINE when the shaped run breaches; the expected durations
  come from the correct policy.
- **Ground truth:** `affected_record_ids` = shipment ID, declared policy ID and the reshaped
  reading IDs.
- **Example (Phase 5):** _to be filled from starter output._

## 3. `missing_data_silently_passed`

- **Injection:** delete a contiguous block of readings longer than `max_reading_gap_min` (at least
  3 consecutive readings, so the gap is ≥ 40 min) from the middle of the window, and keep
  `carrier_reported_status = PASS`.
- **Why it matters:** "no out-of-range readings" is not proof of compliance when data is
  missing.
- **Engine:** DATA_GAP; LOW_COVERAGE if the deletion pushes coverage below 95 %;
  CARRIER_STATUS_CONTRADICTED (PASS vs REVIEW).
- **Expected:** INCONCLUSIVE → REVIEW.
- **Ground truth:** `removed_record_ids` = the deleted reading IDs; `affected_record_ids` = the
  readings just before and after the gap.
- **Example (Phase 5):** _to be filled from starter output._

## 4. `custody_gap_not_flagged`

- **Injection:** on a shipment with at least one intermediate stop, delete one middle HANDOFF
  custody event. The chain breaks: the previous event's `to_party_id` no longer equals the next
  event's `from_party_id`, and the sequence numbers skip one.
- **Why it matters:** an unaccounted custody interval means nobody can vouch for the goods
  during it.
- **Engine:** CUSTODY_GAP; CARRIER_STATUS_CONTRADICTED (PASS vs REVIEW).
- **Expected:** INCONCLUSIVE → REVIEW.
- **Ground truth:** `removed_record_ids` = the deleted custody event ID; `affected_record_ids` =
  the events on either side of it.
- **Example (Phase 5):** _to be filled from starter output._

## 5. `excursion_across_unrelated_shipments`

- **Injection:** pick two shipments A and B that use the same device back to back: B's
  assignment starts at or after A's ends, so the windows never overlap. Devices are reused at
  both scales, so such pairs exist naturally; the injector prefers an existing consecutive pair.
  Then shape A's last readings and B's first readings warm, both above `max_c`.
- **Why it matters:** a naive engine that groups readings by *device* sees one long excursion
  spanning both shipments and computes a wrong, inflated duration (possibly a false breach).
- **Engine:** computes excursions per logger assignment window only (compliance_rules.md §1.5).
  A gets an excursion ending at its last reading; B gets one starting at its first reading. Each
  duration is computed separately, and the status follows from each duration.
- **Expected:** per shipment, from the rules. The two `anomalies` rows share one `anomaly_id`
  and each carries its own `expected_excursion_durations_min`.
- **Ground truth:** `affected_record_ids` = both logger IDs and the reshaped reading IDs.
- **Example (Phase 5):** _to be filled from starter output._
