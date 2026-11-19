# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 5 (edge-case injection and ground truth). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest                                      # full suite; `pytest -m "not slow"` for a fast dev run
coldchain generate --scale starter          # writes output/starter/{raw,ground_truth}/*.parquet + *.csv
coldchain inject --scale starter            # injects the 5 labelled edge cases (optional step)
coldchain check --scale starter             # reads raw/ only, writes output/starter/compliance/
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
  injectors, `compliance/` the engine)
- `tests/`: pytest suite
- `docs/`: plan, decisions, brief, and the design documents:
  - [`data_dictionary.md`](docs/data_dictionary.md): every table and field
  - [`er_diagram.md`](docs/er_diagram.md): Mermaid ER diagram
  - [`compliance_rules.md`](docs/compliance_rules.md): decision table and formulas
  - [`anomaly_catalogue.md`](docs/anomaly_catalogue.md): the five injected edge cases
  - [`BACKLOG.md`](docs/BACKLOG.md): deferred work items
