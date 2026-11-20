"""Tests for scripts/export_prompt_history.py (Claude Code transcript → Markdown)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from coldchain.config import PROJECT_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "export_prompt_history", PROJECT_ROOT / "scripts" / "export_prompt_history.py"
)
assert _SPEC and _SPEC.loader
eph = importlib.util.module_from_spec(_SPEC)
sys.modules["export_prompt_history"] = eph
_SPEC.loader.exec_module(eph)


def _user(text: str, ts: str, uuid: str, **extra: object) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        **extra,
    }


def _assistant(blocks: list[dict], ts: str, uuid: str) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": ts,
        "message": {"role": "assistant", "content": blocks},
    }


def _write_session(folder: Path, session_id: str, records: list[dict]) -> Path:
    path = folder / f"{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def projects_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "projects"
    folder.mkdir()
    _write_session(
        folder,
        "bbbb",
        [
            {"type": "ai-title", "aiTitle": "Phase 2 work"},
            _user(
                '\n<pasted_content id="x">\nProceed with Phase 2.\n</pasted_content id="x">'
                "<system-reminder>hidden</system-reminder>",
                "2026-11-19T12:00:00.000Z",
                "u1",
            ),
            _assistant(
                [
                    {"type": "thinking", "thinking": "secret reasoning"},
                    {"type": "text", "text": "Plan for Phase 2."},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {"command": "pytest -q", "description": "Run the tests"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Read",
                        "input": {"file_path": str(PROJECT_ROOT / "README.md")},
                    },
                ],
                "2026-11-19T12:01:00.000Z",
                "a1",
            ),
            {
                "type": "user",
                "uuid": "r1",
                "timestamp": "2026-11-19T12:02:00.000Z",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "OUTPUT"}]
                },
            },
            _user("Summary of before", "2026-11-19T12:03:00.000Z", "c1", isCompactSummary=True),
            _assistant([{"type": "text", "text": "Done."}], "2026-11-19T12:04:00.000Z", "a2"),
            _assistant([{"type": "text", "text": "Done."}], "2026-11-19T12:04:00.000Z", "a2"),
        ],
    )
    _write_session(
        folder,
        "aaaa",
        [
            {"type": "ai-title", "aiTitle": "Tidy up: docs!"},
            _user("Small cleanup task.", "2026-11-18T09:00:00.000Z", "u1"),
        ],
    )
    return folder


def test_export_writes_one_file_per_session_in_date_order(projects_dir: Path, tmp_path: Path):
    out = tmp_path / "out"
    written = eph.export(projects_dir, out)
    assert [p.name for p in written] == [
        "2026-11-18_tidy-up-docs.md",
        "2026-11-19_phase-2.md",
        "INDEX.md",
    ]
    index = (out / "INDEX.md").read_text(encoding="utf-8")
    assert index.index("tidy-up-docs") < index.index("phase-2")


def test_rendered_session_keeps_prompts_and_replies_and_summarises_tools(
    projects_dir: Path, tmp_path: Path
):
    eph.export(projects_dir, tmp_path / "out")
    text = (tmp_path / "out" / "2026-11-19_phase-2.md").read_text(encoding="utf-8")
    assert "Proceed with Phase 2." in text
    assert "Plan for Phase 2." in text
    assert "- **Bash** — Run the tests" in text
    assert "- **Read** — README.md" in text
    assert text.count("Done.") == 1  # duplicate record (same uuid) rendered once
    assert "Context compacted" in text
    for hidden in ("pasted_content", "system-reminder", "hidden", "secret reasoning", "OUTPUT"):
        assert hidden not in text
    assert "Summary of before" not in text


def test_ask_user_question_answers_are_included(tmp_path: Path):
    folder = tmp_path / "p"
    folder.mkdir()
    _write_session(
        folder,
        "cccc",
        [
            _user("Phase 1 go", "2026-11-19T10:00:00.000Z", "u1"),
            _assistant(
                [
                    {
                        "type": "tool_use",
                        "id": "q1",
                        "name": "AskUserQuestion",
                        "input": {"questions": [{"question": "Which?"}]},
                    }
                ],
                "2026-11-19T10:01:00.000Z",
                "a1",
            ),
            {
                "type": "user",
                "uuid": "r1",
                "timestamp": "2026-11-19T10:02:00.000Z",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "q1"}]},
                "toolUseResult": {"questions": [], "answers": {"Which?": "Option A"}},
            },
        ],
    )
    session = eph.parse_session(folder / "cccc.jsonl")
    text = eph.render_session(session, eph.session_label(session))
    assert "- **AskUserQuestion** — 1 question(s) for the user" in text
    assert "  - Q: Which? → **Option A**" in text


def test_secret_in_a_prompt_blocks_all_writes(projects_dir: Path, tmp_path: Path):
    _write_session(
        projects_dir,
        "dddd",
        [_user("my key is sk-ant-api03-" + "x" * 40, "2026-11-20T08:00:00.000Z", "u1")],
    )
    out = tmp_path / "out"
    with pytest.raises(eph.SecretsFoundError):
        eph.export(projects_dir, out)
    assert not out.exists()
    assert eph.main(["--projects-dir", str(projects_dir), "--output-dir", str(out)]) == 1


@pytest.mark.parametrize(
    "text",
    [
        "ghp_" + "a" * 36,
        "AKIAABCDEFGHIJKLMNOP",
        "-----BEGIN RSA PRIVATE KEY-----",
        "password = hunter2hunter2",
        "postgres://user:s3cretpw@db.example.com/x",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123",
    ],
)
def test_scan_detects_common_secret_formats(text: str):
    findings = eph.scan_for_secrets("s", f"line one\n{text}\n")
    assert findings
    assert findings[0].line == 2
    assert text not in findings[0].preview


@pytest.mark.parametrize(
    "text",
    [
        "seed = 20261022",
        "the token count was high",
        "`--require-hashes` checks sha256 of every file",
        "password: ********",
    ],
)
def test_scan_ignores_ordinary_project_text(text: str):
    assert eph.scan_for_secrets("s", text) == []


def test_empty_or_missing_folder_is_an_error(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        eph.export(tmp_path / "nope", tmp_path / "out")
    assert eph.main(["--projects-dir", str(tmp_path), "--output-dir", str(tmp_path / "o")]) == 1


def test_label_falls_back_to_title_slug_and_handles_name_collisions(tmp_path: Path):
    session = eph.Session(session_id="x", title="")
    assert eph.session_label(session) == "session"
    folder = tmp_path / "p"
    folder.mkdir()
    for sid in ("s1", "s2"):
        _write_session(folder, sid, [_user("Phase 3 again", "2026-11-19T10:00:00.000Z", "u")])
    outputs = eph.build_outputs([eph.parse_session(p) for p in sorted(folder.glob("*.jsonl"))])
    assert {"2026-11-19_phase-3.md", "2026-11-19_phase-3-2.md"} <= set(outputs)


def test_default_projects_dir_matches_claude_code_naming():
    path = eph.default_projects_dir(Path("/home/me/My.Proj"))
    assert path == Path.home() / ".claude" / "projects" / "-home-me-My-Proj"
