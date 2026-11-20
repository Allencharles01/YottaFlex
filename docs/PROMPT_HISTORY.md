# AI Prompt History – YottaFlex Cold Chain Compliance Challenge

## 1. AI Tool Information

* **AI Tools:**
  * Claude (claude.ai chat, Anthropic, model Opus 5.5): planning, explanations and independent review
  * Claude Code (VS Code extension, Anthropic, model Opus 5.5): implementation
* **Dates of Use:** November 19–20, 2026
* **Other AI Tools Used:** None

## 2. Prompt History and Assistance Summary

The entries below summarise my requests during the project, grouped by purpose. The prompts are lightly edited for readability, not verbatim copies. The full, unedited Claude Code session transcripts are in [`claude_code/`](../prompt_history/claude_code/), and the master prompt is saved verbatim in [`docs/PROJECT_PLAN.md`](../docs/PROJECT_PLAN.md).

### Prompt 1 – Understanding the Brief

**Prompt:**

> I've attached the brief. Can you break it down in simple terms using headings and bullet points? I want to understand what the project actually is, what they're asking me to build, what the final deliverable should look like, and whether there's anything specific I should focus on.

### Prompt 2 – Tech Stack and Roadmap

> What tech stack would you recommend? I'm leaning towards Python. Can you also lay out a roadmap in phases, where each phase ends with something working and tested before we move on to the next?

### Prompt 3 – Scope Clarification

> Before I start building, who are the users of this system? Should there be separate views for the supplier, carrier, driver and receiver? Here's how I picture the flow from supplier to receiver. Am I understanding it correctly?

This made it clear that the brief asks for an after-the-fact audit pipeline with synthetic data, planted anomalies and a compliance checker, not a live tracking app with logins.

### Prompt 4 – Master Prompt for the Implementation Agent

> With everything we've discussed, give me a master prompt to operate on in Claude Code. It should follow solid engineering principles (readability, maintainability, scalability, modularity, error handling) and break the work into phases. Each phase should include the folder and file structure and test steps I can run myself. At the end of each phase, it should print a status report that I'll bring back here for review before approving the next phase.

### Prompt 5 – Environment Setup

> Can you walk me through setting up my environment before I start? That covers checking Python and Git, the VS Code extensions I'll need, and the project folder. Also, which model and effort settings should I use in Claude Code for each phase?

This included moving the project to a Python 3.12 virtual environment, because my system Python 3.15 was too new for some data libraries.

### Prompt 6 – Phase-by-Phase Implementation (Claude Code)

**Prompt:**

> Read `YottaFlex_Master_Prompt.md` and follow it exactly. Start with Phase 0.

Each later phase started with "Proceed with Phase N per docs/PROJECT_PLAN.md", plus reminders from the previous review:

| Phase | Work |
|---|---|
| 0 | Project setup |
| 1 | Data dictionary, ER diagram, schemas, compliance rules |
| 2 | Master data generator |
| 3 | Temperature readings, custody and status events |
| 4 | Compliance engine |
| 5 | Anomaly injection and ground truth |
| 6 | Validation and evaluation |
| 7 | Full-scale run and CSV/JSON/SQL export |
| 8 | Reproducibility, run-all and README |
| 9 | Streamlit dashboard |

### Prompt 7 – Phase Review and Acceptance Testing

> Here's the status report for Phase N. Can you review it against the plan and give me the test steps to verify it on my machine?

I ran every command myself and pasted the real output back, one step at a time, until the reviewer and I were satisfied. Claude Code's multiple-choice design questions also came here, so I understood the trade-offs before answering.

### Prompt 8 – Realism and Correctness Fixes

Reviews led to revision requests, including:
- realistic temperature ramps instead of square-wave excursions;
- reporting on clipped readings;
- saving each device's hidden calibration offset to the ground truth;
- choosing back-to-back device pairs with the shortest idle time;
- a "full tests before every commit" rule after a commit with a failing test;
- a seed-consistency check across pipeline stages;
- documenting openly a detector correction from 96% to 100% precision.

### Prompt 9 – Security, Scalability, Observability and Auditing

> Have we accounted for security, scalability, observability and auditing in this project?

This led to:
- safe YAML loading, a fictional-names check and a `pip-audit` scan;
- pinned lockfiles and safe SQL escaping;
- a provenance manifest;
- per-stage timing, `--log-level` and `run_summary.json`.

### Prompt 10 – Dashboard and Visual Verification

> Should I build the optional dashboard?

After building it, I checked every page by eye and reported:

> The temperature chart isn't drawing any data. There's no readings line, no excursion windows, and the x-axis shows plain numbers instead of dates, even though the stats above the chart are correct.

All automated tests had passed. The root cause was found and fixed, regression tests were added, and overlapping chart labels were also fixed.

### Prompt 11 – GitHub, Submission Guidelines and Disclosure

> Since the project is complete, run a final check to confirm we've met all our targets. If everything checks out, help me get it ready to push to GitHub. That means a public repo, checking it against the submission guidelines, exporting my prompt history, and writing up how I used AI on this project.

The AI usage disclosure (`docs/AI_USAGE.md`) was written from my own answers to questions about how I worked.

## 3. AI Usage Disclosure

Claude and Claude Code were used as technical development assistants throughout the project. The work was split between them:

* **Claude (chat):** understanding the brief, architecture and roadmap planning, writing the master prompt, reviewing each phase, designing acceptance tests, debugging guidance and submission preparation.
* **Claude Code:** writing all the code, tests and project documentation.

I directed the work phase by phase, ran every acceptance test on my own machine, answered the design questions, approved each phase and controlled every commit. Full details are in [`docs/AI_USAGE.md`](../docs/AI_USAGE.md).

## 4. Final Verification Results

* **Automated tests:** 1,639 passed (99% coverage); ruff lint and format checks clean.
* **Shipments:** 5,000 at full scale (50 at starter scale).
* **Logger devices:** 1,000 (each assigned to many shipments over time).
* **Temperature readings:** 1,162,819 at 10-minute intervals.
* **Injected anomalies:** 225 across all 5 required edge-case types, plus 250 ground-truth rows. Every type was detected with 100% precision and recall.
* **Compliance results:**
  * 5,000 of 5,000 shipments match the answer key (status, reason codes and excursion durations).
  * Excursion-duration exact-match rate: 100%.
* **Validation:**
  * 133 checks; 0 integrity failures.
  * 13,160 business-rule findings, all explained by injected anomalies (unexplained: 0).
  * The clean baseline has 0 findings.
* **Negative tests:** corrupted data and mixed seeds are refused (exit code 1).
* **Export:** CSV, JSON and SQL. The SQL loads into both DuckDB and SQLite with foreign keys enforced, and row counts match the CSVs.
* **Full-scale pipeline:** completes in about 3.5 minutes on a laptop (peak memory about 1.6 GB).
* **Reproducibility:**
  * 134 of 134 output files are identical across two runs with the same seed; a different seed changes 119 files.
  * A fresh clone produced an identical dataset fingerprint.
* **Security:** `pip-audit` found no known vulnerabilities.
