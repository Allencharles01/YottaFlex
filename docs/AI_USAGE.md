# How I used AI on this project

## Summary

I built this project with heavy AI assistance, using two Claude tools in a deliberate split:

| Tool | Role |
|---|---|
| **Claude (claude.ai chat, Opus 5.5)** | Planner and **independent reviewer**: explained the brief, proposed the tech stack and phased roadmap, wrote the master prompt, reviewed every phase report, and designed the acceptance tests I ran |
| **Claude Code (VS Code extension, Opus 5.5, "Auto" mode)** | Implementer: wrote all the code, tests and project docs, phase by phase |
| **Me** | Set the direction, ran every acceptance test myself, decided whether each phase passed, answered design questions, and controlled every commit |

The prompt history is in [`prompt_history/`](../prompt_history/) (see "Prompt history" below).

## Workflow

1. **Understanding the brief.** I asked Claude to explain the brief in plain terms, researched the supply-chain terms myself, and re-read the brief. My first mental model was a live, end-to-end tracking app with screens for each role (supplier, carrier, driver, receiver). After re-reading the brief, I understood that it asks for something closer to a *black box*: an after-the-fact audit that reports once the journey is complete. That means synthetic data, planted anomalies, and a checker scored against an answer key.
2. **Plan.** I chose Python and asked for a phase-by-phase roadmap in which every phase is tested before moving on. Claude wrote a master prompt (saved verbatim as `docs/PROJECT_PLAN.md`) defining 10 phases, engineering principles, the data model, compliance rules and per-phase acceptance tests. I added the requirement that every phase ends with a fixed-format status report, which I bring to the reviewer before approving.
3. **Phase gates.** For each phase:
   - Claude Code implemented the phase and printed a status report.
   - I pasted the report into the reviewer chat. The reviewer checked it against the plan and gave me step-by-step tests.
   - **I ran every test in my own terminal** and pasted the real output back.
   - Only after the reviewer and I were satisfied did I send `Phase N approved`, and Claude Code committed. One commit per phase (Phases 0–9), plus a commit for the prompt-history export.
4. **Design questions.** When Claude Code hit ambiguity, it stopped and asked multiple-choice questions. I brought each one to the reviewer chat and made sure I understood the trade-off. In most cases I then chose the recommended option.

## Where AI accelerated the work

- **All implementation code** and **1,639 automated tests** were written by Claude Code.
- The read-only Streamlit dashboard (6 pages) was built by Claude Code in Phase 9.
- The data dictionary, ER diagram, compliance rules, README and 131 logged design decisions were drafted by AI.
- Performance work: vectorised generation of 1.16M readings, with the full pipeline taking about 3.5 minutes.

## Where I relied heavily on AI

- **I did not write the code line by line.** The architecture, data model and algorithms came from the AI-written master prompt and Claude Code's implementation.
- The domain rules (excursion duration formula, decision table, anomaly designs) were proposed by AI. I reviewed and approved them.
- **Domain knowledge.** The cold-chain domain (temperature excursions, custody handoffs, compliance policies) and the approach of generating synthetic data with planted anomalies and scoring it against an answer key were new to me. I learned them through the AI's explanations during this project.

## What I personally reviewed, changed or validated

**Validation I ran myself, every phase** (in my own terminal, on my machine):
- Test suites, lint checks and checksum comparisons for reproducibility
- Tracing a real decision end to end: SHP-2026-000018 → excursion report → 5 readings → policy
- Viewing temperature plots of real shipments
- Comparing the checker's output with the answer key (50/50 at starter, 5,000/5,000 at full scale)
- **Negative tests**: deliberately corrupting data and confirming that validation and export refuse it (exit code 1)
- The full-scale run (3 min 22 s) and a **fresh-clone test** whose fingerprint matched exactly
- Clicking through all six dashboard pages and comparing them against known results (the screenshots in the README are mine)

**Issues found during review that changed the build:**

| Found during review | What changed |
|---|---|
| Temperature excursions were a perfect "square wave", which is physically unrealistic | Gradual ramps up and down (Phase 3, Rev 2) |
| Natural readings were clipped to stay within limits, which could distort the data | Added a clipping report: 0.00% clipped |
| Each logger's hidden calibration offset wasn't saved anywhere | Saved to the ground truth as `device_truth.csv` |
| The back-to-back device trap used pairs with days of idle time | Shortest-idle pair chosen: 1–7 h at full scale |
| The Phase 3 commit contained a failing test | New rule: full tests before every commit |
| Removing the seed from the config fingerprint left a gap | Export now refuses mixed seeds (a real bug, reproduced, then fixed) |
| One detector was "fixed" from 96% to 100% precision | Required the full story to be documented openly (`docs/evaluation.md` §5) |
| **I checked the dashboard by eye and found the temperature chart drew no data at all** (no line, no excursion windows, a numeric x-axis), although all 1,620 automated tests passed | Root cause: plotly.js typed the axis from an empty legend trace. Fixed, and regression tests added on the JSON the browser actually receives (they fail on the old code) |
| On the dashboard, excursion labels overlapped on a shipment with 4 close excursions | Labels are now staggered, with a test |

**My own decisions and questions:**
- I chose **Python** and asked for a **phase-by-phase plan with testing after every phase**.
- I required a **status report at the end of every phase**, reviewed before approving, so no phase moved forward untested.
- I asked whether the project needed role-based pages and logins. It didn't; that question clarified the scope.
- **I asked mid-project whether security, scalability, observability and auditing had been considered.** This led to safe YAML loading, a fictional-names check, `pip-audit`, pinned lockfiles, safe SQL escaping, the provenance manifest, `--log-level` and `run_summary.json`.
- I chose to build the optional **Phase 9 dashboard**, and checked every page by eye. That's how the chart bug above was found.

**Environment decisions:**
- Python 3.15 was too new for some libraries, so the project uses a Python 3.12 virtual environment.

## What I learned

- **The client's business, not just the client's request.** Reading the brief, I had heard of terms like Product, Lot, Shipment, Packaging and Logger, and knew roughly what they were, but not how they function. As I researched them, I began to see them as the building blocks of a supply chain: a lot is a production batch of a product, a shipment carries lots in specific packaging, and a logger records the temperature that proves the product stayed safe. For a Forward Deployed role, where working directly with clients is central, this taught me that understanding the client's need isn't enough. I also need to understand their business to see the full picture.
- **Reading the brief carefully before building.** At first I assumed I should build end-to-end tracking software. Re-reading the brief showed me the real need was an audit of completed journeys. That correction early on saved me from building the wrong product.
- **Working with AI effectively, including token management.** AI made the implementation much faster, but it has to be directed. I learned to manage my usage limit by matching the AI's effort setting to the task: **High** for the design- and logic-heavy phases (data design, readings, the compliance engine, anomaly injection, debugging), and **Medium** for setup, exports, documentation, approvals and commits. At one point I reached 92% of my usage limit and had to plan around the reset.

## Honest limitations of my AI use

- The reviewer was also an AI. My own role was directing the work, running and checking tests, and making the final call on each phase, rather than reviewing every line of code.
- I understand the overall architecture and can explain the design, each phase, and how each was verified. For internal implementation details I would need to read the code.

## Prompt history

- [`prompt_history/claude_code/`](../prompt_history/claude_code/): the Claude Code session transcripts, one per phase, exported from VS Code
- `docs/PROJECT_PLAN.md`: the master prompt, verbatim
