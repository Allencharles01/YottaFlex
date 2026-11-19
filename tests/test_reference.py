import shutil
from datetime import date
from pathlib import Path

import pytest
import yaml

from coldchain.config import (
    DEFAULT_REFERENCE_DIR,
    REFERENCE_FILES,
    load_config,
    load_reference,
)
from coldchain.enums import PolicyCode
from coldchain.errors import ConfigError


@pytest.fixture
def ref_dir(tmp_path: Path) -> Path:
    """A writable copy of the reference directory."""
    target = tmp_path / "reference"
    shutil.copytree(DEFAULT_REFERENCE_DIR, target)
    return target


def _edit(ref_dir: Path, key: str, mutate) -> None:
    path = ref_dir / REFERENCE_FILES[key]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(data[key])
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_default_reference_loads() -> None:
    ref = load_reference()
    assert {p.policy_code for p in ref.policies} == set(PolicyCode)
    assert len(ref.products) >= 10
    assert len(ref.locations) >= 10


def test_refrig_has_v1_and_v2_back_to_back() -> None:
    refrig = sorted(
        (p for p in load_reference().policies if p.policy_code is PolicyCode.REFRIG_2_8),
        key=lambda p: p.version,
    )
    v1, v2 = refrig
    assert (v1.version, v2.version) == (1, 2)
    assert v1.effective_from == date(2026, 1, 1)
    assert v1.effective_to == v2.effective_from == date(2026, 7, 1)
    assert v2.effective_to is None
    assert (v1.max_single_excursion_min, v2.max_single_excursion_min) == (30, 20)


def test_policy_limits_match_plan_table() -> None:
    expected = {
        PolicyCode.REFRIG_2_8: (2, 8, 0, 12, 30, 60),
        PolicyCode.COOL_8_15: (8, 15, 5, 20, 20, 60),
        PolicyCode.FROZEN: (-24, -18, -30, -15, 30, 60),
        PolicyCode.AMBIENT_15_25: (15, 25, 10, 30, 60, 120),
    }
    for p in load_reference().policies:
        assert p.max_reading_gap_min == 30
        assert p.min_coverage_pct == 95
        if p.version == 1:
            limits = (p.min_c, p.max_c, p.abs_min_c, p.abs_max_c)
            allowances = (p.max_single_excursion_min, p.max_cumulative_excursion_min)
            assert (*limits, *allowances) == expected[p.policy_code]


def test_every_product_code_has_packaging_for_the_longest_trip() -> None:
    ref = load_reference()
    longest = load_config().trip_duration_hours.max
    for code in {p.policy_code for p in ref.products}:
        holds = [k.max_hold_hours for k in ref.packaging if k.supported_policy_code is code]
        assert max(holds) >= longest, code


def test_overlapping_policy_versions_rejected(ref_dir: Path) -> None:
    def overlap(policies: list) -> None:
        policies[0]["effective_to"] = date(2026, 7, 2)

    _edit(ref_dir, "policies", overlap)
    with pytest.raises(ConfigError, match="overlap"):
        load_reference(ref_dir)


def test_duplicate_policy_version_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "policies", lambda ps: ps.__setitem__(1, {**ps[1], "version": 1}))
    with pytest.raises(ConfigError, match="duplicate"):
        load_reference(ref_dir)


def test_missing_policy_code_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "policies", lambda ps: ps.pop())
    with pytest.raises(ConfigError, match="no policy defined"):
        load_reference(ref_dir)


def test_policy_limit_order_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "policies", lambda ps: ps[0].update(min_c=9.0))
    with pytest.raises(ConfigError, match="abs_min_c < min_c"):
        load_reference(ref_dir)


def test_packaging_setpoint_outside_band_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "packaging", lambda ps: ps[0].update(setpoint_c=9.0))
    with pytest.raises(ConfigError, match="outside"):
        load_reference(ref_dir)


def test_product_without_packaging_rejected(ref_dir: Path) -> None:
    def drop_frozen(profiles: list) -> None:
        profiles[:] = [p for p in profiles if p["supported_policy_code"] != "FROZEN"]

    _edit(ref_dir, "packaging", drop_frozen)
    with pytest.raises(ConfigError, match="no packaging supports"):
        load_reference(ref_dir)


def test_duplicate_product_name_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "products", lambda ps: ps.append(dict(ps[0])))
    with pytest.raises(ConfigError, match="duplicate product names"):
        load_reference(ref_dir)


def test_unknown_key_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "products", lambda ps: ps[0].update(colour="blue"))
    with pytest.raises(ConfigError, match="colour"):
        load_reference(ref_dir)


def test_location_outside_india_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "locations", lambda ls: ls[0].update(latitude=51.5))
    with pytest.raises(ConfigError, match="latitude"):
        load_reference(ref_dir)


def test_no_hub_rejected(ref_dir: Path) -> None:
    _edit(ref_dir, "locations", lambda ls: [loc.update(is_hub=False) for loc in ls])
    with pytest.raises(ConfigError, match="hub"):
        load_reference(ref_dir)


def test_missing_file_rejected(ref_dir: Path) -> None:
    (ref_dir / REFERENCE_FILES["locations"]).unlink()
    with pytest.raises(ConfigError, match="not found"):
        load_reference(ref_dir)


def test_wrong_top_level_key_rejected(ref_dir: Path) -> None:
    (ref_dir / REFERENCE_FILES["products"]).write_text("items: []\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="single top-level key 'products'"):
        load_reference(ref_dir)


def test_bad_yaml_rejected(ref_dir: Path) -> None:
    (ref_dir / REFERENCE_FILES["packaging"]).write_text("packaging: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Could not parse YAML"):
        load_reference(ref_dir)


@pytest.mark.parametrize(
    ("key", "index", "changes", "message"),
    [
        (
            "policies",
            0,
            {"max_cumulative_excursion_min": 10},
            "max_cumulative_excursion_min must be",
        ),
        ("policies", 0, {"effective_to": date(2026, 1, 1)}, "effective_to must be after"),
        ("products", 0, {"lot_size_units": {"min": 10, "max": 5}}, "must be >= min"),
    ],
)
def test_row_level_rules_rejected(ref_dir: Path, key, index, changes, message) -> None:
    _edit(ref_dir, key, lambda rows: rows[index].update(changes))
    with pytest.raises(ConfigError, match=message):
        load_reference(ref_dir)


def test_no_manufacturing_cluster_rejected(ref_dir: Path) -> None:
    _edit(
        ref_dir, "locations", lambda ls: [loc.update(is_manufacturing_cluster=False) for loc in ls]
    )
    with pytest.raises(ConfigError, match="manufacturing cluster"):
        load_reference(ref_dir)
