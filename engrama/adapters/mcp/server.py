"""
Engrama MCP server — high-level memory tools for AI agents.

Exposes ten tools via the Model Context Protocol:

* **engrama_search** — fulltext search across the memory graph.
* **engrama_remember** — create or update a node (always MERGE).
* **engrama_relate** — create a relationship between two nodes.
* **engrama_context** — retrieve the neighbourhood of a node.
* **engrama_sync_note** — sync a single Obsidian note to the graph.
* **engrama_sync_vault** — full vault scan, reconcile all notes.
* **engrama_reflect** — cross-entity pattern detection → Insight nodes.
* **engrama_surface_insights** — read pending Insights for agent presentation.
* **engrama_approve_insight** — human approves or dismisses an Insight.
* **engrama_write_insight_to_vault** — append approved Insight to Obsidian note.

All writes use ``MERGE`` with automatic timestamps.  All queries use
Cypher parameters — never string formatting.

The server uses an **async** Neo4j driver managed through FastMCP's
lifespan hook, so the connection is shared across tool calls and
properly closed on shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from neo4j import AsyncGraphDatabase, AsyncDriver
from pydantic import BaseModel, ConfigDict, Field

from engrama.core.schema import NodeType, RelationType, TITLE_KEYED_LABELS
from engrama.adapters.obsidian import ObsidianAdapter, NoteParser

logger = logging.getLogger("engrama_mcp")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Valid labels / relations (used for validation)
# ---------------------------------------------------------------------------

_VALID_LABELS: set[str] = {member.value for member in NodeType}
_VALID_RELATIONS: set[str] = {member.value for member in RelationType}


# ---------------------------------------------------------------------------
# Pydantic input models
# ---------------------------------------------------------------------------


class SearchInput(BaseModel):
    """Input for ``engrama_search``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        description="Lucene-syntax search string (e.g. 'neo4j', 'Python framework').",
        min_length=1,
        max_length=500,
    )
    limit: int = Field(
        default=10,
        description="Maximum results to return.",
        ge=1,
        le=50,
    )


class RememberInput(BaseModel):
    """Input for ``engrama_remember``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(
        ...,
        description=(
            f"Node label — one of: {', '.join(sorted(_VALID_LABELS))}."
        ),
    )
    properties: dict[str, Any] = Field(
        ...,
        description=(
            "Node properties. Must include 'name' (or 'title' for Decision/Problem). "
            "Example: {\"name\": \"engrama\", \"status\": \"active\", \"repo\": \"scops/engrama\"}."
        ),
    )


class RelateInput(BaseModel):
    """Input for ``engrama_relate``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    from_name: str = Field(..., description="Name of the source node.", min_length=1)
    from_label: str = Field(
        ...,
        description=f"Label of the source node — one of: {', '.join(sorted(_VALID_LABELS))}.",
    )
    rel_type: str = Field(
        ...,
        description=f"Relationship type — one of: {', '.join(sorted(_VALID_RELATIONS))}.",
    )
    to_name: str = Field(..., description="Name of the target node.", min_length=1)
    to_label: str = Field(
        ...,
        description=f"Label of the target node — one of: {', '.join(sorted(_VALID_LABELS))}.",
    )


class ContextInput(BaseModel):
    """Input for ``engrama_context``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(..., description="Name of the starting node.", min_length=1)
    label: str = Field(
        ...,
        description=f"Label of the starting node — one of: {', '.join(sorted(_VALID_LABELS))}.",
    )
    hops: int = Field(
        default=1,
        description="Maximum relationship depth to traverse.",
        ge=1,
        le=3,
    )


class SyncNoteInput(BaseModel):
    """Input for ``engrama_sync_note``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    path: str = Field(
        ...,
        description="Vault-relative path to the note, e.g. '10-projects/engrama.md'.",
        min_length=1,
    )


class SyncVaultInput(BaseModel):
    """Input for ``engrama_sync_vault``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    folder: str = Field(
        default="",
        description="Optional folder to restrict the scan (vault-relative). "
                    "Empty string scans the entire vault.",
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_engrama_mcp(
    db_url: str = "bolt://localhost:7687",
    username: str = "neo4j",
    password: str = "",
    database: str = "neo4j",
    vault_path: str | None = None,
) -> FastMCP:
    """Create and return a configured Engrama MCP server.

    Parameters:
        db_url: Neo4j bolt URI.
        username: Neo4j username.
        password: Neo4j password.
        database: Neo4j database name.
        vault_path: Absolute path to the Obsidian vault root.
                    Falls back to ``VAULT_PATH`` env var.

    Returns:
        A :class:`FastMCP` instance ready to run.
    """

    # -- Lifespan: manage the async Neo4j driver + Obsidian adapter ------

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # noqa: ARG001
        driver: AsyncDriver = AsyncGraphDatabase.driver(
            db_url, auth=(username, password)
        )
        # Initialise Obsidian adapter (optional — sync tools disabled if no vault)
        resolved_vault = vault_path or os.environ.get("VAULT_PATH")
        obsidian: ObsidianAdapter | None = None
        if resolved_vault:
            try:
                obsidian = ObsidianAdapter(resolved_vault)
                logger.info("Obsidian adapter initialised: %s", obsidian.vault_path)
            except FileNotFoundError:
                logger.warning("VAULT_PATH %s not found — sync tools disabled", resolved_vault)
        else:
            logger.info("VAULT_PATH not set — Obsidian sync tools disabled")

        try:
            await driver.verify_connectivity()
            logger.info("Engrama MCP connected to Neo4j at %s", db_url)
            yield {
                "driver": driver,
                "database": database,
                "obsidian": obsidian,
                "parser": NoteParser(),
            }
        finally:
            await driver.close()
            logger.info("Engrama MCP disconnected from Neo4j")

    mcp = FastMCP("engrama_mcp", lifespan=lifespan)

    # -- Helper: get driver from context ----------------------------------

    def _driver_and_db(ctx: Context) -> tuple[AsyncDriver, str]:
        """Extract the Neo4j driver and database name from the lifespan state."""
        state = ctx.request_context.lifespan_context
        return state["driver"], state["database"]

    # -- Tool: engrama_search ---------------------------------------------

    @mcp.tool(
        name="engrama_search",
        annotations=ToolAnnotations(
            title="Search Memory Graph",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_search(params: SearchInput, ctx: Context) -> str:
        """Search the Engrama memory graph using fulltext search.

        Queries the ``memory_search`` fulltext index across all node types
        and text properties (name, title, description, notes, rationale,
        solution, context).

        Returns a JSON array of matches with ``type``, ``name``, and ``score``.
        """
        driver, db = _driver_and_db(ctx)
        cypher = (
            'CALL db.index.fulltext.queryNodes("memory_search", $query) '
            "YIELD node, score "
            "RETURN labels(node)[0] AS type, node.name AS name, score "
            "ORDER BY score DESC LIMIT $limit"
        )
        records, _, _ = await driver.execute_query(
            cypher,
            parameters_={"query": params.query, "limit": params.limit},
            database_=db,
        )
        results = [dict(r) for r in records]
        if not results:
            return f"No results found for '{params.query}'."
        return json.dumps(results, default=str, indent=2)

    # -- Tool: engrama_remember -------------------------------------------

    @mcp.tool(
        name="engrama_remember",
        annotations=ToolAnnotations(
            title="Remember (Merge Node)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_remember(params: RememberInput, ctx: Context) -> str:
        """Create or update a node in the memory graph using MERGE.

        If a node with the same ``name`` (or ``title``) already exists, its
        properties are updated.  ``created_at`` is set on first write;
        ``updated_at`` is refreshed on every call.

        Returns a summary of the operation.
        """
        label = params.label
        props = params.properties

        if label not in _VALID_LABELS:
            return f"Error: Invalid label '{label}'. Must be one of: {', '.join(sorted(_VALID_LABELS))}."

        # Determine merge key
        if "name" in props:
            merge_key = "name"
        elif "title" in props:
            merge_key = "title"
        else:
            return "Error: properties must include 'name' or 'title' as a merge key."

        merge_value = props[merge_key]
        extra = {k: v for k, v in props.items() if k not in {merge_key, "created_at", "updated_at"}}

        # Build parameterised SET clauses
        set_create = ["n.created_at = datetime()", "n.updated_at = datetime()"]
        set_match = ["n.updated_at = datetime()"]
        cypher_params: dict[str, Any] = {"merge_value": merge_value}

        for idx, (key, value) in enumerate(extra.items()):
            pname = f"p{idx}"
            set_create.append(f"n.{key} = ${pname}")
            set_match.append(f"n.{key} = ${pname}")
            cypher_params[pname] = value

        cypher = (
            f"MERGE (n:{label} {{{merge_key}: $merge_value}}) "
            f"ON CREATE SET {', '.join(set_create)} "
            f"ON MATCH SET {', '.join(set_match)} "
            "RETURN n"
        )

        driver, db = _driver_and_db(ctx)
        records, _, _ = await driver.execute_query(
            cypher, parameters_=cypher_params, database_=db
        )
        node = dict(records[0]["n"]) if records else {}
        return json.dumps(
            {"status": "ok", "label": label, "node": node},
            default=str,
            indent=2,
        )

    # -- Tool: engrama_relate ---------------------------------------------

    @mcp.tool(
        name="engrama_relate",
        annotations=ToolAnnotations(
            title="Relate (Merge Relationship)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_relate(params: RelateInput, ctx: Context) -> str:
        """Create or update a relationship between two existing nodes.

        Both endpoints are matched by ``name``.  If either node does not
        exist, no relationship is created (no error).

        Returns a confirmation or a message if no match was found.
        """
        if params.from_label not in _VALID_LABELS:
            return f"Error: Invalid from_label '{params.from_label}'."
        if params.to_label not in _VALID_LABELS:
            return f"Error: Invalid to_label '{params.to_label}'."
        if params.rel_type not in _VALID_RELATIONS:
            return f"Error: Invalid rel_type '{params.rel_type}'. Must be one of: {', '.join(sorted(_VALID_RELATIONS))}."

        # Decision and Problem nodes use `title` as their unique key;
        # all other node types use `name`.
        from_key = "title" if params.from_label in TITLE_KEYED_LABELS else "name"
        to_key = "title" if params.to_label in TITLE_KEYED_LABELS else "name"

        cypher = (
            f"MATCH (a:{params.from_label} {{{from_key}: $from_name}}) "
            f"MATCH (b:{params.to_label} {{{to_key}: $to_name}}) "
            f"MERGE (a)-[r:{params.rel_type}]->(b) "
            f"RETURN type(r) AS rel_type, "
            f"a.{from_key} AS from_name, b.{to_key} AS to_name"
        )
        cypher_params = {"from_name": params.from_name, "to_name": params.to_name}

        driver, db = _driver_and_db(ctx)
        records, _, _ = await driver.execute_query(
            cypher, parameters_=cypher_params, database_=db
        )
        if not records:
            return (
                f"No relationship created — could not find "
                f"(:{params.from_label} {{name: '{params.from_name}'}}) "
                f"or (:{params.to_label} {{name: '{params.to_name}'}})."
            )
        r = dict(records[0])
        return json.dumps({"status": "ok", **r}, default=str, indent=2)

    # -- Tool: engrama_context --------------------------------------------

    @mcp.tool(
        name="engrama_context",
        annotations=ToolAnnotations(
            title="Get Context (Neighbourhood)",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_context(params: ContextInput, ctx: Context) -> str:
        """Retrieve the local neighbourhood of a node up to N hops.

        Returns the starting node and all connected nodes with their
        relationship types, useful for building context before answering
        a question.
        """
        if params.label not in _VALID_LABELS:
            return f"Error: Invalid label '{params.label}'."

        cypher = (
            f"MATCH (start:{params.label} {{name: $name}}) "
            f"OPTIONAL MATCH (start)-[r*1..{params.hops}]-(neighbour) "
            "WITH start, r, neighbour "
            "WHERE neighbour IS NOT NULL "
            "RETURN start, "
            "  [rel IN r | type(rel)] AS rel_types, "
            "  labels(neighbour)[0] AS neighbour_label, "
            "  neighbour.name AS neighbour_name, "
            "  properties(neighbour) AS neighbour_props"
        )
        driver, db = _driver_and_db(ctx)
        records, _, _ = await driver.execute_query(
            cypher, parameters_={"name": params.name}, database_=db
        )

        if not records:
            return f"No node found: (:{params.label} {{name: '{params.name}'}})."

        # Format: start node + list of neighbours
        start_node = dict(records[0]["start"]) if records[0]["start"] else {}
        neighbours = []
        for r in records:
            if r["neighbour_name"]:
                neighbours.append({
                    "label": r["neighbour_label"],
                    "name": r["neighbour_name"],
                    "via": r["rel_types"],
                    "properties": {
                        k: v for k, v in (r["neighbour_props"] or {}).items()
                        if k not in {"created_at", "updated_at"}
                    },
                })

        result = {
            "node": {
                "label": params.label,
                "properties": {
                    k: v for k, v in start_node.items()
                    if k not in {"created_at", "updated_at"}
                },
            },
            "neighbours": neighbours,
        }
        return json.dumps(result, default=str, indent=2)

    # -- Helper: async MERGE via the async driver (mirrors engine logic) --

    async def _async_merge_node(
        driver: AsyncDriver,
        db: str,
        label: str,
        properties: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge a node using the async driver (same logic as EngramaEngine.merge_node).

        Returns a dict with ``node`` properties and ``created`` flag.
        """
        if "name" in properties:
            merge_key = "name"
        elif "title" in properties:
            merge_key = "title"
        else:
            raise ValueError("properties must include 'name' or 'title'")

        merge_value = properties[merge_key]
        extra = {
            k: v for k, v in properties.items()
            if k not in {merge_key, "created_at", "updated_at"}
        }

        set_create = ["n.created_at = datetime()", "n.updated_at = datetime()"]
        set_match = ["n.updated_at = datetime()"]
        params: dict[str, Any] = {"merge_value": merge_value}

        for idx, (key, value) in enumerate(extra.items()):
            pname = f"p{idx}"
            set_create.append(f"n.{key} = ${pname}")
            set_match.append(f"n.{key} = ${pname}")
            params[pname] = value

        cypher = (
            f"MERGE (n:{label} {{{merge_key}: $merge_value}}) "
            f"ON CREATE SET {', '.join(set_create)} "
            f"ON MATCH SET {', '.join(set_match)} "
            "RETURN n, "
            "CASE WHEN n.created_at = n.updated_at THEN true ELSE false END AS created"
        )

        records, _, _ = await driver.execute_query(
            cypher, parameters_=params, database_=db
        )
        if records:
            return {"node": dict(records[0]["n"]), "created": records[0]["created"]}
        return {"node": {}, "created": False}

    # -- Tool: engrama_sync_note ------------------------------------------

    @mcp.tool(
        name="engrama_sync_note",
        annotations=ToolAnnotations(
            title="Sync Obsidian Note to Graph",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_sync_note(params: SyncNoteInput, ctx: Context) -> str:
        """Sync a single Obsidian note to the Neo4j memory graph.

        Reads the note via ObsidianAdapter, parses entities via NoteParser,
        merges the node into Neo4j, and injects ``engrama_id`` back into
        the note's YAML frontmatter.

        Returns JSON with status, label, name, engrama_id, and
        whether the node was created or updated.
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        parser: NoteParser = state["parser"]
        driver: AsyncDriver = state["driver"]
        db: str = state["database"]

        if obsidian is None:
            return json.dumps({
                "status": "error",
                "error": "VAULT_PATH not configured — Obsidian sync disabled.",
            })

        # 1. Read note
        note_data = obsidian.read_note(params.path)
        if not note_data["success"]:
            return json.dumps({
                "status": "error",
                "error": f"Could not read note: {params.path}",
            })

        # 2. Parse entities
        parsed = parser.parse(
            path=params.path,
            content=note_data["content"],
            frontmatter=note_data["frontmatter"],
        )
        if parsed is None:
            return json.dumps({
                "status": "skipped",
                "reason": "Note could not be classified into an Engrama label.",
            })

        # 3. Ensure engrama_id
        engrama_id = parsed.engrama_id or str(uuid.uuid4())
        if not parsed.engrama_id:
            obsidian.inject_engrama_id(params.path, engrama_id)

        # 4. Merge node into Neo4j
        props = {
            **parsed.properties,
            "obsidian_id": engrama_id,
            "obsidian_path": params.path,
        }
        merge_result = await _async_merge_node(driver, db, parsed.label, props)

        return json.dumps({
            "status": "ok",
            "label": parsed.label,
            "name": parsed.name,
            "engrama_id": engrama_id,
            "created_or_updated": "created" if merge_result["created"] else "updated",
        }, default=str, indent=2)

    # -- Tool: engrama_sync_vault -----------------------------------------

    @mcp.tool(
        name="engrama_sync_vault",
        annotations=ToolAnnotations(
            title="Sync Obsidian Vault to Graph",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def engrama_sync_vault(params: SyncVaultInput, ctx: Context) -> str:
        """Scan the Obsidian vault and reconcile all documentable notes.

        Iterates over all ``.md`` files (optionally restricted to a folder),
        parses entities, and merges nodes into Neo4j.  Injects ``engrama_id``
        into notes that don't have one yet.

        Returns JSON with created, updated, and skipped counts.
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        parser: NoteParser = state["parser"]
        driver: AsyncDriver = state["driver"]
        db: str = state["database"]

        if obsidian is None:
            return json.dumps({
                "status": "error",
                "error": "VAULT_PATH not configured — Obsidian sync disabled.",
            })

        notes = obsidian.list_notes(folder=params.folder, recursive=True)
        created = updated = skipped = 0

        for note_meta in notes:
            path = note_meta["path"]
            note_data = obsidian.read_note(path)
            if not note_data["success"]:
                skipped += 1
                continue

            parsed = parser.parse(
                path=path,
                content=note_data["content"],
                frontmatter=note_data["frontmatter"],
            )
            if parsed is None:
                skipped += 1
                continue

            # Ensure engrama_id
            engrama_id = parsed.engrama_id or str(uuid.uuid4())
            if not parsed.engrama_id:
                obsidian.inject_engrama_id(path, engrama_id)

            # Merge node
            props = {
                **parsed.properties,
                "obsidian_id": engrama_id,
                "obsidian_path": path,
            }
            merge_result = await _async_merge_node(driver, db, parsed.label, props)
            if merge_result["created"]:
                created += 1
            else:
                updated += 1

        return json.dumps({
            "status": "ok",
            "created": created,
            "updated": updated,
            "skipped": skipped,
        }, indent=2)

    # -- Tool: engrama_reflect --------------------------------------------

    # Import the query constants from the reflect skill so they stay in one
    # place.  The skill module is pure Python with no heavy deps.
    from engrama.skills.reflect import (
        _QUERY_CROSS_PROJECT_SOLUTION,
        _QUERY_SHARED_TECHNOLOGY,
        _QUERY_TRAINING_OPPORTUNITY,
    )

    @mcp.tool(
        name="engrama_reflect",
        annotations=ToolAnnotations(
            title="Reflect (Cross-Entity Pattern Detection)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_reflect(ctx: Context) -> str:
        """Run cross-entity pattern detection across the memory graph.

        Executes three detection queries looking for patterns that span
        projects, problems, decisions, technologies, and courses.  Each
        detected pattern is written as an ``Insight`` node with
        ``status: "pending"`` — the human reviews and approves or dismisses.

        Detection queries:

        1. **Cross-project solution transfer** — an open Problem shares a
           Concept with a resolved Problem that has a Decision in another
           Project.
        2. **Shared technology** — two active Projects use the same
           Technology.
        3. **Training opportunity** — an open Problem shares a Concept
           with a Course.

        Returns JSON with a list of insights created/updated, grouped by
        detection query, plus total counts.
        """
        driver, db = _driver_and_db(ctx)
        insights: list[dict[str, Any]] = []

        # --- Query 1: cross-project solution transfer ---
        records, _, _ = await driver.execute_query(
            _QUERY_CROSS_PROJECT_SOLUTION,
            parameters_={"open_status": "open", "resolved_status": "resolved"},
            database_=db,
        )
        for r in records:
            title = (
                f"Solution transfer: {r['decision']} "
                f"({r['source_project']} → {r['target_project']})"
            )
            body = (
                f"The open problem \"{r['open_problem']}\" in project "
                f"\"{r['target_project']}\" shares the concept "
                f"\"{r['concept']}\" with a resolved problem in project "
                f"\"{r['source_project']}\". The decision "
                f"\"{r['decision']}\" may apply here."
            )
            await _async_merge_node(driver, db, "Insight", {
                "title": title,
                "body": body,
                "confidence": 0.8,
                "status": "pending",
                "source_query": "cross_project_solution",
            })
            insights.append({
                "query": "cross_project_solution",
                "title": title,
                "confidence": 0.8,
            })

        # --- Query 2: shared technology ---
        records, _, _ = await driver.execute_query(
            _QUERY_SHARED_TECHNOLOGY,
            parameters_={"active_status": "active"},
            database_=db,
        )
        for r in records:
            title = (
                f"Shared technology: {r['technology']} "
                f"({r['project_a']} & {r['project_b']})"
            )
            body = (
                f"Both \"{r['project_a']}\" and \"{r['project_b']}\" "
                f"use {r['technology']}. Consider sharing knowledge, "
                f"libraries, or configuration between these projects."
            )
            await _async_merge_node(driver, db, "Insight", {
                "title": title,
                "body": body,
                "confidence": 0.7,
                "status": "pending",
                "source_query": "shared_technology",
            })
            insights.append({
                "query": "shared_technology",
                "title": title,
                "confidence": 0.7,
            })

        # --- Query 3: training opportunity ---
        records, _, _ = await driver.execute_query(
            _QUERY_TRAINING_OPPORTUNITY,
            parameters_={"open_status": "open"},
            database_=db,
        )
        for r in records:
            title = (
                f"Training opportunity: {r['course']} "
                f"covers {r['concept']} (relates to: {r['problem']})"
            )
            body = (
                f"The open problem \"{r['problem']}\" involves the concept "
                f"\"{r['concept']}\", which is covered by the course "
                f"\"{r['course']}\". Reviewing this material may help."
            )
            await _async_merge_node(driver, db, "Insight", {
                "title": title,
                "body": body,
                "confidence": 0.6,
                "status": "pending",
                "source_query": "training_opportunity",
            })
            insights.append({
                "query": "training_opportunity",
                "title": title,
                "confidence": 0.6,
            })

        return json.dumps({
            "status": "ok",
            "insights_count": len(insights),
            "insights": insights,
        }, default=str, indent=2)

    # -- Tool: engrama_surface_insights ------------------------------------

    class SurfaceInput(BaseModel):
        """Input for engrama_surface_insights."""
        model_config = ConfigDict(extra="forbid")
        limit: int = Field(
            default=10,
            description="Maximum number of pending Insights to return.",
        )

    @mcp.tool(
        name="engrama_surface_insights",
        annotations=ToolAnnotations(
            title="Surface Pending Insights",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_surface_insights(params: SurfaceInput, ctx: Context) -> str:
        """Read all pending Insights and format them for presentation.

        Returns JSON with a list of pending Insights, newest first.
        The agent should present these to the human for review — never
        act on them without explicit approval.
        """
        driver, db = _driver_and_db(ctx)

        records, _, _ = await driver.execute_query(
            "MATCH (i:Insight {status: $status}) "
            "RETURN i.title AS title, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query, "
            "       i.created_at AS created_at "
            "ORDER BY i.created_at DESC "
            "LIMIT $limit",
            parameters_={"status": "pending", "limit": params.limit},
            database_=db,
        )

        insights = []
        for r in records:
            created = r["created_at"]
            insights.append({
                "title": r["title"],
                "body": r["body"],
                "confidence": r["confidence"],
                "source_query": r["source_query"],
                "created_at": str(created) if created else None,
            })

        return json.dumps({
            "status": "ok",
            "pending_count": len(insights),
            "insights": insights,
        }, default=str, indent=2)

    # -- Tool: engrama_approve_insight -------------------------------------

    class ApproveInput(BaseModel):
        """Input for engrama_approve_insight."""
        model_config = ConfigDict(extra="forbid")
        title: str = Field(description="Exact title of the Insight.")
        action: str = Field(
            default="approve",
            description="'approve' or 'dismiss'.",
        )

    @mcp.tool(
        name="engrama_approve_insight",
        annotations=ToolAnnotations(
            title="Approve or Dismiss Insight",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_approve_insight(params: ApproveInput, ctx: Context) -> str:
        """Approve or dismiss a pending Insight after human review.

        Sets the Insight's status to ``"approved"`` or ``"dismissed"``
        and records a timestamp.  Only approved Insights can later be
        written to Obsidian.
        """
        driver, db = _driver_and_db(ctx)

        if params.action not in ("approve", "dismiss"):
            return json.dumps({
                "status": "error",
                "error": f"Invalid action '{params.action}'. Use 'approve' or 'dismiss'.",
            })

        new_status = "approved" if params.action == "approve" else "dismissed"
        ts_field = "approved_at" if params.action == "approve" else "dismissed_at"

        query = (
            "MATCH (i:Insight {title: $title}) "
            f"SET i.status = $new_status, "
            f"    i.{ts_field} = datetime(), "
            "    i.updated_at = datetime() "
            "RETURN i.title AS title, i.status AS status"
        )
        records, _, _ = await driver.execute_query(
            query,
            parameters_={"title": params.title, "new_status": new_status},
            database_=db,
        )

        if not records:
            return json.dumps({
                "status": "error",
                "error": f"Insight not found: {params.title}",
            })

        return json.dumps({
            "status": "ok",
            "title": params.title,
            "action": params.action,
            "new_status": new_status,
        }, indent=2)

    # -- Tool: engrama_write_insight_to_vault ------------------------------

    class WriteInsightInput(BaseModel):
        """Input for engrama_write_insight_to_vault."""
        model_config = ConfigDict(extra="forbid")
        title: str = Field(description="Exact title of the approved Insight.")
        target_note: str = Field(
            description="Relative path to the Obsidian note to append to.",
        )

    @mcp.tool(
        name="engrama_write_insight_to_vault",
        annotations=ToolAnnotations(
            title="Write Approved Insight to Obsidian",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_write_insight_to_vault(
        params: WriteInsightInput, ctx: Context
    ) -> str:
        """Append an approved Insight as a section in an Obsidian note.

        Only Insights with ``status: "approved"`` are written.  The agent
        **must not** call this on unapproved Insights.

        The Insight is appended as a Markdown section with a horizontal
        rule separator, including confidence, source query, and approval
        timestamp.
        """
        import datetime as dt

        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        driver: AsyncDriver = state["driver"]
        db: str = state["database"]

        if obsidian is None:
            return json.dumps({
                "status": "error",
                "error": "VAULT_PATH not configured — Obsidian sync disabled.",
            })

        # 1. Read the Insight from Neo4j
        records, _, _ = await driver.execute_query(
            "MATCH (i:Insight {title: $title}) "
            "RETURN i.status AS status, i.body AS body, "
            "       i.confidence AS confidence, "
            "       i.source_query AS source_query",
            parameters_={"title": params.title},
            database_=db,
        )

        if not records:
            return json.dumps({
                "status": "error",
                "error": f"Insight not found: {params.title}",
            })

        insight = records[0]

        if insight["status"] != "approved":
            return json.dumps({
                "status": "error",
                "error": (
                    f"Insight status is '{insight['status']}', not 'approved'. "
                    "Only approved Insights can be written to the vault."
                ),
            })

        # 2. Verify target note exists
        note = obsidian.read_note(params.target_note)
        if not note["success"]:
            return json.dumps({
                "status": "error",
                "error": f"Target note not found: {params.target_note}",
            })

        # 3. Build markdown section and append
        now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        confidence_pct = int(insight["confidence"] * 100)
        section = (
            f"\n## Insight: {params.title}\n\n"
            f"> **Confidence:** {confidence_pct}% · "
            f"**Source:** {insight['source_query']} · "
            f"**Approved:** {now}\n\n"
            f"{insight['body']}\n"
        )

        target_path = obsidian._resolve(params.target_note)
        current = target_path.read_text(encoding="utf-8")
        target_path.write_text(
            current.rstrip("\n") + "\n\n---\n" + section,
            encoding="utf-8",
        )

        # 4. Mark as synced in Neo4j
        await driver.execute_query(
            "MATCH (i:Insight {title: $title}) "
            "SET i.obsidian_path = $path, "
            "    i.synced_at = datetime(), "
            "    i.updated_at = datetime()",
            parameters_={"title": params.title, "path": params.target_note},
            database_=db,
        )

        return json.dumps({
            "status": "ok",
            "title": params.title,
            "target_note": params.target_note,
            "written": True,
        }, indent=2)

    return mcp
