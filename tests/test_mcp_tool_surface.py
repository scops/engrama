"""Guards on what MCP clients see: tool descriptions and input schemas.

Tool descriptions come from the handler docstrings and ship verbatim to every
client, so they must not carry internal development references (spec and
requirement ids, design-record numbers, CI jargon). Input schemas must not
contain empty sub-schemas: ``{}`` is legal JSON Schema but constrains nothing,
and some MCP clients refuse or mishandle it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from engrama.adapters.mcp.server import _ADMIN_TOOLS, create_engrama_mcp

pytestmark = pytest.mark.asyncio

_INTERNAL_REF = re.compile(
    r"\b(?:N?FR-\d+|US-\d+|DDR-\d+|BUG-\d+|Spec \d+)\b|scope-exempt|CI-allowlist"
)

# Keywords whose value is a single sub-schema, or a map of them.
_SUBSCHEMA_KEYS = ("items", "additionalProperties", "not")
_SUBSCHEMA_MAPS = ("properties", "$defs", "definitions", "patternProperties")
_SUBSCHEMA_LISTS = ("anyOf", "oneOf", "allOf", "prefixItems")


@pytest.fixture()
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    monkeypatch.delenv("VAULT_PATH", raising=False)
    return create_engrama_mcp(
        backend="sqlite",
        config={"ENGRAMA_DB_PATH": str(tmp_path / "engrama.db")},
        vault_path=None,
    )


def _empty_subschemas(schema: Any, path: str = "") -> list[str]:
    """Paths of sub-schemas that are ``{}`` (accept anything)."""
    found: list[str] = []
    if not isinstance(schema, dict):
        return found
    for key in _SUBSCHEMA_KEYS:
        sub = schema.get(key)
        if sub == {}:
            found.append(f"{path}.{key}")
        found += _empty_subschemas(sub, f"{path}.{key}")
    for key in _SUBSCHEMA_MAPS:
        for name, sub in (schema.get(key) or {}).items():
            if sub == {}:
                found.append(f"{path}.{key}.{name}")
            found += _empty_subschemas(sub, f"{path}.{key}.{name}")
    for key in _SUBSCHEMA_LISTS:
        for i, sub in enumerate(schema.get(key) or []):
            if sub == {}:
                found.append(f"{path}.{key}[{i}]")
            found += _empty_subschemas(sub, f"{path}.{key}[{i}]")
    return found


async def test_tool_descriptions_carry_no_internal_references(server) -> None:
    tools = await server.list_tools()
    assert tools
    leaks = {
        t.name: sorted(
            set(_INTERNAL_REF.findall(json.dumps({"d": t.description, "s": t.input_schema})))
        )
        for t in tools
    }
    assert not {k: v for k, v in leaks.items() if v}


async def test_admin_tool_reasons_carry_no_internal_references() -> None:
    for tool in _ADMIN_TOOLS:
        assert not _INTERNAL_REF.search(tool["reason"]), tool


async def test_input_schemas_have_no_empty_subschemas(server) -> None:
    tools = await server.list_tools()
    empty = {t.name: _empty_subschemas(t.input_schema) for t in tools}
    assert not {k: v for k, v in empty.items() if v}


async def test_remember_relation_targets_are_string_or_object(server) -> None:
    tools = {t.name: t for t in await server.list_tools()}
    schema = tools["engrama_remember"].input_schema
    relations = schema["$defs"]["RememberInput"]["properties"]["relations"]
    items = relations["additionalProperties"]["items"]
    assert {s.get("type") for s in items["anyOf"]} == {"string", "object"}
