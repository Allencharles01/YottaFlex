"""The hash-pinned lockfiles (BACKLOG #4/#6): complete, consistent with pyproject.toml, and
equal to the environment the tests run in."""

import re
import tomllib
from importlib import metadata

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from coldchain.config import PROJECT_ROOT

RUNTIME_LOCK = PROJECT_ROOT / "requirements.lock"
DEV_LOCK = PROJECT_ROOT / "requirements-dev.lock"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^ ;\\]+)\s*(?:;\s*([^\\]+?))?\s*\\?$")


def _parse(path) -> dict[str, tuple[str, str | None, int]]:
    """Package → (version, marker, number of hashes)."""
    pins: dict[str, tuple[str, str | None, int]] = {}
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if match := PIN.match(line):
            current = canonicalize_name(match.group(1))
            pins[current] = (match.group(2), match.group(3), 0)
        elif line.strip().startswith("--hash=sha256:") and current is not None:
            version, marker, hashes = pins[current]
            pins[current] = (version, marker, hashes + 1)
    return pins


def _project() -> dict:
    return tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]


RUNTIME = _parse(RUNTIME_LOCK)
DEV = _parse(DEV_LOCK)


@pytest.mark.parametrize("lock", [RUNTIME, DEV], ids=["runtime", "dev"])
def test_every_pin_carries_hashes(lock) -> None:
    assert lock
    assert all(hashes >= 1 for _, _, hashes in lock.values())


@pytest.mark.parametrize(
    ("lock", "requirements"),
    [
        (RUNTIME, _project()["dependencies"]),
        (
            DEV,
            _project()["dependencies"]
            + _project()["optional-dependencies"]["dev"]
            + _project()["optional-dependencies"]["dashboard"],
        ),
    ],
    ids=["runtime", "dev"],
)
def test_every_declared_dependency_is_pinned_within_its_range(lock, requirements) -> None:
    for text in requirements:
        requirement = Requirement(text)
        name = canonicalize_name(requirement.name)
        assert name in lock, f"{name} is not pinned"
        assert requirement.specifier.contains(lock[name][0], prereleases=True), (name, lock[name])


def test_duckdb_is_pinned() -> None:
    assert RUNTIME["duckdb"][0] == DEV["duckdb"][0] == metadata.version("duckdb")


def test_runtime_lock_is_a_subset_of_the_dev_lock() -> None:
    for name, (version, _, _) in RUNTIME.items():
        assert DEV[name][0] == version, name


def test_the_test_environment_matches_the_dev_lock() -> None:
    mismatched = {}
    for name, (version, marker, _) in DEV.items():
        if marker and not Marker(marker).evaluate():
            continue
        try:
            installed = metadata.version(name)
        except metadata.PackageNotFoundError:
            installed = None
        if installed != version:
            mismatched[name] = (version, installed)
    assert not mismatched, f"locked vs installed: {mismatched} (run scripts/lock_dependencies.sh)"
