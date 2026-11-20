"""Security checks (BACKLOG #3): safe YAML loading and fictional names only.

* YAML is only ever parsed with ``yaml.safe_load`` (static scan of every Python file, plus a
  live attempt to load a ``!!python/object`` tag, which must be refused);
* every party, product, packaging and device name is generic and fictional: party names follow
  the ``<city> <label> <nn>`` pattern, and no name contains a real brand or company.
"""

import ast
import re
from pathlib import Path

import pytest

from coldchain.config import DEFAULT_REFERENCE_DIR, PROJECT_ROOT, load_config, load_reference
from coldchain.enums import TableName
from coldchain.errors import ConfigError

PYTHON_FILES = sorted(
    path
    for folder in ("src", "scripts", "tests", "app")
    if (PROJECT_ROOT / folder).is_dir()
    for path in (PROJECT_ROOT / folder).rglob("*.py")
)

UNSAFE_YAML = {"load", "load_all", "full_load", "full_load_all", "unsafe_load", "unsafe_load_all"}
UNSAFE_LOADERS = {"Loader", "FullLoader", "UnsafeLoader", "CLoader", "CFullLoader", "CUnsafeLoader"}

REAL_BRANDS = (
    # Pharmaceutical and vaccine makers (global and Indian).
    "Pfizer",
    "BioNTech",
    "Moderna",
    "AstraZeneca",
    "GSK",
    "GlaxoSmithKline",
    "Novartis",
    "Sanofi",
    "Merck",
    "MSD",
    "Johnson & Johnson",
    "Janssen",
    "Roche",
    "Genentech",
    "Abbott",
    "AbbVie",
    "Bayer",
    "Lilly",
    "Novo Nordisk",
    "Takeda",
    "Amgen",
    "Gilead",
    "Biogen",
    "Teva",
    "Viatris",
    "Mylan",
    "Serum Institute",
    "Bharat Biotech",
    "Biological E",
    "Cipla",
    "Sun Pharma",
    "Dr. Reddy",
    "Dr Reddy",
    "Lupin",
    "Zydus",
    "Cadila",
    "Biocon",
    "Glenmark",
    "Aurobindo",
    "Torrent",
    "Alkem",
    "Mankind",
    "Intas",
    "Wockhardt",
    "Panacea Biotec",
    "Covishield",
    "Covaxin",
    "Comirnaty",
    "Spikevax",
    "Lantus",
    "Humira",
    # Logistics, cold chain and logger vendors.
    "DHL",
    "FedEx",
    "UPS",
    "Maersk",
    "Kuehne",
    "DB Schenker",
    "Blue Dart",
    "Delhivery",
    "Gati",
    "Snowman",
    "Coldex",
    "Allcargo",
    "TCI Express",
    "Thermo King",
    "Carrier Transicold",
    "Envirotainer",
    "Sensitech",
    "ELPRO",
    "Testo",
    "Berlinger",
    "Controlant",
    "Tive",
    "Emerson",
    "Cryoport",
    "World Courier",
    "Marken",
    "Cold Chain Technologies",
    "Sonoco",
    "Pelican",
    # Hospital chains and public institutions.
    "Apollo",
    "Fortis",
    "Max Healthcare",
    "Manipal",
    "Narayana",
    "Medanta",
    "AIIMS",
)
"""Real brands and companies that must never appear in a generated or reference name."""


def _brand_hits(name: str) -> list[str]:
    return [
        brand
        for brand in REAL_BRANDS
        if re.search(rf"(?<![A-Za-z]){re.escape(brand)}(?![A-Za-z])", name, flags=re.IGNORECASE)
    ]


# --------------------------------------------------------------------------------------------
# YAML
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", PYTHON_FILES, ids=lambda p: str(p.relative_to(PROJECT_ROOT)))
def test_yaml_is_only_parsed_with_safe_load(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        is_yaml_attribute = (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "yaml"
        )
        if is_yaml_attribute:
            assert node.attr not in UNSAFE_YAML | UNSAFE_LOADERS, (
                f"{path.name}:{node.lineno} uses yaml.{node.attr}"
            )
        if isinstance(node, ast.ImportFrom) and node.module == "yaml":
            names = {alias.name for alias in node.names}
            assert not names & (UNSAFE_YAML | UNSAFE_LOADERS), f"{path.name} imports {names}"


def test_some_yaml_is_actually_read_in_the_package() -> None:
    config_source = (PROJECT_ROOT / "src" / "coldchain" / "config.py").read_text(encoding="utf-8")
    assert "yaml.safe_load(" in config_source


def test_a_python_object_tag_is_refused(tmp_path) -> None:
    marker = tmp_path / "pwned"
    path = tmp_path / "evil.yaml"
    path.write_text(f'seed: !!python/object/apply:os.system ["touch {marker}"]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    assert not marker.exists()


def test_reference_files_hold_plain_data_only() -> None:
    for path in sorted(Path(DEFAULT_REFERENCE_DIR).glob("*.yaml")):
        assert "!!" not in path.read_text(encoding="utf-8"), path.name


# --------------------------------------------------------------------------------------------
# Fictional names
# --------------------------------------------------------------------------------------------


def test_brand_matcher_works() -> None:
    assert _brand_hits("Mumbai Pfizer Depot") == ["Pfizer"]
    assert _brand_hits("dhl express") == ["DHL"]
    assert _brand_hits("Upstream Pharma 01") == []  # "UPS" only as a whole word
    assert _brand_hits("Mumbai Pharma 01") == []


def test_generated_party_names_are_generic(app_config, starter_tables) -> None:
    labels = app_config.generation.party_name_labels
    for row in starter_tables[TableName.PARTIES].itertuples(index=False):
        label = labels[row.party_type]
        assert re.fullmatch(rf"{re.escape(row.city)} {re.escape(label)} \d{{2}}", row.name), row
        assert _brand_hits(row.name) == [], row.name


def test_configured_name_parts_are_not_brands(app_config) -> None:
    parts = [
        *app_config.generation.party_name_labels.values(),
        *app_config.generation.devices.models,
    ]
    for part in parts:
        assert _brand_hits(part) == [], part


def test_reference_and_generated_names_are_not_brands(reference, starter_tables) -> None:
    names = [
        *(p.name for p in reference.products),
        *(p.name for p in reference.packaging),
        *starter_tables[TableName.PRODUCTS]["name"],
        *starter_tables[TableName.PACKAGING]["name"],
        *starter_tables[TableName.DEVICES]["model"],
    ]
    assert names
    for name in names:
        assert _brand_hits(name) == [], name


def test_test_fixture_names_are_not_brands(valid_tables) -> None:
    for table, column in (
        (TableName.PARTIES, "name"),
        (TableName.PRODUCTS, "name"),
        (TableName.PACKAGING, "name"),
    ):
        for name in valid_tables[table][column]:
            assert _brand_hits(name) == [], name


def test_reference_loader_still_validates() -> None:
    assert load_reference().products  # the scan above ran on real, validated reference data
