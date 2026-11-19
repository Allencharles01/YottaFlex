"""The data dictionary and schemas.py must describe exactly the same tables and fields."""

import re
from pathlib import Path

import pytest

from coldchain import schemas
from coldchain.config import PROJECT_ROOT
from coldchain.enums import TableName

DICTIONARY = PROJECT_ROOT / "docs" / "data_dictionary.md"
HEADING = re.compile(r"^### `(\w+)`\s*$")
ROW = re.compile(r"^\| `(\w+)` \| (\w+) \| (yes|no) \|")
LAYER = re.compile(r"\*\*Layer:\*\* (\w+)")
PRIMARY_KEY = re.compile(r"\*\*Primary key:\*\* ([^·]+)")


def _parse(path: Path) -> dict[str, dict]:
    tables: dict[str, dict] = {}
    current: dict | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if heading := HEADING.match(line):
            current = tables.setdefault(heading.group(1), {"fields": [], "layer": None, "pk": None})
        elif line.startswith("## "):
            current = None
        elif current is not None:
            if (layer := LAYER.search(line)) and (pk := PRIMARY_KEY.search(line)):
                current["layer"] = layer.group(1)
                current["pk"] = tuple(re.findall(r"`(\w+)`", pk.group(1)))
            elif row := ROW.match(line):
                current["fields"].append(row.groups())
    return tables


def _type_word(dtype: object) -> str:
    text = str(dtype)
    if text.startswith(("string", "str")):
        return "string"
    if text.lower() == "int64":
        return "integer"
    if text == "float64":
        return "float"
    if text.startswith("datetime64"):
        return "timestamp"
    if text == "date":
        return "date"
    raise AssertionError(f"unmapped dtype {text}")


DOCUMENTED = _parse(DICTIONARY)


def test_dictionary_documents_every_table() -> None:
    assert set(DOCUMENTED) == {t.value for t in TableName}


@pytest.mark.parametrize("table", list(TableName))
def test_fields_match_schema(table: TableName) -> None:
    documented = DOCUMENTED[table.value]
    schema = schemas.SCHEMAS[table]
    expected = [
        (name, _type_word(col.dtype), "no" if col.nullable else "yes")
        for name, col in schema.columns.items()
    ]
    assert documented["fields"] == expected


@pytest.mark.parametrize("table", list(TableName))
def test_layer_and_primary_key_match_schema(table: TableName) -> None:
    documented = DOCUMENTED[table.value]
    assert documented["layer"] == schemas.TABLE_LAYERS[table].value
    assert documented["pk"] == schemas.PRIMARY_KEYS[table]
