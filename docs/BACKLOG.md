# Backlog

Project notes and deferred work items. Each item says where it came from; remove it (or move it
to `DECISIONS.md`) once it is done.

| # | Added | Origin | Item | Status |
|---|---|---|---|---|
| 1 | 2026-11-19 | Phase 2 review | Add a pytest `slow` marker for the large parametrised schema cases so `pytest -m 'not slow'` gives a fast dev run. | Open |
| 2 | 2026-11-19 | Phase 4 approval | **Phase 5:** injected anomalies must be ones the engine can flag (well-formed data that breaks a rule), never ones that trigger a `ComplianceError` (e.g. unknown device, no lots, no effective policy). Add a test that `coldchain check` completes on injected data. | Open |
