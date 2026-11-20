"""Export this project's Claude Code session transcripts as readable Markdown.

Reads every ``*.jsonl`` session file in the Claude Code projects folder for this repository
(``~/.claude/projects/<project path with "/" replaced by "-">/``) and writes one Markdown file
per session to ``prompt_history/claude_code/``, named ``<date>_phase-<n>.md`` (or
``<date>_<title-slug>.md`` for sessions that are not a numbered phase), plus an ``INDEX.md``.

Each file contains, in chronological order:

* every user prompt in full (``<pasted_content>`` wrappers and ``<system-reminder>`` blocks
  removed);
* every assistant text reply in full;
* one line per tool call (tool name + short description); tool outputs are never included,
  except the user's answers to ``AskUserQuestion`` (they are user decisions);
* one-line notes for background-task notifications, loaded skills and context compaction.

Hidden reasoning ("thinking" blocks), tool outputs and the auto-generated compaction
summaries are left out.

Before anything is written, the rendered Markdown is scanned for secrets (API keys, tokens,
private keys, passwords). If any are found, nothing is written, the findings are logged with
the matched value redacted, and the script exits with status 1.

Usage:
    python scripts/export_prompt_history.py [--projects-dir DIR] [--output-dir DIR]
                                            [--exclude SESSION_ID ...]
"""

import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "prompt_history" / "claude_code"
MAX_TOOL_DESCRIPTION = 140

logger = logging.getLogger("export_prompt_history")

SECRET_PATTERNS: dict[str, re.Pattern[str]] = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "OpenAI-style API key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}"),
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "AWS access key ID": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "Google API key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "Stripe key": re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}"),
    "Private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "JSON Web Token": re.compile(
        r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"
    ),
    "Bearer token": re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]{20,}=*"),
    "Credentials in URL": re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{3,}@"),
    "Assigned secret": re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?token|auth[_-]?token)"
        r"\s*[:=]\s*['\"]?(?!\*)[^\s'\"<>{}()]{8,}"
    ),
}

_PASTED_TAG = re.compile(r"</?pasted_content(?:\s[^>]*)?>")
_SYSTEM_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_IDE_TAGS = re.compile(
    r"<(ide_selection|ide_opened_file|ide_diagnostics)>.*?</\1>", re.DOTALL | re.IGNORECASE
)
_TASK_SUMMARY = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_PHASE = re.compile(r"\bPhase\s+(\d+)\b")
_BLANK_LINES = re.compile(r"\n{3,}")


class SecretsFoundError(Exception):
    """Raised when the rendered prompt history contains something that looks like a secret."""


@dataclass(frozen=True)
class SecretFinding:
    """One suspected secret in a rendered session.

    Attributes:
        session_id: Session the finding belongs to.
        line: 1-based line number in the rendered Markdown.
        kind: Name of the pattern that matched.
        preview: The matched text with everything but its first four characters redacted.
    """

    session_id: str
    line: int
    kind: str
    preview: str


@dataclass
class Session:
    """A parsed session ready to be rendered.

    Attributes:
        session_id: Claude Code session UUID (the ``.jsonl`` file stem).
        title: The AI-generated session title, or an empty string.
        started: Timestamp of the first record, ``None`` if the file has none.
        ended: Timestamp of the last record, ``None`` if the file has none.
        entries: Ordered ``(role, timestamp, markdown)`` tuples; role is ``user``,
            ``assistant``, ``tool`` or ``note``.
        prompts: Number of user prompts.
        replies: Number of assistant text blocks.
        tool_calls: Number of tool calls.
    """

    session_id: str
    title: str = ""
    started: datetime | None = None
    ended: datetime | None = None
    entries: list[tuple[str, datetime | None, str]] = field(default_factory=list)
    prompts: int = 0
    replies: int = 0
    tool_calls: int = 0


def default_projects_dir(project_root: Path = PROJECT_ROOT) -> Path:
    """Return the Claude Code transcript folder for a project.

    Args:
        project_root: Absolute path of the project.

    Returns:
        ``~/.claude/projects/<path with every "/" and "." replaced by "-">``.
    """
    slug = re.sub(r"[/.]", "-", str(project_root))
    return Path.home() / ".claude" / "projects" / slug


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp as written by Claude Code.

    Args:
        value: Timestamp such as ``2026-11-19T11:18:35.147Z``, or ``None``.

    Returns:
        A timezone-aware datetime, or ``None`` when the value is missing or invalid.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def format_timestamp(value: datetime | None) -> str:
    """Format a timestamp for display (UTC, minute precision).

    Args:
        value: Timestamp or ``None``.

    Returns:
        ``YYYY-MM-DD HH:MM UTC``, or ``unknown time``.
    """
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "unknown time"


def clean_user_text(text: str) -> str:
    """Strip harness wrappers from a user prompt while keeping its full wording.

    Args:
        text: Raw prompt text.

    Returns:
        The prompt without ``<pasted_content>`` tags, system reminders or IDE context blocks.
    """
    text = _SYSTEM_REMINDER.sub("", text)
    text = _IDE_TAGS.sub("", text)
    text = _PASTED_TAG.sub("", text)
    return _BLANK_LINES.sub("\n\n", text).strip()


def one_line(text: str, limit: int = MAX_TOOL_DESCRIPTION) -> str:
    """Collapse text to a single Markdown-safe line, truncated to ``limit`` characters.

    Args:
        text: Any text.
        limit: Maximum length of the result.

    Returns:
        The first non-empty line, with backticks and pipes neutralised.
    """
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    line = lines[0] if lines else ""
    if len(lines) > 1:
        line += " …"
    line = line.replace("`", "'").replace("|", "\\|")
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _relative(path: str) -> str:
    """Show a path relative to the project root, or with the home folder written as ``~``."""
    candidate = Path(path)
    for base, prefix in ((PROJECT_ROOT, ""), (Path.home(), "~/")):
        if candidate.is_relative_to(base):
            return prefix + str(candidate.relative_to(base))
    return path


def describe_tool_call(name: str, tool_input: dict[str, Any]) -> str:
    """Summarise one tool call as a short description.

    Args:
        name: Tool name, e.g. ``Bash``.
        tool_input: The tool's input arguments.

    Returns:
        A one-line description (no tool output).
    """
    if name == "Bash":
        text = tool_input.get("description") or tool_input.get("command", "")
    elif name in {"Read", "Write", "Edit", "NotebookEdit"}:
        text = _relative(str(tool_input.get("file_path") or tool_input.get("notebook_path", "")))
    elif name in {"Grep", "Glob"}:
        text = str(tool_input.get("pattern", ""))
    elif name in {"Agent", "Task"}:
        text = str(tool_input.get("description", ""))
    elif name == "Skill":
        text = str(tool_input.get("skill", ""))
    elif name == "AskUserQuestion":
        count = len(tool_input.get("questions", []))
        text = f"{count} question(s) for the user"
    else:
        text = next((str(v) for v in tool_input.values() if isinstance(v, str)), "")
    return one_line(text)


def _answers_markdown(tool_result: Any) -> list[str]:
    """Render the user's answers to an AskUserQuestion call as nested bullet lines."""
    if not isinstance(tool_result, dict):
        return []
    answers = tool_result.get("answers") or {}
    return [
        f"  - Q: {one_line(str(q), 400)} → **{one_line(str(a), 200)}**" for q, a in answers.items()
    ]


def _text_blocks(content: Any) -> list[str]:
    """Return the text blocks of a message's content (a string or a list of blocks)."""
    if isinstance(content, str):
        return [content]
    return [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]


def parse_session(path: Path) -> Session:
    """Parse one Claude Code ``.jsonl`` transcript into ordered Markdown entries.

    Args:
        path: Path to the session file.

    Returns:
        The parsed session. Malformed lines are skipped with a warning.
    """
    session = Session(session_id=path.stem)
    timestamps: list[datetime] = []
    seen: set[str] = set()
    pending_questions: dict[str, int] = {}  # tool_use_id -> entry index

    with path.open(encoding="utf-8") as handle:
        records = []
        for number, line in enumerate(handle, start=1):
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning("%s:%d: skipped malformed line", path.name, number)

    for record in records:
        ts = parse_timestamp(record.get("timestamp"))
        if ts:
            timestamps.append(ts)
        kind = record.get("type")
        if kind == "ai-title" and record.get("aiTitle"):
            session.title = str(record["aiTitle"])
        if kind not in {"user", "assistant"} or record.get("isSidechain"):
            continue
        uuid = record.get("uuid")
        if uuid in seen:
            continue
        seen.add(uuid)
        content = record.get("message", {}).get("content", [])

        if kind == "assistant":
            for block in content if isinstance(content, list) else []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    session.entries.append(("assistant", ts, block["text"].strip()))
                    session.replies += 1
                elif block.get("type") == "tool_use":
                    name = block.get("name", "tool")
                    desc = describe_tool_call(name, block.get("input") or {})
                    session.entries.append(("tool", ts, f"- **{name}** — {desc}"))
                    session.tool_calls += 1
                    if name == "AskUserQuestion":
                        pending_questions[block.get("id", "")] = len(session.entries) - 1
            continue

        # User records: compaction summaries, skill bodies, task notifications, prompts.
        if record.get("isCompactSummary"):
            session.entries.append(
                (
                    "note",
                    ts,
                    "*Context compacted: the session continued from an automatic summary"
                    " of the earlier conversation (summary not reproduced).*",
                )
            )
            continue
        if record.get("isMeta"):
            skill = next((t for t in _text_blocks(content) if "skill" in t.lower()), "")
            if skill:
                name = Path(skill.split("\n", 1)[0].rsplit(" ", 1)[-1]).name
                session.entries.append(("note", ts, f"*Skill instructions loaded: {name}.*"))
            continue
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    index = pending_questions.pop(block.get("tool_use_id", ""), None)
                    if index is not None:
                        role, when, text = session.entries[index]
                        lines = _answers_markdown(record.get("toolUseResult"))
                        session.entries[index] = (role, when, "\n".join([text, *lines]))
        for raw in _text_blocks(content):
            if raw.lstrip().startswith("<task-notification>"):
                match = _TASK_SUMMARY.search(raw)
                summary = one_line(match.group(1) if match else "background task finished", 300)
                session.entries.append(("note", ts, f"*Background task notification: {summary}*"))
                continue
            text = clean_user_text(raw)
            if text:
                session.entries.append(("user", ts, text))
                session.prompts += 1

    if timestamps:
        session.started, session.ended = min(timestamps), max(timestamps)
    return session


def session_label(session: Session) -> str:
    """Derive the file-name label of a session: ``phase-<n>`` or a slug of its title.

    The phase is the first ``Phase <n>`` in the session's first prompt.

    Args:
        session: A parsed session.

    Returns:
        A lower-case, file-name-safe label.
    """
    first_prompt = next((text for role, _, text in session.entries if role == "user"), "")
    match = _PHASE.search(first_prompt)
    if match:
        return f"phase-{int(match.group(1))}"
    slug = re.sub(r"[^a-z0-9]+", "-", session.title.lower()).strip("-")
    return slug[:60].rstrip("-") or "session"


def render_session(session: Session, label: str) -> str:
    """Render a parsed session as Markdown.

    Args:
        session: A parsed session.
        label: Its file-name label (see :func:`session_label`).

    Returns:
        The Markdown document, ending with a newline.
    """
    heading = label.replace("phase-", "Phase ") if label.startswith("phase-") else session.title
    lines = [
        f"# {heading or session.session_id} — Claude Code session",
        "",
        f"- **Session ID:** `{session.session_id}`",
        f"- **Title:** {session.title or '—'}",
        f"- **Started:** {format_timestamp(session.started)}",
        f"- **Ended:** {format_timestamp(session.ended)}",
        f"- **User prompts:** {session.prompts} · **Assistant replies:** {session.replies}"
        f" · **Tool calls:** {session.tool_calls}",
        "",
        "> Generated by `scripts/export_prompt_history.py` — do not edit by hand. "
        "Prompts and replies are verbatim; each tool call is summarised in one line "
        "(outputs omitted).",
        "",
    ]
    previous = None
    for role, ts, text in session.entries:
        if previous == "tool" and role != "tool":
            lines.append("")  # close the bullet list of tool calls
        if role == "user":
            lines += ["---", "", f"## User · {format_timestamp(ts)}", "", text, ""]
        elif role in {"assistant", "tool"}:
            if previous not in {"assistant", "tool"}:
                lines += [f"## Claude · {format_timestamp(ts)}", ""]
            lines += [text, ""] if role == "assistant" else [text]
        else:
            lines += [text, ""]
        previous = role
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).rstrip() + "\n"


def scan_for_secrets(session_id: str, markdown: str) -> list[SecretFinding]:
    """Scan rendered Markdown for anything that looks like a credential.

    Args:
        session_id: Session the text belongs to (for reporting).
        markdown: The rendered text.

    Returns:
        One finding per match, with the matched value redacted.
    """
    findings = []
    for number, line in enumerate(markdown.splitlines(), start=1):
        for kind, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(0)
                preview = value[:4] + "*" * max(len(value) - 4, 0)
                findings.append(SecretFinding(session_id, number, kind, preview))
    return findings


def build_outputs(sessions: list[Session]) -> dict[str, str]:
    """Render every session and the index, keyed by output file name.

    Args:
        sessions: Parsed sessions, in any order.

    Returns:
        ``{file name: markdown}``, sessions in chronological order, plus ``INDEX.md``.
    """
    ordered = sorted(sessions, key=lambda s: (s.started is None, s.started, s.session_id))
    outputs: dict[str, str] = {}
    rows = []
    for session in ordered:
        if not session.entries:
            logger.info("Skipping empty session %s", session.session_id)
            continue
        label = session_label(session)
        date = session.started.strftime("%Y-%m-%d") if session.started else "undated"
        name, suffix = f"{date}_{label}.md", 2
        while name in outputs:
            name, suffix = f"{date}_{label}-{suffix}.md", suffix + 1
        outputs[name] = render_session(session, label)
        rows.append(
            f"| [{name}]({name}) | `{session.session_id}` | {format_timestamp(session.started)} "
            f"| {format_timestamp(session.ended)} | {session.prompts} | {session.replies} "
            f"| {session.tool_calls} | {one_line(session.title, 80) or '—'} |"
        )
    outputs["INDEX.md"] = "\n".join(
        [
            "# Claude Code sessions — index",
            "",
            "> Generated by `scripts/export_prompt_history.py` — do not edit by hand.",
            "",
            "| File | Session | Started | Ended | Prompts | Replies | Tool calls | Title |",
            "|---|---|---|---|---:|---:|---:|---|",
            *rows,
            "",
        ]
    )
    return outputs


def export(projects_dir: Path, output_dir: Path, exclude: set[str] | None = None) -> list[Path]:
    """Convert every session in ``projects_dir`` and write the Markdown files.

    Nothing is written if any rendered file contains a suspected secret.

    Args:
        projects_dir: Folder holding the ``*.jsonl`` transcripts.
        output_dir: Destination folder (created if missing).
        exclude: Session IDs to leave out.

    Returns:
        The written file paths, in write order.

    Raises:
        FileNotFoundError: If ``projects_dir`` does not exist or holds no transcripts.
        SecretsFoundError: If the secret scan found anything (nothing has been written).
    """
    files = sorted(p for p in projects_dir.glob("*.jsonl") if p.stem not in (exclude or set()))
    if not files:
        raise FileNotFoundError(f"no *.jsonl session files in {projects_dir}")
    outputs = build_outputs([parse_session(path) for path in files])

    findings = [f for name, text in outputs.items() for f in scan_for_secrets(name, text)]
    if findings:
        for finding in findings:
            logger.error(
                "Possible secret: %s line %d — %s: %s",
                finding.session_id,
                finding.line,
                finding.kind,
                finding.preview,
            )
        raise SecretsFoundError(f"{len(findings)} possible secret(s) found; nothing written")

    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in outputs.items():
        target = output_dir / name
        target.write_text(text, encoding="utf-8")
        written.append(target)
        logger.info("Wrote %s (%d bytes)", target, target.stat().st_size)
    return written


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point.

    Args:
        argv: Arguments (default: ``sys.argv[1:]``).

    Returns:
        Process exit code: 0 on success, 1 if secrets were found or no transcripts exist.
    """
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--projects-dir", type=Path, default=default_projects_dir())
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--exclude", nargs="*", default=[], help="session IDs to leave out")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        export(args.projects_dir, args.output_dir, set(args.exclude))
    except (FileNotFoundError, SecretsFoundError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
