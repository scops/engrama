"""Graph health report: pure metrics, scoped snapshots and the CLI."""

from __future__ import annotations

import json
import uuid
from argparse import Namespace
from pathlib import Path

import pytest

import engrama.cli as cli
from engrama import Engrama
from engrama.core.health import compute_health, format_health, normalise_key


def _node(nid: str, label: str, key: str, **extra) -> dict:
    base = {
        "id": nid,
        "label": label,
        "key": key,
        "has_engrama_id": True,
        "has_source": True,
        "has_trust": True,
    }
    return {**base, **extra}


@pytest.fixture()
def report() -> dict:
    nodes = [
        _node("p", "Project", "Atlas"),
        _node("d1", "Decision", "use atlas", tags=["atlas"]),  # tag names an anchor, no edge
        _node("d2", "Decision", "lonely"),  # live orphan
        _node("s", "Technology", "Toolbox", status="stub"),  # hub stub (degree 3)
        _node("t1", "Concept", "a"),
        _node("t2", "Concept", "b"),
        _node("arch", "Concept", "Old Atlas", status="archived"),  # bridge
        _node("dup", "Concept", "old-atlas"),  # normalised duplicate of the archived one
        _node("i", "Insight", "Shared technology: Toolbox", source_query="shared_technology"),
        _node("hand", "Insight", "lesson learned", has_engrama_id=False),  # hand-written
    ]
    edges = [("p", "s"), ("t1", "s"), ("t2", "s"), ("arch", "t1"), ("hand", "p"), ("dup", "t2")]
    return compute_health(nodes, edges)


def test_normalise_key_unifies_case_accents_and_separators() -> None:
    assert normalise_key("  Café_Olé-Déjà  vu ") == "cafe ole deja vu"


def test_totals_split_system_insights_from_hand_written(report: dict) -> None:
    t = report["totals"]
    assert (t["nodes"], t["system_insights"], t["archived"], t["stubs"]) == (10, 1, 1, 1)
    assert t["live"] == 8  # hand-written Insight counts as a live domain node


def test_orphans_exclude_system_insights(report: dict) -> None:
    o = report["orphans"]
    assert o["live"] == 2  # d1 and d2
    assert o["by_label"] == {"Decision": 2}
    assert o["system_insights"] == 1


def test_hub_stubs_duplicates_tags_and_bridges(report: dict) -> None:
    assert report["hub_stubs"]["top"] == [
        {"node": "Technology:Toolbox", "degree": 3, "has_summary": None}
    ]
    assert report["duplicates"]["groups"] == 1
    assert sorted(report["duplicates"]["top"][0]) == [
        "Concept:Old Atlas",
        "Concept:old-atlas",
    ]
    assert report["tags_without_edge"]["top"] == [{"anchor": "Project:Atlas", "unlinked": 1}]
    assert report["archived"]["top_bridges"] == [
        {"node": "Concept:Old Atlas", "live_neighbours": 1}
    ]


def test_components_live_and_core(report: dict) -> None:
    live = report["components"]["live"]
    # {p, s, t1, t2, hand, dup} + {d1} + {d2}; the archived bridge is not live.
    assert (live["nodes"], live["components"], live["largest"], live["singletons"]) == (8, 3, 6, 2)
    core = report["components"]["core"]
    # Without the stub, the hub falls apart: {p, hand}, {t1}, {t2, dup}, {d1}, {d2}.
    assert (core["components"], core["largest"]) == (5, 2)


def test_schema_gaps_and_text_rendering(report: dict) -> None:
    assert report["schema"]["missing_engrama_id"] == 1
    text = format_health(report)
    assert "Hub stubs (degree >= 3): 1" in text
    assert "Project:Atlas: 1" in text


def test_empty_snapshot_is_well_formed() -> None:
    empty = compute_health([], [])
    assert empty["orphans"]["live_pct"] == 0.0
    assert empty["components"]["live"]["components"] == 0


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EMBEDDING_PROVIDER", "null")
    for var in ("ENGRAMA_ORG_ID", "ENGRAMA_USER_ID", "ENGRAMA_LOCAL_SUB", "VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)


def test_sqlite_report_is_scoped_to_the_caller(tmp_path: Path) -> None:
    db = tmp_path / "health.db"
    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="alice") as eng:
        eng.remember("Project", "roadmap", "alice project")
        eng.remember("Decision", "ship it", "orphan decision")
        eng.associate("roadmap", "Project", "INFORMED_BY", "ship it", "Decision")
        eng.remember("Concept", "alone", "orphan concept")
    with Engrama(backend="sqlite", db_path=db, org_id="globex", user_id="bob") as eng:
        eng.remember("Concept", "bob-only", "never visible to alice")

    with Engrama(backend="sqlite", db_path=db, org_id="acme", user_id="alice") as eng:
        report = eng.health()
    assert report["totals"]["nodes"] == 3
    assert report["totals"]["edges"] == 1
    assert report["orphans"]["by_label"] == {"Concept": 1}


def test_cli_health_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("GRAPH_BACKEND", "sqlite")
    monkeypatch.setenv("ENGRAMA_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("ENGRAMA_LOCAL_SUB", "cli-user")
    with Engrama(backend="sqlite", db_path=tmp_path / "cli.db") as eng:
        eng.remember("Concept", "solo", "an orphan")

    assert cli.cmd_health(Namespace(json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["orphans"]["live"] == 1


@pytest.mark.neo4j
def test_neo4j_snapshot_matches_the_sqlite_contract() -> None:
    owner = f"health-{uuid.uuid4().hex[:8]}"
    with Engrama(backend="neo4j", org_id=owner, user_id=owner) as eng:
        try:
            eng.remember("Project", f"{owner}-p", "project")
            eng.remember("Decision", f"{owner}-d", "decision")
            eng.associate(f"{owner}-p", "Project", "INFORMED_BY", f"{owner}-d", "Decision")
            eng.remember("Concept", f"{owner}-alone", "orphan")
            report = eng.health()
        finally:
            eng._store._client.run("MATCH (n) WHERE n.org_id = $o DETACH DELETE n", {"o": owner})
    assert (report["totals"]["nodes"], report["totals"]["edges"]) == (3, 1)
    assert report["orphans"]["by_label"] == {"Concept": 1}
