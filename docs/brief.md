Business Context / Problem
Prove that temperature-sensitive shipments remained within product and route requirements, and manage deviations.

Objective and Scope
Build a working solution for this end-to-end workflow: Prove that temperature-sensitive shipments remained within product and route requirements, and manage deviations. The solution should expose key decisions and exceptions, preserve traceability, and produce measurable outputs. Medium complexity means multiple related entities, conditional rules, at least one exception path and meaningful evaluation metrics.

Primary Actors / Systems
Supplier, planner, carrier/warehouse operator, logistics analyst and operations manager

Business Workflow and Rules

1. Generate/reference master data first: Product, Lot, Shipment, Packaging, Logger.
2. Generate dependent transaction/event records using valid parent IDs.
3. Apply business logic: Create product/lot requirements and packaging profiles; assign calibrated loggers to shipments; generate timestamped temperature readings and custody handoffs; define product-specific thresholds and allowed durations; inject controlled excursions and missing data; create compliance reports.
4. Persist state changes as explicit records/events where relevant; do not overwrite history.
5. Generate outcomes and ground-truth labels from documented rules, not independent random assignment.

Entities and Relationships
Core entities: Product, Lot, Shipment, Packaging, Logger, TemperatureReading, CustodyEvent, RouteStop, ThresholdPolicy, ExcursionReport. Define a primary key for every entity and foreign keys for every relationship. Document cardinality (1:1, 1:N, N:M), optional relationships, effective dates and lifecycle states. Use bridge tables for many-to-many relationships; retain event history for state changes; validate all joins. For deliberate anomaly cases, keep an issue_type/ground_truth label rather than silently corrupting unrelated rows.

ID / Nomenclature Conventions
LOT-2026-000001; SHP-2026-000001; LOG-<shipment-id>-01; TEMP-<logger-id>-<timestamp>; EXCR-2026-000001.

Synthetic Data Generation Instructions (Granular)
Step 1 — Write a data dictionary for: Product, Lot, Shipment, Packaging, Logger, TemperatureReading, CustodyEvent, RouteStop, ThresholdPolicy, ExcursionReport. For each field specify type, required/optional, allowed values, units, uniqueness and validation rule.
Step 2 — Create parent/master tables first, then child and event tables. Apply this domain-specific generation logic: Create product/lot requirements and packaging profiles; assign calibrated loggers to shipments; generate timestamped temperature readings and custody handoffs; define product-specific thresholds and allowed durations; inject controlled excursions and missing data; create compliance reports.
Step 3 — Apply deterministic IDs and naming: LOT-2026-000001; SHP-2026-000001; LOG-<shipment-id>-01; TEMP-<logger-id>-<timestamp>; EXCR-2026-000001. Keep counters zero-padded, IDs immutable, and referenced IDs exactly consistent across files.
Step 4 — Generate the baseline volume: Generate 5,000 shipments, 1,000 loggers and readings every 10 minutes. Include normal trips, excursions, calibration uncertainty, logger gaps and route dwell events. Use a fixed seed (e.g. 20261022) and parameterize scale so starter and full datasets use the same generator.
Step 5 — Generate valid baseline records first, then inject labelled edge cases: Logger not assigned to shipment; product threshold mismatch; missing data silently passed; custody gap not flagged; excursion duration computed across unrelated shipments.
Step 6 — Calculate derived values (totals, balances, scores, statuses and dates) from source records rather than generating them independently.
Step 7 — Produce a separate ground_truth.csv/json for known anomalies, expected decisions or labels; never leak it into input features.
Step 8 — Run validation: primary-key uniqueness, foreign-key integrity, allowed enums, date ordering, arithmetic reconciliation, lifecycle transitions and duplicate checks.
Step 9 — Export CSV/JSON/SQL seed data plus README, schema/data dictionary, generator command, fixed seed, source manifest and validation summary.
Step 10 — Regenerate twice with the same seed and compare row counts/checksums to demonstrate reproducibility.

Suggested Volume and Coverage
Generate 5,000 shipments, 1,000 loggers and readings every 10 minutes. Include normal trips, excursions, calibration uncertainty, logger gaps and route dwell events.

Required Edge Cases / Negative Scenarios
Logger not assigned to shipment; product threshold mismatch; missing data silently passed; custody gap not flagged; excursion duration computed across unrelated shipments.

Validation Rules / Acceptance Criteria
Compliance report traces each conclusion to readings and policy; gaps are flagged; excursion duration is reproducible; lot and shipment links remain intact.

Evaluation Focus for Generated Data
Review: (a) realistic distributions and meaningful variation; (b) consistent IDs and valid joins; (c) dependency/lifecycle consistency across entities; (d) correct derived values and reconciliation; (e) normal, boundary, failure and exception coverage; (f) separate ground truth; (g) fixed-seed reproducibility, documentation and lawful provenance.

Public Data Guidance / Lawful Use
External data is optional. If used, record source URL, dataset/version, retrieval date, license/terms, attribution, transformations and fields discarded. Use only data you are legally permitted to access and redistribute; do not bypass access controls or collect confidential/personal information. If licensing or suitability is unclear, use synthetic data and cite public reference data only for schema inspiration.
