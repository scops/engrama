"""System Insights link to the entities they talk about (DDR-008)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from engrama import Engrama
from engrama.core.scope import MemoryScope

OWNER = MemoryScope(org_id="about-org", user_id="about-user")


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_LOCAL_SUB", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


def _seed_shared_tech(eng: Engrama, p: str = "") -> None:
    eng.remember("Technology", f"{p}python", "a language")
    for proj in ("alpha", "beta", "gamma"):
        eng.remember("Project", f"{p}{proj}", "a project")
        eng.associate(f"{p}{proj}", "Project", "USES", f"{p}python", "Technology")


def _about_targets(eng: Engrama, title: str) -> set[str]:
    rows = eng._store.get_neighbours("Insight", "title", title, scope=eng._engine.default_scope)
    return {r["neighbour"].get("name") for r in rows}


def test_sdk_reflect_links_insight_to_its_evidence(tmp_path: Path) -> None:
    with Engrama(
        backend="sqlite", db_path=tmp_path / "a.db", org_id=OWNER.org_id, user_id=OWNER.user_id
    ) as eng:
        _seed_shared_tech(eng)
        eng.reflect()
        assert _about_targets(eng, "Shared technology: python") == {
            "python",
            "alpha",
            "beta",
            "gamma",
        }
        # Annotation edges don't change the structural picture.
        assert eng.health()["orphans"]["system_insights"] == 0


def test_hand_written_insight_stays_out_of_the_review_queue(tmp_path: Path) -> None:
    with Engrama(
        backend="sqlite", db_path=tmp_path / "b.db", org_id=OWNER.org_id, user_id=OWNER.user_id
    ) as eng:
        eng._engine.merge_node("Insight", {"title": "my lesson", "status": "pending", "body": "x"})
        assert eng.surface_insights() == []


async def test_mcp_reflect_links_and_context_shows_the_insight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcp.client import Client

    from engrama.adapters.mcp import server as srv_module
    from engrama.adapters.mcp.server import create_engrama_mcp

    db = tmp_path / "mcp.db"
    with Engrama(backend="sqlite", db_path=db, org_id=OWNER.org_id, user_id=OWNER.user_id) as eng:
        _seed_shared_tech(eng)

    server = create_engrama_mcp(backend="sqlite", config={"ENGRAMA_DB_PATH": str(db)})
    monkeypatch.setattr(srv_module, "resolve_scope", lambda _ctx: OWNER)
    async with Client(server) as client:
        await client.call_tool("engrama_reflect", {})
        result = await client.call_tool(
            "engrama_context", {"params": {"name": "python", "label": "Technology"}}
        )
    context = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert "Shared technology: python" in json.dumps(context)


@pytest.mark.neo4j
def test_neo4j_sdk_reflect_links_insight() -> None:
    owner = f"about-{uuid.uuid4().hex[:8]}"
    with Engrama(backend="neo4j", org_id=owner, user_id=owner) as eng:
        try:
            _seed_shared_tech(eng, p=f"{owner}-")
            eng.reflect()
            targets = _about_targets(eng, f"Shared technology: {owner}-python")
            assert f"{owner}-python" in targets and f"{owner}-alpha" in targets
        finally:
            eng._store._client.run("MATCH (n) WHERE n.org_id = $o DETACH DELETE n", {"o": owner})
