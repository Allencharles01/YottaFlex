# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 6 (validation suite and evaluation metrics). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest                                      # full suite; `pytest -m "not slow"` for a fast dev run
coldchain generate --scale starter          # writes output/starter/{raw,ground_truth}/*.parquet + *.csv
coldchain inject --scale starter            # injects the 5 labelled edge cases (optional step)
coldchain check --scale starter             # reads raw/ only, writes output/starter/compliance/
coldchain validate --scale starter          # integrity checks, writes output/starter/validation/
coldchain validate --scale starter --source baseline   # the clean pre-injection snapshot
coldchain evaluate --scale starter          # engine vs ground truth, writes output/starter/evaluation/
pip-audit --skip-editable                   # known-vulnerability scan of the installed dependencies
```

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

It exits 1 on any unexplained finding, and at starter scale on any metric below 100 %.
Definitions: [`docs/evaluation.md`](docs/evaluation.md).

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
  metrics)
- `tests/`: pytest suite
- `docs/`: plan, decisions, brief, and the design documents:
  - [`data_dictionary.md`](docs/data_dictionary.md): every table and field
  - [`er_diagram.md`](docs/er_diagram.md): Mermaid ER diagram
  - [`compliance_rules.md`](docs/compliance_rules.md): decision table and formulas
  - [`anomaly_catalogue.md`](docs/anomaly_catalogue.md): the five injected edge cases
  - [`evaluation.md`](docs/evaluation.md): validation checks, detection and metric definitions
  - [`BACKLOG.md`](docs/BACKLOG.md): deferred work items
