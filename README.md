# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 7 (full-scale run and exports). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install --require-hashes -r requirements-dev.lock   # the exact, hash-checked tested versions
pip install --no-deps -e .                              # the package itself (deps already pinned)
coldchain version
pytest                                      # full suite; `pytest -m "not slow"` for a fast dev run
coldchain generate --scale starter          # writes output/starter/{raw,ground_truth}/*.parquet + *.csv
coldchain inject --scale starter            # injects the 5 labelled edge cases (optional step)
coldchain check --scale starter             # reads raw/ only, writes output/starter/compliance/
coldchain validate --scale starter          # integrity checks, writes output/starter/validation/
coldchain validate --scale starter --source baseline   # the clean pre-injection snapshot
coldchain evaluate --scale starter          # engine vs ground truth, writes output/starter/evaluation/
coldchain export --scale starter            # CSV / JSON / SQL bundle + manifest, output/starter/export/
pip-audit --skip-editable                   # known-vulnerability scan of the installed dependencies
```

**Dependencies.** `pyproject.toml` declares version ranges. The lockfiles pin the exact versions
the test suite ran against, each with its sha256 hashes: `requirements.lock` (runtime) and
`requirements-dev.lock` (runtime + tests, lint, audit; includes `duckdb`). With
`--require-hashes`, pip refuses any file whose hash differs. `pip install -e ".[dev]"` still
works but resolves the newest versions within the ranges. To refresh the locks after upgrading
the venv, run `scripts/lock_dependencies.sh` (needs `uv` and network access).
`tests/test_lockfile.py` fails when the installed versions drift from the lock.

`coldchain generate` options: `--scale starter|full`, `--seed N` (overrides the config),
`--config PATH` (default `config/default.yaml`), `--output-dir DIR` (default `output/`).
It generates every raw table (parties, threshold policies, products, packaging, lots, devices,
shipments, shipment lots, loggers, 10-minute temperature readings, route stops, custody events and
shipment status events) into `raw/`, and the answer key `expected_outcomes` (the natural scenario
of every shipment and its expected status, reason codes and excursion windows, derived from
`docs/compliance_rules.md`) into `ground_truth/`, together with `device_truth` (each device's
hidden systematic offset; never read by the compliance engine).

`coldchain inject` options: `--scale`, `--seed`, `--config`, `--output-dir` (as above). It
injects the five edge cases of [`docs/anomaly_catalogue.md`](docs/anomaly_catalogue.md) into
about 1 % of the shipments each (disjoint NORMAL, COMPLIANT shipments chosen by the seeded RNG,
at least one of each type). The first run copies the clean generator output to
`baseline/{raw,ground_truth}/`; every run starts from that snapshot, so re-running it gives the
same result. It rewrites `raw/` (only the target shipments' records change; diff against
`baseline/raw/` to see exactly which), updates `ground_truth/expected_outcomes` and writes
`ground_truth/anomalies.{parquet,csv,json}` (one row per affected shipment). Running `generate`
again removes the stale snapshot and anomaly list.

`coldchain check` options: `--scale`, `--config`, `--output-dir` (as above). It runs the
compliance engine (`docs/compliance_rules.md`) on the raw tables only, never the ground truth,
and writes four tables to `compliance/`: `excursion_reports` (one row per excursion),
`excursion_report_readings` (report → reading links), `compliance_decisions` (status, reason
codes, coverage, gaps and excursion totals per shipment) and `compliance_status_events`
(UNDER_REVIEW → RELEASED/QUARANTINED, appended after the raw history). Raw files are never
modified.

`coldchain validate` options: `--scale`, `--source raw|baseline`, `--config`, `--output-dir`.
It judges data integrity on its own, never reading the ground truth and never reusing the
engine. It runs 133 checks: primary keys, DuckDB foreign-key joins, enums, schema, duplicates,
date ordering, lifecycle transitions, cross-table consistency, and arithmetic reconciliation.
The reconciliation covers lot quantities, `dwell_min`, and excursion durations, coverage and
gaps recomputed from the readings. It writes `validation/validation_summary.{md,json}`, with
PASS/FAIL and counts per check and every finding. INTEGRITY checks must pass (exit 1
otherwise). BUSINESS_RULE checks report the defects that injected anomalies cause on purpose;
the clean baseline has none.

`coldchain evaluate` options: `--scale`, `--config`, `--output-dir`. Run it after `check` and
`validate`. It compares the engine with `ground_truth/` and writes
`evaluation/evaluation_report.{md,json}`. The report holds:
- precision, recall and F1 per issue type and per reason code;
- a status confusion matrix;
- the excursion-duration exact-match rate;
- accuracy per scenario;
- the cross-reference of every validation finding to the anomaly that explains it
  ("unexplained findings: 0").

It exits 1 on any unexplained finding, and at a strict scale (`starter` and `full`) on any
metric below 100 %.
Definitions: [`docs/evaluation.md`](docs/evaluation.md).

`coldchain export` options: `--scale`, `--config`, `--output-dir`, `--verify-sql/--no-verify-sql`
(default on). Run it last: it needs generate → [inject] → check → validate → evaluate with a
PASS verdict, and refuses to run when any stage's provenance record no longer matches the files
(e.g. `inject` was re-run after `check`). It writes `output/<scale>/export/`:
- `csv/<layer>/` and `json/<layer>/`: every table, with the ground truth in its own
  `ground_truth/` folders (`temperature_readings` as JSON Lines);
- `sql/schema.sql` and `sql/seed.sql`: DDL with PKs and FKs, then batched multi-row INSERTs in
  dependency order, for the raw and compliance tables;
- `sql/ground_truth_schema.sql` and `sql/ground_truth_seed.sql`: the same for the answer key,
  never mixed into `seed.sql`;
- the reports, the data dictionary, a README;
- `manifest.json`: seed, scale, commands, package version, git commit and dirty flag, config
  hash, input checksums, and every file's row count and sha256.

The SQL is proved to load into fresh DuckDB and SQLite databases with foreign keys enforced, and
the row counts are compared with the CSVs; the result is recorded in the manifest. Every
pipeline command also writes `output/<scale>/provenance/<stage>.json`. Format details:
[`docs/data_dictionary.md`](docs/data_dictionary.md#export-bundle-coldchain-export).

To trace a decision: take a shipment's `excursion_report_ids` in `compliance_decisions.csv`,
find those rows in `excursion_reports.csv`, follow `excursion_report_readings.csv` to the reading
IDs, and look them up in `raw/temperature_readings.csv`; `applied_policy_id` names the limits
in `raw/threshold_policies.csv`.

To plot one shipment's readings against its applied policy (needs the `dev` extra):
`python scripts/plot_shipment.py SHP-2026-000018` → `output/starter/plots/SHP-2026-000018.png`.
Add `--engine` (after `coldchain check`) to overlay the engine's excursion reports and show its
decision in the title.

## Layout

- `config/`: pipeline configuration (`default.yaml`) and reference data (`reference/*.yaml`:
  threshold policies, products, packaging, locations)
- `src/coldchain/`: the package (`schemas.py` holds the pandera schema of every table, `io.py`
  reads/writes tables, `generate/` holds the data generators, `inject/` the anomaly
  injectors, `compliance/` the engine, `validation/` the integrity checks, `evaluation/` the
  metrics, `export/` the CSV/JSON/SQL bundle and manifest, `provenance.py` the stage records)
- `tests/`: pytest suite
- `scripts/`: `plot_shipment.py` (one shipment's readings as a PNG) and `lock_dependencies.sh`
  (regenerates the lockfiles)
- `requirements.lock`, `requirements-dev.lock`: hash-pinned dependency versions
- `docs/`: plan, decisions, brief, and the design documents:
  - [`data_dictionary.md`](docs/data_dictionary.md): every table and field
  - [`er_diagram.md`](docs/er_diagram.md): Mermaid ER diagram
  - [`compliance_rules.md`](docs/compliance_rules.md): decision table and formulas
  - [`anomaly_catalogue.md`](docs/anomaly_catalogue.md): the five injected edge cases
  - [`evaluation.md`](docs/evaluation.md): validation checks, detection and metric definitions
  - [`BACKLOG.md`](docs/BACKLOG.md): deferred work items

## Known issues

- **`nanobind: leaked … instances` at exit.** DuckDB 2.0's Python bindings sometimes print this
  message when a pytest process exits, after the results have been reported. It is cosmetic:
  the exit status and the test results are unaffected. It comes from the DuckDB wheel, not from
  this project.
