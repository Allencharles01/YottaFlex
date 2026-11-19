# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 3 (event data, readings and natural scenarios). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest
coldchain generate --scale starter          # writes output/starter/{raw,ground_truth}/*.parquet + *.csv
```

`coldchain generate` options: `--scale starter|full`, `--seed N` (overrides the config),
`--config PATH` (default `config/default.yaml`), `--output-dir DIR` (default `output/`).
It generates every raw table (parties, threshold policies, products, packaging, lots, devices,
shipments, shipment lots, loggers, 10-minute temperature readings, route stops, custody events and
shipment status events) into `raw/`, and the answer key `expected_outcomes` (the natural scenario
of every shipment and its expected status, reason codes and excursion windows, derived from
`docs/compliance_rules.md`) into `ground_truth/`, together with `device_truth` (each device's
hidden systematic offset; never read by the compliance engine).

To plot one shipment's readings against its applied policy (needs the `dev` extra):
`python scripts/plot_shipment.py SHP-2026-000018` → `output/starter/plots/SHP-2026-000018.png`.

## Layout

- `config/`: pipeline configuration (`default.yaml`) and reference data (`reference/*.yaml`:
  threshold policies, products, packaging, locations)
- `src/coldchain/`: the package (`schemas.py` holds the pandera schema of every table, `io.py`
  reads/writes tables, `generate/` holds the data generators)
- `tests/`: pytest suite
- `docs/`: plan, decisions, brief, and the design documents:
  - [`data_dictionary.md`](docs/data_dictionary.md): every table and field
  - [`er_diagram.md`](docs/er_diagram.md): Mermaid ER diagram
  - [`compliance_rules.md`](docs/compliance_rules.md): decision table and formulas
  - [`anomaly_catalogue.md`](docs/anomaly_catalogue.md): the five injected edge cases
  - [`BACKLOG.md`](docs/BACKLOG.md): deferred work items
