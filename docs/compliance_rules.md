# Compliance Rules

The rules the compliance engine (Phase 4) applies to every shipment, using only `raw/` tables
(never `ground_truth/`). They are deterministic: the same input always yields the same decision,
reason codes, excursion reports and durations. Constants come from `config/default.yaml` and the
applied policy; the status/reason/action mappings live in `src/coldchain/enums.py`
(`REASON_STATUS`, `STATUS_ACTION`, `worst_status`).

Notation: *i* = the device's `reading_interval_min` (10), *W* = the logger assignment window
`[assigned_at, unassigned_at]`, *u* = the device's `uncertainty_c`.

## 1. Definitions and formulas

### 1.1 Applied policy

1. Collect the `policy_code` of every product in the shipment (`shipment_lots → lots → products`).
   All of them must be the same: one container is one temperature class. A shipment that mixes
   codes breaks an invariant, so the engine stops with an error (and validation reports it).
2. The **applied policy** is the version of that code whose effective window contains
   `actual_departure_at`: `effective_from ≤ departure < effective_to`, with dates read as UTC
   midnight and a null `effective_to` treated as open-ended. Exactly one version matches, because
   versions never overlap.
3. The planner's `declared_policy_id` is checked against it but never used on its own. Any
   difference (code *or* version) adds **POLICY_MISMATCH_DECLARED**, and `policy_source` becomes
   `PRODUCT_OVERRIDE`. When they agree, `policy_source` is `DECLARED_CONFIRMED`.

*Example:* REFRIG_2_8 departing `2026-06-30T23:50Z` uses V1 (30 min single allowance);
departing `2026-07-01T00:00Z` uses V2 (20 min).

*Version-only mismatch:* the planner declares the version effective at `planned_departure_at`.
A shipment planned for `2026-06-30T22:00Z` (declared V1) that actually departs at
`2026-07-01T01:00Z` is evaluated under V2 and legitimately gets POLICY_MISMATCH_DECLARED, a
warning only (DECISIONS.md #26). The ground truth derives this from the same rule.

### 1.2 Trusted readings

A reading counts for the shipment when **all** of these hold:

- its `logger_id` is the shipment's logger;
- its `device_id` is the device in that logger assignment;
- its `recorded_at` lies inside *W* (both ends inclusive).

Readings on the shipment's logger inside *W* whose `device_id` differs from the assignment are
**untrusted**. They add **LOGGER_NOT_ASSIGNED** and are excluded from every gap, coverage,
excursion and uncertainty calculation, because they cannot be attributed to the assigned,
calibrated device (DECISIONS.md #10). Readings outside *W* are ignored for every rule, the device
check included: a wrong-device reading outside *W* adds nothing (DECISIONS.md #60).

If the shipment has **no logger assignment**, the engine adds LOGGER_NOT_ASSIGNED, uses
`[actual_departure_at, actual_arrival_at]` as *W*, and treats the shipment as having zero trusted
readings. Without a device, *i* is the config `reading_interval_min`, and CALIBRATION_EXPIRED and
NEAR_LIMIT_UNCERTAIN are not evaluated (DECISIONS.md #59).

### 1.3 Coverage

```
expected_reading_count = floor((window_end − window_start) / i) + 1
reading_count          = number of trusted readings
coverage_pct           = round(100 × reading_count / expected_reading_count, 2)
```

A logger window lies on the reading grid, so the division is exact. The no-logger fallback
window usually does not, so the count is floored (DECISIONS.md #59).

**LOW_COVERAGE** if `coverage_pct < min_coverage_pct` (strict: exactly 95.00 % passes).

### 1.4 Gaps

The trusted reading times, sorted, are padded with two *virtual* readings: one at
`assigned_at − i` and one at `unassigned_at + i`. Each **gap** is the difference between
consecutive times in that padded list:

```
max_gap_min = max over k of (t[k+1] − t[k])     (in minutes)
```

A complete stream therefore has `max_gap_min = i`. Missing *m* consecutive readings creates a gap
of `(m + 1) × i`. A gap exists when the difference is `> i`. It breaches the policy, adding
**DATA_GAP**, when `max_gap_min > max_reading_gap_min` (strict). The padding also catches a logger
that starts late or stops early. With zero trusted readings, `max_gap_min = (window) + 2i`, and
the engine adds both DATA_GAP and LOW_COVERAGE.

| Missing readings in a row | Gap (min) | DATA_GAP (limit 30)? |
|---|---|---|
| 0 | 10 | no |
| 2 | 30 | no (boundary) |
| 3 | 40 | yes |

### 1.5 Excursions

- **Out of range:** `temperature_c > max_c` (HIGH) or `temperature_c < min_c` (LOW). A reading
  exactly at `min_c` or `max_c` is *in range*.
- **Excursion:** a maximal run of consecutive trusted readings (sorted by `recorded_at`) that are
  out of range in the same direction. Excursions are computed **per logger assignment only**,
  never per device, so back-to-back shipments on one device are never merged. An in-range
  reading, or a change of direction, ends the run. A **gap does not end the run** (conservative:
  the missing time counts as excursion time; the gap itself is also judged by §1.4) (DECISIONS.md #9).
- **Report fields:** `started_at` / `ended_at` = first / last reading of the run;
  `reading_count`; `peak_c` = max (HIGH) or min (LOW) temperature; first/last reading IDs.
- **Duration:**

  ```
  duration_min = (ended_at − started_at) + i
  ```

  So a single out-of-range reading lasts *i* minutes, and *n* consecutive readings last *n × i*.

- **Breach type** of each excursion (first match wins):
  1. `ABSOLUTE_LIMIT_BREACH` if any reading in the run is `> abs_max_c` or `< abs_min_c`;
  2. `SINGLE_DURATION_BREACH` if `duration_min > max_single_excursion_min`;
  3. `WITHIN_ALLOWANCE` otherwise.
- **Cumulative:** `total_excursion_min = Σ duration_min` over all excursions of the shipment,
  in both directions. **CUMULATIVE_DURATION_BREACH** if `total_excursion_min > max_cumulative_excursion_min`.
- **Report numbering:** `EXCR-2026-000001, …`, assigned in order of
  (`shipment_id`, `started_at`, `direction`) over the whole run.

| Run under REFRIG_2_8 V1 (max_c 8, abs_max_c 12, single 30) | Duration | Breach type |
|---|---|---|
| 3 readings at 8.5 °C | 30 | WITHIN_ALLOWANCE (boundary) |
| 4 readings at 8.5 °C | 40 | SINGLE_DURATION_BREACH |
| 1 reading at 12.00 °C | 10 | WITHIN_ALLOWANCE (12.00 is not > 12) |
| 1 reading at 12.01 °C | 10 | ABSOLUTE_LIMIT_BREACH |
| 3 readings at 8.5 °C under **V2** (single 20) | 30 | SINGLE_DURATION_BREACH |

### 1.6 Calibration uncertainty

**NEAR_LIMIT_UNCERTAIN** if any trusted reading satisfies `|temperature_c − min_c| ≤ u` or
`|temperature_c − max_c| ≤ u`, where the difference is rounded to 2 decimals before comparing.
So a reading exactly at `max_c` is in range but uncertain, giving COMPLIANT_WITH_WARNINGS.

### 1.7 Calibration validity

Calibration is valid from `calibrated_on` through `calibration_due_on` (inclusive). The engine adds
**CALIBRATION_EXPIRED** if the calibration does not cover the whole window:
`calibration_due_on < date(unassigned_at)` or `calibrated_on > date(assigned_at)`.

### 1.8 Custody chain

Custody events are sorted by `sequence`. The engine adds **CUSTODY_GAP** if any of these hold:

- there are no custody events;
- the sequences are not contiguous `1..n` (an event is missing);
- an event's `from_party_id` ≠ the previous event's `to_party_id`;
- the first event is not a PICKUP from the shipment's supplier;
- the last event is not a DELIVERY to the shipment's receiver.

### 1.9 Dwell

**LONG_DWELL** if any route stop has `dwell_min > long_dwell_threshold_min` (120, strict).

### 1.10 Carrier claim

The carrier's `carrier_reported_status` is an input claim, never trusted. PASS means "release it".
The engine adds **CARRIER_STATUS_CONTRADICTED** when the claim and the engine's action disagree:
PASS while the action is not RELEASE, or FAIL while the action is RELEASE. This code is
informational: it never changes the status.

## 2. Decision table

The engine evaluates the rules in this order: policy resolution (§1.1) → logger and calibration
(§1.2, §1.7) → coverage and gaps (§1.3, §1.4) → excursions (§1.5) → uncertainty (§1.6) →
custody (§1.8) → dwell (§1.9). It collects **all** reason codes, takes the **most severe** status,
derives the action, and finally checks the carrier claim (§1.10).

| Severity | Status | Triggered by (reason codes) | Action |
|---|---|---|---|
| 4 | NON_COMPLIANT | ABSOLUTE_LIMIT_BREACH, SINGLE_DURATION_BREACH, CUMULATIVE_DURATION_BREACH | QUARANTINE |
| 3 | INCONCLUSIVE | LOGGER_NOT_ASSIGNED, DATA_GAP, LOW_COVERAGE, CUSTODY_GAP, CALIBRATION_EXPIRED | REVIEW |
| 2 | COMPLIANT_WITH_WARNINGS | EXCURSION_WITHIN_ALLOWANCE, NEAR_LIMIT_UNCERTAIN, POLICY_MISMATCH_DECLARED, LONG_DWELL | RELEASE |
| 1 | COMPLIANT | (none) | RELEASE |
| — | (no effect) | CARRIER_STATUS_CONTRADICTED | — |

- `reason_codes` are written once each, `|`-separated, in the order of the table above (the
  declaration order of `ReasonCode`).
- EXCURSION_WITHIN_ALLOWANCE is added when at least one excursion is WITHIN_ALLOWANCE, even if
  another excursion breaches.
- A status that is not NON_COMPLIANT never hides a breach: every code is kept for traceability.

## 3. Traceability

Every `compliance_decisions` row can be followed back to its evidence:

```
compliance_decisions.applied_policy_id ──► threshold_policies (limits used)
compliance_decisions.excursion_report_ids ──► excursion_reports
    └─ excursion_report_readings (report_id, reading_id) ──► temperature_readings
compliance_decisions.reading_count / expected_reading_count / coverage_pct / max_gap_min
    ──► recomputable from temperature_readings + loggers (§1.2–1.4)
```

The schema also checks that `first_reading_id` / `last_reading_id` encode the report's logger
and its start/end times, so a report cannot point at readings from another shipment.

## 4. Status events appended by the engine

For every shipment the engine appends to `compliance/compliance_status_events.csv`, never to the
raw history. The sequence numbers continue after the last raw event.

| Decided action | Appended events | Actor |
|---|---|---|
| RELEASE | UNDER_REVIEW, then RELEASED | logistics analyst, then operations manager |
| QUARANTINE | UNDER_REVIEW, then QUARANTINED | logistics analyst, then operations manager |
| REVIEW | UNDER_REVIEW only (waits for a human) | logistics analyst |

**Actors** are assigned round-robin: the *k*-th shipment in `shipment_id` order (0-based) gets
the analyst at position *k* mod *n* among the LOGISTICS_ANALYST parties sorted by `party_id`, and
the operations manager chosen the same way (DECISIONS.md #61). No randomness is involved.

`event_at` is the logger's `unassigned_at` (or `actual_arrival_at` when there is no logger). It
is derived from the data and never taken from the wall clock.

## 5. Boundary summary

| Rule | Passes at the boundary | Fails just beyond |
|---|---|---|
| In range | `temperature_c == max_c` / `== min_c` | `max_c + 0.01` / `min_c − 0.01` |
| Absolute limit | `== abs_max_c` / `== abs_min_c` | `abs_max_c + 0.01` |
| Single excursion | `duration_min == max_single_excursion_min` | `+ i` |
| Cumulative | `total == max_cumulative_excursion_min` | `+ i` |
| Gap | `max_gap_min == max_reading_gap_min` | `+ i` |
| Coverage | `coverage_pct == min_coverage_pct` | `coverage_pct < min_coverage_pct` |
| Dwell | `dwell_min == 120` | `121` |
| Near limit | `abs(t − limit) > u` → no warning | `abs(t − limit) ≤ u` → warning (inclusive) |
| Policy version | departure `< 2026-07-01T00:00Z` → V1 | departure `≥` → V2 |
