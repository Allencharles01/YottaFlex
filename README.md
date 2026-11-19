# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 2 (master data generator). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest
coldchain generate --scale starter          # writes output/starter/raw/*.parquet + *.csv
```

`coldchain generate` options: `--scale starter|full`, `--seed N` (overrides the config),
`--config PATH` (default `config/default.yaml`), `--output-dir DIR` (default `output/`).
It currently runs the master stage: parties, threshold policies, products, packaging, lots,
devices, shipments, shipment lots, route stops and loggers.

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
