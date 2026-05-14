"""Tests for DDR-003 Phase E layer 1 — the Sanitiser.

Covers control-character stripping, length capping, reserved-key removal,
schema whitelist validation, and the engine-level + MCP-level
integrations that apply the sanitiser at every write boundary.
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from engrama.adapters.mcp.server import _with_mcp_provenance
from engrama.core.engine import EngramaEngine
from engrama.core.security import (
    DEFAULT_TRUST_LEVELS,
    MAX_PROPERTY_VALUE_LEN,
    RESERVED_PROVENANCE_KEYS,
    Provenance,
    Sanitiser,
)

# ---------------------------------------------------------------------------
# 1. Sanitiser.sanitise_properties
# ---------------------------------------------------------------------------


class TestSanitiseProperties:
    def setup_method(self):
        self.s = Sanitiser()

    def test_returns_a_new_dict(self):
        src = {"name": "Alice"}
        out = self.s.sanitise_properties(src)
        assert out is not src

    def test_passes_through_clean_input(self):
        out = self.s.sanitise_properties({"name": "Alice", "notes": "Hello world"})
        assert out == {"name": "Alice", "notes": "Hello world"}

    @pytest.mark.parametrize("reserved", sorted(RESERVED_PROVENANCE_KEYS))
    def test_strips_each_reserved_provenance_key(self, reserved):
        out = self.s.sanitise_properties({"name": "X", reserved: "anything"})
        assert reserved not in out

    def test_strips_underscore_prefixed_keys(self):
        out = self.s.sanitise_properties({"name": "X", "_internal": "leak"})
        assert "_internal" not in out
        assert out["name"] == "X"

    def test_strips_control_characters_from_strings(self):
        # Includes a null byte, bell, and DEL — all C0 chars that should
        # disappear; tab and newline should be preserved.
        dirty = "Hello\x00world\x07\tline\nbreak\x7f"
        out = self.s.sanitise_properties({"name": "X", "notes": dirty})
        assert out["notes"] == "Helloworld\tline\nbreak"

    def test_recurses_into_nested_lists(self):
        out = self.s.sanitise_properties({"name": "X", "tags": ["clean", "dirty\x00val"]})
        assert out["tags"] == ["clean", "dirtyval"]

    def test_recurses_into_nested_dicts(self):
        out = self.s.sanitise_properties({"name": "X", "meta": {"author": "alice\x00bob"}})
        assert out["meta"]["author"] == "alicebob"

    def test_truncates_long_strings(self, caplog):
        big = "a" * (MAX_PROPERTY_VALUE_LEN + 500)
        with caplog.at_level(logging.WARNING, logger="engrama.core.security"):
            out = self.s.sanitise_properties({"name": "X", "notes": big})
        assert len(out["notes"]) == MAX_PROPERTY_VALUE_LEN
        assert "truncated" in caplog.text.lower()

    def test_non_string_scalars_untouched(self):
        out = self.s.sanitise_properties(
            {"name": "X", "confidence": 0.7, "valid": True, "count": 42}
        )
        assert out["confidence"] == 0.7
        assert out["valid"] is True
        assert out["count"] == 42

    def test_custom_max_value_len(self):
        s = Sanitiser(max_value_len=10)
        out = s.sanitise_properties({"name": "X", "notes": "a" * 50})
        assert len(out["notes"]) == 10


# ---------------------------------------------------------------------------
# 2. Sanitiser.validate_label / validate_relation
# ---------------------------------------------------------------------------


class TestSchemaWhitelist:
    def test_valid_label_passes(self):
        s = Sanitiser()
        assert s.validate_label("Project") == "Project"
        assert s.validate_label("Concept") == "Concept"

    def test_invalid_label_raises(self):
        s = Sanitiser()
        with pytest.raises(ValueError, match="Unknown node label"):
            s.validate_label("DoesNotExist")

    def test_valid_relation_passes(self):
        s = Sanitiser()
        assert s.validate_relation("USES") == "USES"
        assert s.validate_relation("BELONGS_TO") == "BELONGS_TO"

    def test_invalid_relation_raises(self):
        s = Sanitiser()
        with pytest.raises(ValueError, match="Unknown relation type"):
            s.validate_relation("FROBNICATES")

    def test_custom_whitelists(self):
        s = Sanitiser(valid_labels={"X"}, valid_relations={"Y"})
        assert s.validate_label("X") == "X"
        with pytest.raises(ValueError):
            s.validate_label("Project")
        assert s.validate_relation("Y") == "Y"
        with pytest.raises(ValueError):
            s.validate_relation("USES")


# ---------------------------------------------------------------------------
# 3. Engine integration
# ---------------------------------------------------------------------------


def _stub_store():
    store = MagicMock(spec=["merge_node", "merge_relation"])
    store.merge_node.return_value = [{"n": {"created_at": "x", "updated_at": "x"}}]
    store.merge_relation.return_value = []
    return store


class TestEngineSanitisation:
    def test_merge_node_rejects_unknown_label(self):
        engine = EngramaEngine(_stub_store())
        with pytest.raises(ValueError, match="Unknown node label"):
            engine.merge_node("DoesNotExist", {"name": "X"})

    def test_merge_node_strips_caller_reserved_keys(self):
        store = _stub_store()
        engine = EngramaEngine(store, default_provenance=Provenance(source="sdk"))
        engine.merge_node(
            "Concept",
            {"name": "Async", "source": "fake", "trust_level": 1.0},
        )
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        # The caller's spoofed source/trust_level were stripped by the
        # sanitiser; the engine's sdk provenance is what got persisted.
        assert extra["source"] == "sdk"
        assert extra["trust_level"] == DEFAULT_TRUST_LEVELS["sdk"]

    def test_explicit_provenance_still_overrides(self):
        store = _stub_store()
        engine = EngramaEngine(store, default_provenance=Provenance(source="sdk"))
        engine.merge_node(
            "Concept",
            {"name": "Async"},
            provenance=Provenance(source="sync"),
        )
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["source"] == "sync"

    def test_merge_node_cleans_control_chars_from_values(self):
        store = _stub_store()
        engine = EngramaEngine(store)
        engine.merge_node("Concept", {"name": "Clean", "notes": "dirty\x00value"})
        _, _, _, extra, *_ = store.merge_node.call_args[0]
        assert extra["notes"] == "dirtyvalue"

    def test_merge_relation_rejects_unknown_rel_type(self):
        engine = EngramaEngine(_stub_store())
        with pytest.raises(ValueError, match="Unknown relation type"):
            engine.merge_relation("a", "Project", "FROBNICATES", "b", "Technology")

    def test_merge_relation_rejects_unknown_from_label(self):
        engine = EngramaEngine(_stub_store())
        with pytest.raises(ValueError, match="Unknown node label"):
            engine.merge_relation("a", "BogusLabel", "USES", "b", "Technology")

    def test_merge_relation_rejects_unknown_to_label(self):
        engine = EngramaEngine(_stub_store())
        with pytest.raises(ValueError, match="Unknown node label"):
            engine.merge_relation("a", "Project", "USES", "b", "BogusLabel")


# ---------------------------------------------------------------------------
# 4. MCP _with_mcp_provenance — sanitised at the boundary too
# ---------------------------------------------------------------------------


class TestMCPSanitisedHelper:
    def test_strips_caller_reserved_keys(self):
        out = _with_mcp_provenance({"body": "ok", "source": "fake", "trust_level": 1.0})
        assert out["source"] == "mcp"
        assert out["trust_level"] == DEFAULT_TRUST_LEVELS["mcp"]
        assert out["body"] == "ok"

    def test_strips_control_chars_in_extras(self):
        out = _with_mcp_provenance({"body": "hello\x00world"})
        assert out["body"] == "helloworld"

    def test_strips_underscore_keys_in_extras(self):
        out = _with_mcp_provenance({"body": "ok", "_secret": "leak"})
        assert "_secret" not in out
