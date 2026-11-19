# Backlog

Project notes and deferred work items. Each item says where it came from; remove it (or move it
to `DECISIONS.md`) once it is done.

| # | Added | Origin | Item | Status |
|---|---|---|---|---|
| 3 | 2026-11-19 | Phase 5 approval | **Phase 6 (security):** verify that `yaml.safe_load` is used everywhere YAML is read; check that every party/company/product name is fictional (no real brands); add `pip-audit` to the dev dependencies and run it. | Open |
| 4 | 2026-11-19 | Phase 5 approval | **Phase 7 (security + audit):** pin dependency versions (lockfile); the SQL export must escape/quote every value safely; `manifest.json` includes the git commit hash, config hash, input file checksums and package version, so every output is traceable to the exact run. | Open |
| 5 | 2026-11-19 | Phase 5 approval | **Phase 8 (observability):** per-stage timing in the logs; a `--log-level` CLI option; a machine-readable `run_summary.json` per run; a "Security & Audit" section in the README. | Open |

_Done: #1 (pytest `slow` marker) and #2 (injected anomalies must be flaggable, never a `ComplianceError`) in Phase 5; moved to DECISIONS.md #77 and #78._
