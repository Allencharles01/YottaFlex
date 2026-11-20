# Prompt history

This folder is the project's prompt history, submitted as the guidelines require ("Submit your
prompt history"). It records how YottaFlex was built with AI assistance: every prompt given to
the implementing agent and every reply it gave, phase by phase.

## How the project was built

Three roles, with a human gate between every step:

| Role | Who / what | Responsibilities |
|---|---|---|
| **Planner & reviewer** | Claude (claude.ai chat) | Wrote the master prompt that became [`docs/PROJECT_PLAN.md`](../docs/PROJECT_PLAN.md); drafted each phase's kickoff prompt; reviewed each Phase Status Report, answered open questions and wrote the approval / revision prompts. |
| **Implementer** | Claude Code in VS Code | Planned and implemented one phase at a time, logged every decision in [`docs/DECISIONS.md`](../docs/DECISIONS.md), ran `ruff check .`, `ruff format --check .` and `pytest` and the demo commands, then stopped with a Phase Status Report and waited for approval before committing. |
| **Owner & acceptance tester** | Me (the author) | Carried prompts between the two, ran every UAT / acceptance step myself on my own machine (CLI runs, plots, full-scale runs, fresh-clone test), and gave the final `Phase N approved` that allowed each commit. |

So a typical phase looks like this in the transcripts:

1. **Kickoff** — "Proceed with Phase N per docs/PROJECT_PLAN.md …" (drafted in the chat, pasted
   into Claude Code).
2. **Plan → implement → checks** — Claude Code's plan, its tool calls and its Phase Status
   Report.
3. **Review** — I run the UAT steps, the chat reviews the report, and I paste back either
   "Not approved yet — please make Revision 2 …" or "Phase N approved … answers to your open
   questions …".
4. **Commit** — Claude Code re-runs all checks and commits.

## Files

| Path | Contents |
|---|---|
| [`claude_code/INDEX.md`](claude_code/INDEX.md) | One row per session: file, session ID, start/end time, number of prompts, replies and tool calls. |
| `claude_code/<date>_phase-<n>.md` | One Claude Code session per file, in chronological order: Phase 0 (scaffold and plan) to Phase 8 (run-all, reproduce, final README). |
| `claude_code/<date>_<title>.md` | Sessions that were not a numbered phase, e.g. `2026-11-20_prompt-history-export-script.md` (this export). |

Each session file contains:

- **every prompt I sent, verbatim** (the paste wrappers and the harness's automatic
  system reminders are removed);
- **every text reply from Claude Code, verbatim**;
- **one line per tool call**: tool name plus its short description (e.g.
  `- **Bash** — Run full test suite`). Tool outputs are not included. The exception is
  `AskUserQuestion`, where the questions and my chosen answers are listed, because they
  are design decisions;
- one-line notes where a background task finished, a skill was loaded, or the context was
  automatically compacted.

Left out: the model's hidden reasoning, tool outputs (file contents, test logs) and the
automatic compaction summaries. The summaries restate content that is already in the same
file.

Timestamps are UTC. All dates are 2026-11-19 and 2026-11-20.

## Regenerating

The files under `claude_code/` are generated. Don't edit them by hand. They are rebuilt from
the local Claude Code transcripts (`~/.claude/projects/<project-path>/*.jsonl`) with:

```bash
source .venv/bin/activate
python scripts/export_prompt_history.py
```

The script scans the rendered Markdown for secrets (API keys, tokens, private keys, passwords,
credentials in URLs) **before writing anything**. If it finds any, it writes nothing, reports
each finding with the value redacted and exits with status 1. Tests:
[`tests/test_export_prompt_history.py`](../tests/test_export_prompt_history.py).

The claude.ai planning/review chat is not stored in this folder. Its output appears here as
the kickoff, revision and approval prompts in each session file.
