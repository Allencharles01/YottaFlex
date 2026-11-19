"""The Mermaid ER diagram must cover every table, column and foreign key in schemas.py."""

import re

from coldchain import schemas
from coldchain.config import PROJECT_ROOT
from coldchain.enums import TableName

TEXT = (PROJECT_ROOT / "docs" / "er_diagram.md").read_text(encoding="utf-8")
BLOCK = re.search(r"```mermaid\n(.*?)```", TEXT, re.S).group(1)
LEFT = r"(?:\|\||\|o|\}o|\}\|)"
RIGHT = r"(?:\|\||o\||o\{|\|\{)"
RELATION = re.compile(rf'^\s+(\w+) {LEFT}(?:--|\.\.){RIGHT} (\w+) : "[^"]+"$')
ENTITY_START = re.compile(r"^\s+(\w+) \{$")
KEYS = r"(?: (?:PK|FK|UK)(?:, (?:PK|FK|UK))*)?"
ATTRIBUTE = re.compile(rf'^\s+(string|int|float|date|timestamp) (\w+){KEYS}(?: "[^"]+")?$')


def _parse() -> tuple[set[tuple[str, str]], dict[str, list[str]]]:
    relations: set[tuple[str, str]] = set()
    entities: dict[str, list[str]] = {}
    current: str | None = None
    for line in BLOCK.splitlines()[1:]:  # skip "erDiagram"
        if not line.strip():
            continue
        if current is not None:
            if line.strip() == "}":
                current = None
                continue
            match = ATTRIBUTE.match(line)
            assert match, f"bad attribute line: {line!r}"
            entities[current].append(match.group(2))
        elif start := ENTITY_START.match(line):
            current = start.group(1)
            entities[current] = []
        else:
            match = RELATION.match(line)
            assert match, f"bad relationship line: {line!r}"
            relations.add(frozenset(match.groups()))
    assert current is None, "unclosed entity block"
    return relations, entities


RELATIONS, ENTITIES = _parse()


def test_diagram_starts_with_er_header() -> None:
    assert BLOCK.splitlines()[0] == "erDiagram"


def test_every_table_has_an_entity_with_its_columns() -> None:
    assert set(ENTITIES) == {t.value for t in TableName}
    for table in TableName:
        assert ENTITIES[table.value] == list(schemas.SCHEMAS[table].columns), table


def test_every_foreign_key_is_drawn() -> None:
    for child, _, parent, _ in schemas.FOREIGN_KEYS:
        assert frozenset({child.value, parent.value}) in RELATIONS, (child, parent)


def test_relationships_only_reference_known_entities() -> None:
    for pair in RELATIONS:
        assert pair <= set(ENTITIES), pair
