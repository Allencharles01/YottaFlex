# CLAUDE.md — YottaFlex Cold Chain Compliance

Single source of truth: `docs/PROJECT_PLAN.md`. Decisions: `docs/DECISIONS.md`.

## Working rules
- Work only inside `/home/allencharles/Documents/YottaFlex`.
- One phase at a time; never pre-build future phases or create empty placeholder modules.
- Start each phase with a short plan (files, design decisions, risks), then implement.
- End of each phase: `ruff check .`, `ruff format --check .`, `pytest` (all green); run the demo
  commands; print Phase Summary, UAT steps, proposed commit message, and the Phase Status Report
  (template in PROJECT_PLAN.md §0.1) as the last thing, in one code block. Then STOP and wait for
  `Phase N approved` before committing.
- Ambiguity or contradiction → ask, don't guess. Log every decision in `docs/DECISIONS.md`.
- Develop on `starter` scale; `full` only when the phase says so.
- Never hand-edit generated output.
- Review feedback → apply, re-run checks, re-print the report with `Revision` incremented.
- Always use the project venv (Python 3.12): `source .venv/bin/activate`.

## Engineering principles
- Readable code, Google-style docstrings on every public function/class, type hints everywhere.
- Small single-responsibility modules; config in `config/`, never hard-coded.
- Vectorise with numpy/pandas; no Python loops over individual readings.
- DRY: one place for IDs (`ids.py`), schemas (`schemas.py`), config loading (`config.py`).
- Pure functions (DataFrame in → DataFrame out); I/O only in `io.py` / CLI.
- PEP 8, `logging` (no `print` in library code), pathlib, enums for allowed values.
- Validate config on load; custom exceptions in `errors.py`; handle empty inputs explicitly.
- snake_case columns, UTC ISO-8601 timestamps, °C floats rounded to 2 decimals.
- RNG: `np.random.Generator` only, one named stream per stage (`rng.py`).
- Determinism: sort tables by PK before writing, fixed rounding, no wall-clock time in data.
- Every module gets pytest tests, including boundary cases.

## ID formats (implemented ONLY in `src/coldchain/ids.py`)
| Entity | Format | Example |
|---|---|---|
| Party | `PTY-0001` | PTY-0042 |
| Product | `PRD-0001` | PRD-0007 |
| ThresholdPolicy | `THR-<CODE>-V<n>` | THR-REFRIG_2_8-V1 |
| Packaging | `PKG-0001` | PKG-0003 |
| Lot | `LOT-2026-000001` | LOT-2026-000123 |
| Device | `DEV-000001` | DEV-000981 |
| Shipment | `SHP-2026-000001` | SHP-2026-004999 |
| Logger (assignment) | `LOG-<shipment-id>-01` | LOG-SHP-2026-000001-01 |
| TemperatureReading | `TEMP-<logger-id>-<YYYYMMDDTHHMM>` | TEMP-LOG-SHP-2026-000001-01-20260310T0910 |
| RouteStop | `RST-<shipment-id>-<nn>` | RST-SHP-2026-000001-02 |
| CustodyEvent | `CUS-<shipment-id>-<nn>` | CUS-SHP-2026-000001-03 |
| ShipmentStatusEvent | `STE-<shipment-id>-<nn>` | STE-SHP-2026-000001-04 |
| ExcursionReport | `EXCR-2026-000001` | EXCR-2026-000057 |
| Ground-truth anomaly | `ANOM-000001` | ANOM-000014 |

Device = physical logger (reused, has calibration). Logger = one device assigned to one shipment.
