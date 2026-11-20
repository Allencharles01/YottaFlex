# Backlog

Project notes and deferred work items. Each item says where it came from; remove it (or move it
to `DECISIONS.md`) once it is done.

| # | Added | Origin | Item | Status |
|---|---|---|---|---|
| 4 | 2026-11-19 | Phase 5 approval | **Phase 7 (security + audit):** pin dependency versions (lockfile); the SQL export must escape/quote every value safely; `manifest.json` includes the git commit hash, config hash, input file checksums and package version, so every output is traceable to the exact run. | Open |
| 5 | 2026-11-19 | Phase 5 approval | **Phase 8 (observability):** per-stage timing in the logs; a `--log-level` CLI option; a machine-readable `run_summary.json` per run; a "Security & Audit" section in the README. | Open |
| 6 | 2026-11-20 | Phase 6 approval | **Phase 7:** pin `duckdb` in the lockfile; note the cosmetic DuckDB 2.0 `nanobind: leaked … instances` message (printed at interpreter exit in some pytest processes) in the README known issues. | Open |

_Done: #1 (pytest `slow` marker) and #2 (injected anomalies must be flaggable, never a `ComplianceError`) in Phase 5; moved to DECISIONS.md #77 and #78. #3 (security: `yaml.safe_load`, fictional names, `pip-audit`) in Phase 6; moved to DECISIONS.md #93._
