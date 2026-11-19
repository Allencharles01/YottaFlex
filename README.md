# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 1 (design: data dictionary, ER diagram, schemas, rules). See
> `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest
```

## Layout

- `config/`: pipeline configuration (`default.yaml`) and reference data (`reference/*.yaml`:
  threshold policies, products, packaging, locations)
- `src/coldchain/`: the package (`schemas.py` holds the pandera schema of every table)
- `tests/`: pytest suite
- `docs/`: plan, decisions, brief, and the design documents:
  - [`data_dictionary.md`](docs/data_dictionary.md): every table and field
  - [`er_diagram.md`](docs/er_diagram.md): Mermaid ER diagram
  - [`compliance_rules.md`](docs/compliance_rules.md): decision table and formulas
  - [`anomaly_catalogue.md`](docs/anomaly_catalogue.md): the five injected edge cases
