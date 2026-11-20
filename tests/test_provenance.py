"""Stage provenance: command lines, config fingerprints, git state and chain verification."""

import hashlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from coldchain import provenance
from coldchain.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, REFERENCE_FILES
from coldchain.enums import PipelineStage, Scale
from coldchain.errors import ProvenanceError
from coldchain.io import file_sha256


def _option(name: str, opts: list[str], *, flag: bool = False, secondary: list[str] | None = None):
    return SimpleNamespace(name=name, opts=opts, is_flag=flag, secondary_opts=secondary or [])


def test_command_line_spells_out_effective_options() -> None:
    params = [
        _option("scale", ["--scale"]),
        _option("seed", ["--seed"]),
        _option("config_path", ["--config"]),
        _option("output_dir", ["--output-dir"]),
        _option("verify_sql", ["--verify-sql"], flag=True, secondary=["--no-verify-sql"]),
    ]
    values = {
        "scale": Scale.FULL,
        "seed": None,
        "config_path": DEFAULT_CONFIG_PATH,
        "output_dir": Path("/tmp/my run"),
        "verify_sql": False,
    }
    line = provenance.command_line("export", params, values)
    assert line == (
        "coldchain export --scale full --config config/default.yaml "
        "--output-dir '/tmp/my run' --no-verify-sql"
    )


def test_display_path_is_relative_inside_the_project() -> None:
    assert provenance.display_path(PROJECT_ROOT / "output") == "output"
    assert provenance.display_path(Path("/somewhere/else")) == "/somewhere/else"


def test_combined_sha256_is_order_independent_and_sha256sum_compatible() -> None:
    digests = {"b.csv": "2" * 64, "a.csv": "1" * 64}
    expected = hashlib.sha256(f"{'1' * 64}  a.csv\n{'2' * 64}  b.csv\n".encode()).hexdigest()
    assert provenance.combined_sha256(digests) == expected
    assert provenance.combined_sha256(dict(reversed(digests.items()))) == expected


def _fingerprint(config, reference_dir=None) -> dict:
    if reference_dir is None:
        return provenance.config_fingerprint(config, DEFAULT_CONFIG_PATH, file_sha256)
    return provenance.config_fingerprint(config, DEFAULT_CONFIG_PATH, file_sha256, reference_dir)


def test_config_fingerprint_covers_settings_and_every_reference_file(app_config) -> None:
    fingerprint = _fingerprint(app_config)
    assert fingerprint["path"] == "config/default.yaml"
    assert set(fingerprint["files"]) == {"pipeline_settings"} | {
        f"reference/{name}" for name in REFERENCE_FILES.values()
    }
    assert fingerprint["sha256"] == provenance.combined_sha256(fingerprint["files"])


def test_export_settings_and_seed_do_not_change_the_fingerprint(app_config) -> None:
    """DECISIONS.md #108: only settings that can change the pipeline's data count."""
    base = _fingerprint(app_config)["sha256"]
    export = app_config.export.model_copy(update={"sql_batch_rows": 7, "sql_gzip_threshold_mb": 0})
    assert _fingerprint(app_config.model_copy(update={"export": export}))["sha256"] == base
    assert _fingerprint(app_config.model_copy(update={"seed": 1}))["sha256"] == base


def test_any_other_config_section_changes_the_fingerprint(app_config) -> None:
    base = _fingerprint(app_config)["sha256"]
    evaluation = app_config.evaluation.model_copy(update={"strict_scales": ()})
    changes = [
        {"long_dwell_threshold_min": app_config.long_dwell_threshold_min + 1},
        {"reading_interval_min": 5},
        {"evaluation": evaluation},
    ]
    for update in changes:
        assert _fingerprint(app_config.model_copy(update=update))["sha256"] != base, update


def test_yaml_comments_and_formatting_do_not_change_the_fingerprint(tmp_path: Path) -> None:
    from coldchain.config import load_config

    text = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")
    edited = tmp_path / "edited.yaml"
    edited.write_text("# a new comment\n" + text.replace("seed: 20261022", "seed:   20261022"))
    assert _fingerprint(load_config(edited))["sha256"] == _fingerprint(load_config())["sha256"]


def test_config_fingerprint_changes_with_a_reference_file(tmp_path: Path, app_config) -> None:
    for name in REFERENCE_FILES.values():
        (tmp_path / name).write_text("x: 1\n", encoding="utf-8")
    before = _fingerprint(app_config, tmp_path)
    (tmp_path / "packaging.yaml").write_text("x: 2\n", encoding="utf-8")
    after = _fingerprint(app_config, tmp_path)
    assert before["sha256"] != after["sha256"]


def test_export_settings_have_their_own_hash(app_config) -> None:
    settings = provenance.export_settings(app_config)
    assert settings["values"]["sql_batch_rows"] == 500
    assert settings["sha256"] == provenance.settings_sha256(settings["values"])
    other = app_config.model_copy(
        update={"export": app_config.export.model_copy(update={"sql_batch_rows": 7})}
    )
    assert provenance.export_settings(other)["sha256"] != settings["sha256"]


def test_git_state_of_the_project() -> None:
    state = provenance.git_state()
    head = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True
    )
    assert state.commit == head.stdout.strip() and len(state.commit) == 40
    assert isinstance(state.dirty, bool)


def test_git_state_outside_a_repository(tmp_path: Path) -> None:
    assert provenance.git_state(tmp_path) == provenance.GitState(None, None)


def test_git_state_detects_a_dirty_tree(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / "f.txt").write_text("a", encoding="utf-8")
    git("add", "f.txt")
    git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "init")
    assert provenance.git_state(tmp_path).dirty is False
    (tmp_path / "f.txt").write_text("b", encoding="utf-8")
    assert provenance.git_state(tmp_path).dirty is True


def test_stage_record_is_sorted_and_has_no_wall_clock_time() -> None:
    record = provenance.stage_record(
        PipelineStage.CHECK,
        command="coldchain check",
        scale="starter",
        seed=None,
        config={"sha256": "c"},
        git=provenance.GitState("abc", False),
        inputs={"b": "2", "a": "1"},
        outputs={"z": "9"},
    )
    assert list(record["inputs"]) == ["a", "b"]
    assert record["git"] == {"commit": "abc", "dirty": False}
    assert not any("time" in key or key.endswith("_at") for key in record)


# --------------------------------------------------------------------------------------------
# verify_chain
# --------------------------------------------------------------------------------------------


def _rec(stage: str, inputs: dict, outputs: dict, config: str = "cfg") -> dict:
    return {
        "stage": stage,
        "scale": "starter",
        "config": {"sha256": config},
        "inputs": inputs,
        "outputs": outputs,
    }


@pytest.fixture
def chain() -> tuple[dict, dict]:
    files = {"raw/a": "a2", "gt/e": "e2", "base/a": "a1", "comp/c": "c1", "val/s": "s1"}
    records = {
        PipelineStage.GENERATE: _rec("generate", {}, {"raw/a": "a1", "gt/e": "e1"}),
        PipelineStage.INJECT: _rec("inject", {"base/a": "a1"}, {"raw/a": "a2", "gt/e": "e2"}),
        PipelineStage.CHECK: _rec("check", {"raw/a": "a2"}, {"comp/c": "c1"}),
        PipelineStage.VALIDATE: _rec("validate", {"raw/a": "a2", "comp/c": "c1"}, {"val/s": "s1"}),
    }
    return records, files


def _verify(records: dict, files: dict, required=("raw/a", "comp/c", "val/s")) -> None:
    provenance.verify_chain(
        records, files.get, scale="starter", config_sha256="cfg", required=required
    )


def test_a_consistent_chain_passes(chain) -> None:
    records, files = chain
    _verify(records, files)  # generate's raw/a output was rewritten by inject: that is fine


def test_a_stage_whose_input_changed_is_stale(chain) -> None:
    records, files = chain
    files["raw/a"] = "a3"
    with pytest.raises(ProvenanceError, match="check: input raw/a changed"):
        _verify(records, files)


def test_a_modified_output_is_reported(chain) -> None:
    records, files = chain
    files["comp/c"] = "tampered"
    with pytest.raises(ProvenanceError, match="check: output comp/c was modified"):
        _verify(records, files)


def test_a_removed_output_is_reported(chain) -> None:
    records, files = chain
    del files["val/s"]
    with pytest.raises(ProvenanceError, match="validate: output val/s was modified or removed"):
        _verify(records, files)


def test_a_different_config_is_reported(chain) -> None:
    records, files = chain
    records[PipelineStage.CHECK]["config"]["sha256"] = "other"
    with pytest.raises(ProvenanceError, match="check: ran with a different config"):
        _verify(records, files)


def test_a_record_of_another_scale_is_reported(chain) -> None:
    records, files = chain
    records[PipelineStage.CHECK]["scale"] = "full"
    with pytest.raises(ProvenanceError, match="recorded for scale 'full'"):
        _verify(records, files)


def test_a_required_file_without_a_writer_is_reported(chain) -> None:
    records, files = chain
    files["extra"] = "x"
    with pytest.raises(ProvenanceError, match="extra was not written by any recorded stage"):
        _verify(records, files, required=("raw/a", "extra"))


def test_without_the_inject_record_generate_must_match(chain) -> None:
    records, files = chain
    del records[PipelineStage.INJECT]
    with pytest.raises(ProvenanceError, match="generate: output raw/a was modified"):
        _verify(records, files)


def test_seeded_stages_must_share_one_seed(chain) -> None:
    records, files = chain
    records[PipelineStage.GENERATE]["seed"] = 20261022
    records[PipelineStage.INJECT]["seed"] = 7
    with pytest.raises(
        ProvenanceError, match=r"different seeds \(generate seed 20261022, inject seed 7\)"
    ):
        _verify(records, files)


def test_one_seed_across_the_seeded_stages_passes(chain) -> None:
    records, files = chain
    for stage in (PipelineStage.GENERATE, PipelineStage.INJECT):
        records[stage]["seed"] = 20261022
    records[PipelineStage.CHECK]["seed"] = None  # draws no random numbers
    _verify(records, files)


def test_without_injection_a_single_seeded_stage_needs_no_partner(chain) -> None:
    records, files = chain
    del records[PipelineStage.INJECT]
    records[PipelineStage.GENERATE]["seed"] = 7
    files["raw/a"], files["gt/e"] = "a1", "e1"
    records[PipelineStage.CHECK]["inputs"] = {"raw/a": "a1"}
    records[PipelineStage.VALIDATE]["inputs"] = {"raw/a": "a1", "comp/c": "c1"}
    _verify(records, files)
