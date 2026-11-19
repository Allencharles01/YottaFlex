# YottaFlex — Cold Chain Compliance Pipeline

A reproducible Python data pipeline that generates synthetic cold-chain data (products, lots,
shipments, loggers, 10-minute temperature readings, custody handoffs), injects labelled anomalies,
and checks per shipment whether it stayed compliant, tracing every conclusion back to specific
readings and policies. All data is 100% synthetic.

> Status: Phase 0 (scaffolding). See `docs/PROJECT_PLAN.md` for the full plan.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
coldchain version
pytest
```

## Layout

- `config/`: pipeline configuration (`default.yaml`)
- `src/coldchain/`: the package
- `tests/`: pytest suite
- `docs/`: plan, decisions, brief
