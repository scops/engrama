"""Tests for issue #51 — engine.merge_node honours ``TITLE_KEYED_LABELS``.

Before the fix, ``EngramaEngine.merge_node`` picked the merge key purely
from the caller's property bag (``name`` wins if present, else
``title``).  The label's canonical position in :data:`TITLE_KEYED_LABELS`
was only consulted by :class:`RememberSkill`; callers writing directly
through the engine (notably the MCP ``engrama_remember`` tool, which
forwards ``params.properties`` verbatim) bypassed it.

The user-visible symptom differs by backend:
* Neo4j — ``MERGE (n:Experiment {name: 'X'})`` and ``MERGE (n:Experiment
  {title: 'X'})`` are two distinct patterns, so the same logical node
  ended up in two rows.
* SQLite — ``UNIQUE(label, key_value)`` collapses both writes onto one
  row, but the row's stored properties (``name`` vs ``title``) diverged
  depending on which adapter wrote first, so downstream code that
  filtered by property key missed the node.

Both manifestations stem from the same root: the engine deferred to the
caller. The fix canonicalises the merge key inside ``merge_node`` after
sanitisation, so both call paths converge on the canonical column.

These tests exercise the SQLite backend (no external services required);
the contract is identical on Neo4j once the engine canonicalises before
delegating to the store.
"""

from __future__ import annotations

import pytest

from engrama import Engrama
from engrama.core.schema import TITLE_KEYED_LABELS


@pytest.fixture()
def sdk(tmp_path, monkeypatch):
    """SDK pinned to a fresh per-test SQLite DB with embeddings disabled."""
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in (
        "ENGRAMA_ORG_ID",
        "ENGRAMA_USER_ID",
        "ENGRAMA_AGENT_ID",
        "ENGRAMA_SESSION_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    eng = Engrama(backend="sqlite", db_path=tmp_path / "merge-key.db")
    yield eng
    eng.close()


@pytest.mark.parametrize("label", sorted(TITLE_KEYED_LABELS))
def test_raw_write_with_wrong_key_uses_canonical(sdk: Engrama, label: str) -> None:
    """A direct ``engine.merge_node`` call that uses ``name`` for a
    title-keyed label must end up keyed by the canonical ``title``."""
    identity = f"raw-only-{label.lower()}"
    sdk._engine.merge_node(label, {"name": identity, "notes": "raw write"})

    row = sdk._store.get_node(label, "title", identity)
    assert row is not None, f"node not stored for {label}"
    assert row.get("title") == identity, (
        f"canonical 'title' missing for {label!r}; "
        "engine forwarded the non-canonical 'name' to the store"
    )
    # The non-canonical alias must NOT have been persisted as a property.
    assert "name" not in row or row.get("name") != identity, (
        f"non-canonical 'name' alias leaked into stored properties for {label!r}"
    )


@pytest.mark.parametrize("label", sorted(TITLE_KEYED_LABELS))
def test_raw_first_then_sdk_converges_canonical(sdk: Engrama, label: str) -> None:
    """Pre-fix, a raw write with ``name`` followed by a SDK write would
    leave the row stored under ``key_field='name'`` even after the SDK
    update, because the SQLite UPDATE branch never rewrites ``key_field``.
    Post-fix the canonicalisation happens before the first store call so
    the row is canonical from the start."""
    identity = f"raw-then-sdk-{label.lower()}"

    # Raw write first with the wrong key.
    sdk._engine.merge_node(label, {"name": identity, "notes": "via raw"})
    # SDK write second (uses canonical key via RememberSkill).
    sdk.remember(label, identity, "via SDK")

    row = sdk._store.get_node(label, "title", identity)
    assert row is not None
    assert row.get("title") == identity
    assert "name" not in row or row.get("name") != identity
    assert row.get("notes") == "via SDK"


def test_name_keyed_label_demotes_stray_title(sdk: Engrama) -> None:
    """Symmetric direction — for a name-keyed label like ``Concept``,
    a caller that mistakenly puts ``title`` in the bag has it canonicalised
    to ``name`` so SDK and raw writes stay aligned."""
    identity = "demote-target"
    sdk._engine.merge_node("Concept", {"title": identity, "notes": "wrong key"})
    row = sdk._store.get_node("Concept", "name", identity)
    assert row is not None
    assert row.get("name") == identity
    assert "title" not in row or row.get("title") != identity


def test_both_keys_present_keeps_canonical_drops_other(sdk: Engrama) -> None:
    """When the caller passes both keys with conflicting values for a
    title-keyed label, the canonical value (``title``) wins and the
    non-canonical alias is silently dropped — matches the sanitiser's
    pattern of silently removing reserved keys."""
    sdk._engine.merge_node(
        "Experiment",
        {"title": "canonical-wins", "name": "loser", "notes": "conflict"},
    )
    row_winner = sdk._store.get_node("Experiment", "title", "canonical-wins")
    row_loser = sdk._store.get_node("Experiment", "title", "loser")

    assert row_winner is not None
    assert row_winner.get("title") == "canonical-wins"
    assert row_winner.get("notes") == "conflict"
    # The non-canonical alias was discarded — it is NOT preserved as a
    # property on the stored node, and no separate row was created.
    assert "name" not in row_winner or row_winner.get("name") != "loser"
    assert row_loser is None


def test_missing_canonical_key_raises_with_label_context(sdk: Engrama) -> None:
    """The error message now names the canonical key the label expects,
    so callers debug the right thing."""
    with pytest.raises(ValueError, match="'title'.*'Experiment'"):
        sdk._engine.merge_node("Experiment", {"notes": "no identity at all"})

    with pytest.raises(ValueError, match="'name'.*'Concept'"):
        sdk._engine.merge_node("Concept", {"notes": "no identity at all"})


def test_remember_skill_unaffected(sdk: Engrama) -> None:
    """RememberSkill already picks the canonical key — the engine fix
    must not change its behaviour."""
    sdk.remember("Experiment", "skill-write", "via skill")
    row = sdk._store.get_node("Experiment", "title", "skill-write")
    assert row is not None
    assert row.get("title") == "skill-write"
    assert row.get("notes") == "via skill"
