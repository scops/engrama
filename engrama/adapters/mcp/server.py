"""
Engrama MCP server — high-level memory tools for AI agents.

Exposes eleven tools via the Model Context Protocol:

* **engrama_search** — fulltext search across the memory graph.
* **engrama_remember** — create or update a node (always MERGE).
* **engrama_relate** — create a relationship between two nodes.
* **engrama_context** — retrieve the neighbourhood of a node.
* **engrama_sync_note** — sync a single Obsidian note to the graph.
* **engrama_sync_vault** — full vault scan, reconcile all notes.
* **engrama_ingest** — read content + return extraction guidance for the agent.
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
# Proactivity state (module-level — survives across tool calls within process)
# ---------------------------------------------------------------------------

_proactive_state: dict[str, int | bool] = {
    "remember_count": 0,
    "last_reflect_at": 0,
    "enabled": True,
}


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
    relations: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Optional relations to create in the same call. "
            "Format: {\"REL_TYPE\": [\"target_name\", ...]}. "
            'Example: {"TEACHES": ["Java"], "IN_DOMAIN": ["teaching"], "FOR": ["Accenture"]}. '
            "Target nodes are found by name; if missing, stub nodes are created."
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


class IngestInput(BaseModel):
    """Input for ``engrama_ingest``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    source: str = Field(
        ...,
        description=(
            "Content to ingest. Either a vault-relative path to a note "
            "(e.g. '10-projects/websocket-debugging.md') or raw text content."
        ),
    )
    source_type: str = Field(
        default="note",
        description=(
            "Type of source: 'note' (vault path — will be read), "
            "'text' (raw text content), or 'conversation' (conversation transcript)."
        ),
    )
    context_hint: str = Field(
        default="",
        description=(
            "Optional hint about the context (e.g. project name, domain, course). "
            "Helps guide entity extraction."
        ),
    )


class SurfaceInput(BaseModel):
    """Input for engrama_surface_insights."""
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(
        default=10,
        description="Maximum number of pending Insights to return.",
    )


class ApproveInput(BaseModel):
    """Input for engrama_approve_insight."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Exact title of the Insight.")
    action: str = Field(
        default="approve",
        description="'approve' or 'dismiss'.",
    )


class WriteInsightInput(BaseModel):
    """Input for engrama_write_insight_to_vault."""
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="Exact title of the approved Insight.")
    target_note: str = Field(
        description="Relative path to the Obsidian note to append to.",
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
    #
    # DDR-003 Phase A: the lifespan also creates a sync graph store via the
    # backend factory.  MCP tools continue using the async driver directly
    # (Phase A is extraction, not rewrite), but the store is available in
    # the context for gradual migration in later phases.

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

        # DDR-003: create graph store via factory (sync, for engine/skills)
        graph_store = None
        try:
            from engrama.backends import create_stores, create_embedding_provider
            config = {
                "GRAPH_BACKEND": "neo4j",
                "NEO4J_URI": db_url,
                "NEO4J_USERNAME": username,
                "NEO4J_PASSWORD": password,
            }
            graph_store, vector_store = create_stores(config)
            embedder = create_embedding_provider()
            logger.info("Backend factory: graph=%r, vector=%r, embedder=%r",
                        graph_store, vector_store, embedder)
        except Exception as e:
            logger.warning("Backend factory failed (non-fatal): %s", e)
            graph_store = None

        try:
            await driver.verify_connectivity()
            logger.info("Engrama MCP connected to Neo4j at %s", db_url)
            yield {
                "driver": driver,
                "database": database,
                "obsidian": obsidian,
                "parser": NoteParser(),
                # DDR-003 Phase A — protocol-based stores (for gradual migration)
                "graph_store": graph_store,
            }
        finally:
            if graph_store is not None:
                try:
                    graph_store.close()
                except Exception:
                    pass
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
        """Search the memory graph.  Use this at the START of every session
        to load context relevant to the current topic, and whenever you need
        to check whether a node already exists before creating it.

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

        # --- Proactivity: check for pending Insights related to search ---
        related_insights: list[dict[str, Any]] = []
        if _proactive_state.get("enabled", True):
            try:
                insight_records, _, _ = await driver.execute_query(
                    'CALL db.index.fulltext.queryNodes("memory_search", $query) '
                    "YIELD node, score "
                    "WHERE 'Insight' IN labels(node) AND node.status = 'pending' "
                    "RETURN node.title AS title, node.body AS body, "
                    "node.confidence AS confidence, score "
                    "ORDER BY score DESC LIMIT 3",
                    parameters_={"query": params.query},
                    database_=db,
                )
                related_insights = [dict(r) for r in insight_records]
            except Exception:
                pass

        response: dict[str, Any] = {"results": results}
        if related_insights:
            response["pending_insights"] = related_insights
            response["proactive_hint"] = (
                "There are pending Insights related to your search. "
                "Consider presenting them to the user with engrama_surface_insights."
            )
        return json.dumps(response, default=str, indent=2)

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
        """Store a piece of knowledge as a node in the memory graph.  Use this
        whenever you learn something new, solve a problem, or encounter an
        important entity (person, technology, project, etc.) that is not yet
        in the graph.  **Immediately after calling this, call engrama_relate**
        to connect the new node to its context — isolated nodes are much less
        useful.

        If a node with the same ``name`` (or ``title``) already exists, its
        properties are updated (MERGE semantics).

        When a vault is configured, a corresponding .md note is created (or
        updated) with full YAML frontmatter including engrama_id and an empty
        relations block (DDR-002).
        """
        import re as _re

        label = params.label
        props = dict(params.properties)

        # Extract relations before MERGE — Neo4j can't store dicts as properties.
        # Relations may arrive via the top-level `relations` field OR nested
        # inside `properties`; merge both sources so either path works.
        inline_relations: dict[str, list[str]] = props.pop("relations", {}) or {}
        if not isinstance(inline_relations, dict):
            inline_relations = {}

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

        # --- Vault note creation (BUG-002 / DDR-002) ---
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        vault_path: str | None = None
        engrama_id: str | None = None

        if obsidian is not None:
            # Generate a filesystem-safe slug from the node name
            slug = _re.sub(r"[^\w\s-]", "", merge_value.lower())
            slug = _re.sub(r"[\s]+", "-", slug).strip("-")
            vault_path = f"{slug}.md"

            # Check if a note already exists for this node (by obsidian_path
            # stored on an existing graph node, or by filename)
            note_data = obsidian.read_note(vault_path)
            if note_data["success"]:
                # Note exists — read its engrama_id
                engrama_id = note_data["frontmatter"].get("engrama_id")

            if not engrama_id:
                engrama_id = str(uuid.uuid4())

            # Build frontmatter for the note
            fm: dict[str, Any] = {"engrama_id": engrama_id, "type": label}
            # Add all user-provided properties
            for k, v in props.items():
                if k not in ("created_at", "updated_at"):
                    fm[k] = v
            # Ensure relations block exists (DDR-002)
            if "relations" not in fm:
                fm["relations"] = {}

            try:
                import yaml as _yaml
                fm_yaml = _yaml.dump(
                    fm, default_flow_style=False,
                    allow_unicode=True, sort_keys=False,
                )
                target = obsidian._resolve(vault_path)

                if note_data["success"]:
                    # Update existing note — replace frontmatter, keep body
                    content = note_data["content"]
                    if content.startswith("---"):
                        end_idx = content.index("---", 3)
                        body = content[end_idx + 3:]
                    else:
                        body = "\n\n" + content
                    new_content = "---\n" + fm_yaml + "---" + body
                else:
                    # Create new note
                    new_content = (
                        "---\n" + fm_yaml + "---\n\n"
                        f"# {merge_value}\n"
                    )
                    # Add notes/description as body text if present
                    desc = props.get("notes") or props.get("description")
                    if desc:
                        new_content += f"\n> {desc}\n"

                target.write_text(new_content, encoding="utf-8")
                logger.info("Vault note written: %s", vault_path)
            except Exception as e:
                logger.warning("Could not write vault note for %s: %s", merge_value, e)
                vault_path = None

        # --- Graph write ---
        # Include obsidian metadata in node properties
        if vault_path:
            props["obsidian_path"] = vault_path
        if engrama_id:
            props["obsidian_id"] = engrama_id

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

        # --- BUG-005: Process inline relations ---
        # Merge both sources: top-level params.relations + extracted from properties
        all_relations: dict[str, list[str]] = {}
        for src in (params.relations, inline_relations):
            for rtype, targets in (src or {}).items():
                merged = all_relations.setdefault(rtype, [])
                for t in (targets if isinstance(targets, list) else [targets]):
                    if t not in merged:
                        merged.append(t)

        relations_created = 0
        if all_relations:
            from engrama.adapters.obsidian.sync import ObsidianSync

            for rel_type, targets in all_relations.items():
                rel_type_upper = rel_type.upper()
                if rel_type_upper not in _VALID_RELATIONS:
                    logger.warning("Skipping unknown relation type: %s", rel_type)
                    continue

                for target_name in targets:
                    # Find or create the target node
                    target_label = None
                    try:
                        lookup_records, _, _ = await driver.execute_query(
                            "MATCH (n) WHERE toLower(n.name) = toLower($name) "
                            "RETURN labels(n)[0] AS label LIMIT 1",
                            parameters_={"name": target_name},
                            database_=db,
                        )
                        if lookup_records:
                            target_label = lookup_records[0]["label"]
                    except Exception:
                        pass

                    if target_label is None:
                        # Infer label from relation type and create stub
                        target_label = ObsidianSync._infer_stub_label(rel_type_upper)
                        try:
                            await _async_merge_node(driver, db, target_label, {
                                "name": target_name,
                                "status": "stub",
                            })
                        except Exception as e:
                            logger.warning("Could not create stub %s: %s", target_name, e)
                            continue

                    # Create the relationship
                    try:
                        from_key_r = "title" if label in TITLE_KEYED_LABELS else "name"
                        to_key_r = "title" if target_label in TITLE_KEYED_LABELS else "name"
                        await driver.execute_query(
                            f"MATCH (a:{label} {{{from_key_r}: $from_name}}) "
                            f"MATCH (b:{target_label} {{{to_key_r}: $to_name}}) "
                            f"MERGE (a)-[:{rel_type_upper}]->(b)",
                            parameters_={"from_name": merge_value, "to_name": target_name},
                            database_=db,
                        )
                        relations_created += 1
                    except Exception as e:
                        logger.warning(
                            "Could not create relation %s -[%s]-> %s: %s",
                            merge_value, rel_type_upper, target_name, e,
                        )

                    # Write relation to vault frontmatter (DDR-002)
                    if obsidian is not None and vault_path:
                        try:
                            obsidian.add_relation(vault_path, rel_type_upper, target_name)
                        except Exception:
                            pass

        # --- Proactivity: increment counter and check threshold ---
        result: dict[str, Any] = {
            "status": "ok",
            "label": label,
            "node": node,
            "vault_path": vault_path,
            "engrama_id": engrama_id,
            "relations_created": relations_created,
        }

        if _proactive_state.get("enabled", True):
            _proactive_state["remember_count"] = _proactive_state.get("remember_count", 0) + 1
            since_last = _proactive_state["remember_count"] - _proactive_state.get("last_reflect_at", 0)
            if since_last >= 10:
                result["proactive_hint"] = (
                    f"You've stored {since_last} entities since the last reflect. "
                    "Consider running engrama_reflect to detect cross-domain patterns, "
                    "then engrama_surface_insights to present findings to the user."
                )

        return json.dumps(result, default=str, indent=2)

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
        """Connect two nodes with a typed relationship.  Always call this
        right after engrama_remember to wire the new node into the graph.
        Also use it to record newly discovered connections between existing
        nodes.  Both endpoints must already exist (create them first with
        engrama_remember if needed).

        Returns a confirmation or a message if either node was not found.
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
            f"a.{from_key} AS from_name, b.{to_key} AS to_name, "
            f"a.obsidian_path AS from_obsidian_path"
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

        # DDR-002: dual-write — also record the relation in vault frontmatter
        vault_written = False
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        from_path = r.pop("from_obsidian_path", None)
        if obsidian is not None:
            # If the source node has no vault note yet, create one now
            if not from_path:
                import re as _re
                slug = _re.sub(r"[^\w\s-]", "", params.from_name.lower())
                slug = _re.sub(r"[\s]+", "-", slug).strip("-")
                from_path = f"{slug}.md"
                note_data = obsidian.read_note(from_path)
                if not note_data["success"]:
                    # Create a minimal vault note for this node
                    try:
                        import yaml as _yaml
                        _eid = str(uuid.uuid4())
                        fm = {
                            "engrama_id": _eid,
                            "type": params.from_label,
                            "name": params.from_name,
                            "relations": {},
                        }
                        fm_yaml = _yaml.dump(
                            fm, default_flow_style=False,
                            allow_unicode=True, sort_keys=False,
                        )
                        target_file = obsidian._resolve(from_path)
                        target_file.write_text(
                            "---\n" + fm_yaml + "---\n\n"
                            f"# {params.from_name}\n",
                            encoding="utf-8",
                        )
                        # Update the graph node with obsidian metadata
                        from_key_r = "title" if params.from_label in TITLE_KEYED_LABELS else "name"
                        await driver.execute_query(
                            f"MATCH (n:{params.from_label} {{{from_key_r}: $name}}) "
                            "SET n.obsidian_path = $path, n.obsidian_id = $eid",
                            parameters_={"name": params.from_name, "path": from_path, "eid": _eid},
                            database_=db,
                        )
                    except Exception as e:
                        logger.warning("Could not create vault note for %s: %s", params.from_name, e)
                        from_path = None

            if from_path:
                try:
                    vault_written = obsidian.add_relation(
                        from_path, params.rel_type, params.to_name,
                    )
                except Exception as e:
                    logger.warning(
                        "DDR-002 vault write failed for %s -[%s]-> %s: %s",
                        params.from_name, params.rel_type, params.to_name, e,
                    )

        return json.dumps(
            {"status": "ok", **r, "vault_written": vault_written},
            default=str, indent=2,
        )

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
        """Retrieve a node and its neighbourhood up to N hops.  Use this to
        build rich context before answering a question — e.g. fetch the
        user's Person node to see their projects, or a Technology node to
        see what it connects to.  Works even for isolated nodes (returns the
        node with an empty neighbours list).
        """
        if params.label not in _VALID_LABELS:
            return f"Error: Invalid label '{params.label}'."

        merge_key = "title" if params.label in TITLE_KEYED_LABELS else "name"
        cypher = (
            f"MATCH (start:{params.label} {{{merge_key}: $name}}) "
            f"OPTIONAL MATCH (start)-[r*1..{params.hops}]-(neighbour) "
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

        # Format: start node + deduplicated list of neighbours
        start_node = dict(records[0]["start"]) if records[0]["start"] else {}
        neighbours = []
        seen: set[tuple[str, str]] = set()
        root_name = start_node.get("name") or start_node.get("title")
        root_key = (params.label, root_name)
        for r in records:
            nname = r["neighbour_name"]
            nlabel = r["neighbour_label"]
            if nname and (nlabel, nname) not in seen and (nlabel, nname) != root_key:
                seen.add((nlabel, nname))
                neighbours.append({
                    "label": nlabel,
                    "name": nname,
                    "via": list(dict.fromkeys(r["rel_types"])),
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

        # 5. DDR-002: merge frontmatter relations into Neo4j
        fm_relations_merged = 0
        stubs_created_count = 0
        if parsed.relations:
            for rel_type, targets in parsed.relations.items():
                for target_name in targets:
                    # Look up target label in graph
                    target_label = None
                    try:
                        lookup_records, _, _ = await driver.execute_query(
                            "MATCH (n) WHERE toLower(n.name) = toLower($name) "
                            "RETURN labels(n)[0] AS label LIMIT 1",
                            parameters_={"name": target_name},
                            database_=db,
                        )
                        if lookup_records:
                            target_label = lookup_records[0]["label"]
                    except Exception:
                        pass

                    if target_label is None:
                        # Create stub node
                        from engrama.adapters.obsidian.sync import ObsidianSync
                        target_label = ObsidianSync._infer_stub_label(rel_type)
                        try:
                            await _async_merge_node(driver, db, target_label, {
                                "name": target_name,
                                "status": "stub",
                            })
                            stubs_created_count += 1
                        except Exception:
                            continue

                    # Merge the typed relation
                    try:
                        from_key = "title" if parsed.label in TITLE_KEYED_LABELS else "name"
                        to_key = "title" if target_label in TITLE_KEYED_LABELS else "name"
                        async with driver.session(database=db) as session:
                            await session.run(
                                f"MATCH (a:{parsed.label} {{{from_key}: $from_name}}) "
                                f"MATCH (b:{target_label} {{{to_key}: $to_name}}) "
                                f"MERGE (a)-[:{rel_type}]->(b)",
                                {
                                    "from_name": parsed.name,
                                    "to_name": target_name,
                                },
                            )
                        fm_relations_merged += 1
                    except Exception:
                        pass

        return json.dumps({
            "status": "ok",
            "label": parsed.label,
            "name": parsed.name,
            "engrama_id": engrama_id,
            "created_or_updated": "created" if merge_result["created"] else "updated",
            "frontmatter_relations": fm_relations_merged,
            "stubs_created": stubs_created_count,
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

        # Pass 1: parse all notes and merge nodes
        parsed_notes: list = []
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
            parsed_notes.append(parsed)

        # Pass 2: resolve wiki-links → create LINKS_TO relations
        from pathlib import Path as _Path
        stem_to_node: dict[str, tuple[str, str]] = {}
        for pn in parsed_notes:
            stem = _Path(pn.path).stem.lower()
            stem_to_node[stem] = (pn.label, pn.name)
            stem_to_node[pn.name.lower()] = (pn.label, pn.name)

        relations_created = 0
        for pn in parsed_notes:
            for link_target in pn.wiki_links:
                target_key = link_target.strip().lower()
                if target_key in stem_to_node:
                    target_label, target_name = stem_to_node[target_key]
                    if target_name == pn.name:
                        continue
                    try:
                        async with driver.session(database=db) as session:
                            await session.run(
                                "MATCH (a {name: $from_name}) "
                                "WHERE $from_label IN labels(a) "
                                "MATCH (b {name: $to_name}) "
                                "WHERE $to_label IN labels(b) "
                                "MERGE (a)-[:LINKS_TO]->(b)",
                                {
                                    "from_name": pn.name,
                                    "from_label": pn.label,
                                    "to_name": target_name,
                                    "to_label": target_label,
                                },
                            )
                        relations_created += 1
                    except Exception:
                        pass  # skip unresolvable links

        # Pass 3 (DDR-002): merge frontmatter relations into Neo4j
        # Build a name→label lookup from all parsed notes
        known_nodes: dict[str, str] = {
            pn.name.lower(): pn.label for pn in parsed_notes
        }
        fm_relations = 0
        stubs_created_count = 0

        for pn in parsed_notes:
            if not pn.relations:
                continue
            for rel_type, targets in pn.relations.items():
                for target_name in targets:
                    target_lower = target_name.lower()

                    # Resolve target label
                    if target_lower in known_nodes:
                        target_label = known_nodes[target_lower]
                    else:
                        # Look up in graph
                        try:
                            lookup_records, _, _ = await driver.execute_query(
                                "MATCH (n) WHERE toLower(n.name) = toLower($name) "
                                "RETURN labels(n)[0] AS label LIMIT 1",
                                parameters_={"name": target_name},
                                database_=db,
                            )
                            target_label = lookup_records[0]["label"] if lookup_records else None
                        except Exception:
                            target_label = None

                    if target_label is None:
                        # Create stub node (DDR-002)
                        from engrama.adapters.obsidian.sync import ObsidianSync
                        target_label = ObsidianSync._infer_stub_label(rel_type)
                        try:
                            await _async_merge_node(driver, db, target_label, {
                                "name": target_name,
                                "status": "stub",
                            })
                            stubs_created_count += 1
                            known_nodes[target_lower] = target_label
                        except Exception:
                            continue

                    # Merge the typed relation
                    try:
                        from_key = "title" if pn.label in TITLE_KEYED_LABELS else "name"
                        to_key = "title" if target_label in TITLE_KEYED_LABELS else "name"
                        async with driver.session(database=db) as session:
                            await session.run(
                                f"MATCH (a:{pn.label} {{{from_key}: $from_name}}) "
                                f"MATCH (b:{target_label} {{{to_key}: $to_name}}) "
                                f"MERGE (a)-[:{rel_type}]->(b)",
                                {
                                    "from_name": pn.name,
                                    "to_name": target_name,
                                },
                            )
                        fm_relations += 1
                    except Exception:
                        pass

        return json.dumps({
            "status": "ok",
            "created": created,
            "updated": updated,
            "skipped": skipped,
            "relations": relations_created,
            "frontmatter_relations": fm_relations,
            "stubs_created": stubs_created_count,
        }, indent=2)

    # -- Tool: engrama_ingest ---------------------------------------------

    @mcp.tool(
        name="engrama_ingest",
        annotations=ToolAnnotations(
            title="Ingest (Extract Knowledge from Content)",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_ingest(params: IngestInput, ctx: Context) -> str:
        """Read a document, conversation, or text and return its content with
        entity extraction guidance.  After calling this tool, use the returned
        content and extraction prompt to identify entities and relationships,
        then call ``engrama_remember`` with inline relations for each entity.

        This is the primary way to populate the graph from existing content.
        The tool reads and prepares — YOU (the agent) extract and store.

        Workflow:
        1. Call ``engrama_ingest(source, source_type)``
        2. Read the returned content and extraction guidance
        3. Identify entities (Problems, Technologies, Decisions, Concepts, etc.)
        4. For each entity, call ``engrama_remember`` with inline relations
        5. Report what was extracted to the user
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        driver, db = _driver_and_db(ctx)

        # --- Read source content ---
        content: str = ""
        source_path: str | None = None

        if params.source_type == "note":
            if obsidian is None:
                return json.dumps({
                    "status": "error",
                    "message": "No Obsidian vault configured. Cannot read notes.",
                })
            note_data = obsidian.read_note(params.source)
            if not note_data["success"]:
                return json.dumps({
                    "status": "error",
                    "message": f"Note not found: {params.source}",
                })
            content = note_data["content"]
            source_path = params.source
        elif params.source_type in ("text", "conversation"):
            content = params.source
        else:
            return json.dumps({
                "status": "error",
                "message": f"Unknown source_type: {params.source_type}. "
                           "Use 'note', 'text', or 'conversation'.",
            })

        if not content.strip():
            return json.dumps({
                "status": "error",
                "message": "Source content is empty.",
            })

        # --- Query graph for existing nodes to help deduplication ---
        existing_nodes: list[dict[str, str]] = []
        try:
            records, _, _ = await driver.execute_query(
                "MATCH (n) WHERE n.name IS NOT NULL OR n.title IS NOT NULL "
                "RETURN labels(n)[0] AS label, "
                "coalesce(n.name, n.title) AS name "
                "ORDER BY name LIMIT 200",
                database_=db,
            )
            existing_nodes = [{"label": r["label"], "name": r["name"]} for r in records]
        except Exception:
            pass

        # --- Build extraction guidance ---
        valid_labels_str = ", ".join(sorted(_VALID_LABELS))
        valid_rels_str = ", ".join(sorted(_VALID_RELATIONS))

        existing_summary = ""
        if existing_nodes:
            by_label: dict[str, list[str]] = {}
            for n in existing_nodes:
                by_label.setdefault(n["label"], []).append(n["name"])
            parts = []
            for label, names in sorted(by_label.items()):
                sample = names[:10]
                suffix = f" (+{len(names) - 10} more)" if len(names) > 10 else ""
                parts.append(f"  {label}: {', '.join(sample)}{suffix}")
            existing_summary = (
                "\n\nExisting nodes in graph (reuse these, don't create duplicates):\n"
                + "\n".join(parts)
            )

        context_line = ""
        if params.context_hint:
            context_line = f"\nContext hint from user: {params.context_hint}\n"

        extraction_prompt = (
            "## Entity extraction guidance\n\n"
            "Analyse the content above and extract all relevant entities and "
            "relationships. For each entity, call `engrama_remember` with inline relations.\n\n"
            "### Valid node labels\n"
            f"{valid_labels_str}\n\n"
            "### Valid relationship types\n"
            f"{valid_rels_str}\n\n"
            "### Extraction rules\n"
            "1. **Search before creating** — check the existing nodes list below. "
            "If an entity already exists, skip it or update it.\n"
            "2. **Every entity needs at minimum**: BELONGS_TO (project/client/course) "
            "and IN_DOMAIN (domain).\n"
            "3. **For Problems, Decisions, Vulnerabilities**: also add INSTANCE_OF → Concept.\n"
            "4. **Concepts must be project-agnostic**: 'sql-injection' yes, 'ticket-42-bug' no.\n"
            "5. **Technologies are reusable**: 'Python', 'Neo4j', 'Docker' — search first.\n"
            "6. **Use inline relations** in engrama_remember to create the node AND "
            "its relationships in a single call.\n"
            f"{context_line}"
            f"{existing_summary}\n\n"
            "### Expected output\n"
            "For each entity you identify, call:\n"
            "```\n"
            "engrama_remember(label=\"...\", properties={...}, relations={...})\n"
            "```\n"
            "After all entities are stored, report a summary to the user: "
            "what was extracted, how many entities and relationships were created."
        )

        return json.dumps({
            "status": "ok",
            "source_type": params.source_type,
            "source_path": source_path,
            "content_length": len(content),
            "content": content,
            "extraction_prompt": extraction_prompt,
        }, default=str, indent=2)

    # -- Tool: engrama_reflect --------------------------------------------

    from engrama.skills.reflect import (
        _QUERY_CROSS_PROJECT_SOLUTION,
        _QUERY_SHARED_TECHNOLOGY,
        _QUERY_TRAINING_OPPORTUNITY,
        _QUERY_TECHNIQUE_TRANSFER,
        _QUERY_CONCEPT_CLUSTERING,
        _QUERY_STALE_KNOWLEDGE,
        _QUERY_UNDER_CONNECTED,
        _QUERY_GRAPH_PROFILE,
    )

    @mcp.tool(
        name="engrama_reflect",
        annotations=ToolAnnotations(
            title="Reflect (Adaptive Pattern Detection)",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_reflect(ctx: Context) -> str:
        """Detect cross-entity patterns in the memory graph.  Call this
        periodically (e.g. at the end of a session or when the user asks
        for insights) to discover solution transfers between projects,
        shared technologies, training opportunities, technique transfers,
        concept clusters, stale knowledge, and under-connected nodes.

        The reflect skill is **adaptive**: it inspects what's actually in the
        graph and only runs queries that apply to the current content.
        Previously dismissed Insights are never re-surfaced.

        Detected patterns are stored as Insight nodes with status "pending" —
        present them to the user via engrama_surface_insights for review.
        """
        driver, db = _driver_and_db(ctx)
        insights: list[dict[str, Any]] = []
        queries_run: list[str] = []
        queries_skipped: list[str] = []

        # --- Step 1: Profile the graph ---
        profile: dict[str, int] = {}
        try:
            records, _, _ = await driver.execute_query(
                _QUERY_GRAPH_PROFILE, database_=db,
            )
            profile = {r["label"]: r["cnt"] for r in records}
        except Exception as e:
            logger.warning("Could not profile graph: %s", e)

        # --- Step 2: Get dismissed titles ---
        dismissed: set[str] = set()
        try:
            records, _, _ = await driver.execute_query(
                "MATCH (i:Insight {status: 'dismissed'}) "
                "RETURN i.title AS title",
                database_=db,
            )
            dismissed = {r["title"] for r in records}
        except Exception:
            pass

        # --- Helper to run a query and create Insights ---
        async def _run_pattern(
            query_name: str,
            query: str,
            params: dict,
            required_labels: list[str] | None = None,
            any_labels: list[list[str]] | None = None,
            min_label_count: dict[str, int] | None = None,
            builder_fn=None,
        ):
            # Check if ALL required labels have data
            for label in (required_labels or []):
                if not profile.get(label):
                    queries_skipped.append(query_name)
                    return
            # Check any_labels: each entry is an OR-group — at least one must exist
            for group in (any_labels or []):
                if not any(profile.get(l) for l in group):
                    queries_skipped.append(query_name)
                    return
            if min_label_count:
                for label, min_cnt in min_label_count.items():
                    if profile.get(label, 0) < min_cnt:
                        queries_skipped.append(query_name)
                        return

            queries_run.append(query_name)
            try:
                records, _, _ = await driver.execute_query(
                    query, parameters_=params, database_=db,
                )
            except Exception as e:
                logger.warning("Reflect query %s failed: %s", query_name, e)
                return

            if builder_fn:
                await builder_fn(records)

        # --- Query builders ---

        async def _build_cross_project(records):
            for r in records:
                title = (
                    f"Solution transfer: {r['decision']} "
                    f"({r['source_project']} → {r['target_project']})"
                )
                if title in dismissed:
                    continue
                body = (
                    f"The open problem \"{r['open_problem']}\" in project "
                    f"\"{r['target_project']}\" shares the concept "
                    f"\"{r['concept']}\" with a resolved problem in project "
                    f"\"{r['source_project']}\". The decision "
                    f"\"{r['decision']}\" may apply here."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": 0.85,
                    "status": "pending", "source_query": "cross_project_solution",
                })
                insights.append({"query": "cross_project_solution",
                                 "title": title, "confidence": 0.85})

        async def _build_shared_tech(records):
            for r in records:
                a_desc = f"{r['type_a']}:{r['entity_a']}"
                b_desc = f"{r['type_b']}:{r['entity_b']}"
                title = (
                    f"Shared technology: {r['technology']} "
                    f"({a_desc} & {b_desc})"
                )
                if title in dismissed:
                    continue
                confidence = 0.75 if r["type_a"] != r["type_b"] else 0.6
                body = (
                    f"{a_desc} and {b_desc} both use {r['technology']}. "
                    f"Consider sharing knowledge or materials between them."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": confidence,
                    "status": "pending", "source_query": "shared_technology",
                })
                insights.append({"query": "shared_technology",
                                 "title": title, "confidence": confidence})

        async def _build_training(records):
            for r in records:
                issue_desc = f"{r['issue_type']}:{r['issue']}"
                title = (
                    f"Training opportunity: {r['course']} "
                    f"covers {r['concept']} (relates to: {issue_desc})"
                )
                if title in dismissed:
                    continue
                body = (
                    f"The {r['issue_type'].lower()} \"{r['issue']}\" involves "
                    f"the concept \"{r['concept']}\", which is covered by the "
                    f"course \"{r['course']}\". Reviewing this material may help."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": 0.65,
                    "status": "pending", "source_query": "training_opportunity",
                })
                insights.append({"query": "training_opportunity",
                                 "title": title, "confidence": 0.65})

        async def _build_technique_transfer(records):
            for r in records:
                title = (
                    f"Technique transfer: {r['technique']} "
                    f"({r['source_domain']} → {r['target_domain']})"
                )
                if title in dismissed:
                    continue
                related = r["related_entities"]
                confidence = min(0.5 + (related * 0.1), 0.9)
                body = (
                    f"The technique \"{r['technique']}\" is used in "
                    f"\"{r['source_domain']}\" but not in "
                    f"\"{r['target_domain']}\". There are {related} "
                    f"entities in {r['target_domain']} sharing concepts "
                    f"with this technique."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": confidence,
                    "status": "pending", "source_query": "technique_transfer",
                })
                insights.append({"query": "technique_transfer",
                                 "title": title, "confidence": confidence})

        async def _build_concept_clustering(records):
            for r in records:
                concept = r["concept"]
                count = r["entity_count"]
                sample = r["sample"]
                title = f"Concept cluster: {concept} ({count} entities)"
                if title in dismissed:
                    continue
                sample_desc = ", ".join(
                    f"{s['label']}:{s['name']}" for s in (sample or [])[:5]
                )
                confidence = min(0.5 + (count * 0.05), 0.9)
                body = (
                    f"The concept \"{concept}\" connects {count} entities: "
                    f"{sample_desc}. This cluster may reveal a pattern."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": confidence,
                    "status": "pending", "source_query": "concept_clustering",
                })
                insights.append({"query": "concept_clustering",
                                 "title": title, "confidence": confidence})

        async def _build_stale(records):
            for r in records:
                name = r["name"]
                title = (
                    f"Stale knowledge: {r['label']}:{name} "
                    f"(linked to {r['project']})"
                )
                if title in dismissed:
                    continue
                last_updated = r["last_updated"]
                if hasattr(last_updated, "isoformat"):
                    last_updated = last_updated.isoformat()[:10]
                body = (
                    f"The {r['label']} \"{name}\" is connected to the active "
                    f"project \"{r['project']}\" via {r['rel']}, but hasn't been "
                    f"updated since {last_updated}. Consider reviewing or archiving."
                )
                await _async_merge_node(driver, db, "Insight", {
                    "title": title, "body": body, "confidence": 0.5,
                    "status": "pending", "source_query": "stale_knowledge",
                })
                insights.append({"query": "stale_knowledge",
                                 "title": title, "confidence": 0.5})

        async def _build_under_connected(records):
            if not records:
                return
            names = [f"{r['label']}:{r['name']}" for r in records[:10]]
            total = len(records)
            title = f"Under-connected nodes: {total} nodes with <2 relationships"
            if title in dismissed:
                return
            body = (
                f"Found {total} nodes with fewer than 2 relationships. "
                f"Candidates for enrichment: {', '.join(names)}."
            )
            await _async_merge_node(driver, db, "Insight", {
                "title": title, "body": body, "confidence": 0.4,
                "status": "pending", "source_query": "under_connected",
            })
            insights.append({"query": "under_connected",
                             "title": title, "confidence": 0.4})

        # --- Step 3: Run applicable patterns ---
        await _run_pattern(
            "cross_project_solution", _QUERY_CROSS_PROJECT_SOLUTION,
            {"open_status": "open", "resolved_status": "resolved"},
            required_labels=["Problem", "Project"],
            builder_fn=_build_cross_project,
        )
        await _run_pattern(
            "shared_technology", _QUERY_SHARED_TECHNOLOGY,
            {},
            required_labels=["Technology"],
            builder_fn=_build_shared_tech,
        )
        await _run_pattern(
            "training_opportunity", _QUERY_TRAINING_OPPORTUNITY,
            {"open_status": "open"},
            any_labels=[["Problem", "Vulnerability"], ["Course"]],
            builder_fn=_build_training,
        )
        await _run_pattern(
            "technique_transfer", _QUERY_TECHNIQUE_TRANSFER, {},
            required_labels=["Technique"],
            min_label_count={"Domain": 2},
            builder_fn=_build_technique_transfer,
        )
        await _run_pattern(
            "concept_clustering", _QUERY_CONCEPT_CLUSTERING, {},
            required_labels=["Concept"],
            builder_fn=_build_concept_clustering,
        )
        await _run_pattern(
            "stale_knowledge", _QUERY_STALE_KNOWLEDGE,
            {"active_status": "active"},
            any_labels=[["Project", "Course"]],
            builder_fn=_build_stale,
        )

        # Under-connected: always run if enough nodes
        total_nodes = sum(profile.values())
        if total_nodes >= 5:
            queries_run.append("under_connected")
            try:
                uc_records, _, _ = await driver.execute_query(
                    _QUERY_UNDER_CONNECTED, database_=db,
                )
                await _build_under_connected(uc_records)
            except Exception as e:
                logger.warning("Under-connected query failed: %s", e)
        else:
            queries_skipped.append("under_connected")

        # --- Proactivity: reset counter ---
        _proactive_state["last_reflect_at"] = _proactive_state.get("remember_count", 0)

        return json.dumps({
            "status": "ok",
            "graph_profile": profile,
            "queries_run": queries_run,
            "queries_skipped": queries_skipped,
            "dismissed_count": len(dismissed),
            "insights_count": len(insights),
            "insights": insights,
        }, default=str, indent=2)

    # -- Tool: engrama_surface_insights ------------------------------------

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
        """Retrieve pending Insights for human review.  Call this after
        engrama_reflect, or periodically, to show the user patterns the
        graph has detected.  Present each Insight and ask the user to
        approve or dismiss it — never act on an Insight without explicit
        approval.
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

    # -- Prompt: engrama_session_guide --------------------------------------

    @mcp.prompt("engrama_session_guide")
    def engrama_session_guide() -> str:
        """Full taxonomy and session protocol for the Engrama memory graph."""
        return (
            "# Engrama — Memory Graph Session Guide\n"
            "\n"
            "You have access to the Engrama memory graph.  Follow this protocol\n"
            "to build and maintain the user's long-term knowledge base.\n"
            "\n"
            "## Session protocol\n"
            "\n"
            "1. **On session start** — call `engrama_search` with keywords relevant\n"
            "   to the current topic.  Load context with `engrama_context` on the\n"
            "   most important results.  This lets you ground your responses in\n"
            "   what the user already knows and has done.\n"
            "\n"
            "2. **When learning something new** — call `engrama_remember` to store\n"
            "   the entity, then *immediately* call `engrama_relate` to connect it\n"
            "   to existing nodes.  Isolated nodes are nearly invisible to pattern\n"
            "   detection — always wire new knowledge into the graph.\n"
            "\n"
            "3. **Proactive node creation** — if you detect that an important entity\n"
            "   is missing (the user's Person node, a Technology being discussed, a\n"
            "   Project being worked on), create it without waiting to be asked.\n"
            "\n"
            "4. **Periodically** — call `engrama_reflect` to run pattern detection,\n"
            "   then `engrama_surface_insights` to review results with the user.\n"
            "\n"
            "## Node labels — when to use each\n"
            "\n"
            "| Label | Use for | Merge key |\n"
            "|-------|---------|----------|\n"
            "| Person | People — the user, colleagues, contacts | name |\n"
            "| Project | Products, repos, major initiatives | name |\n"
            "| Technology | Languages, frameworks, infra components | name |\n"
            "| Concept | Ideas, knowledge areas, domains | name |\n"
            "| Decision | Decisions with rationale (title-keyed) | title |\n"
            "| Problem | Challenges, blockers, bugs (title-keyed) | title |\n"
            "| Tool | Security tools, scanners, utilities | name |\n"
            "| Technique | Attack techniques (MITRE ATT&CK) | name |\n"
            "| Target | Machines, networks under assessment | name |\n"
            "| Vulnerability | CVEs, misconfigs found (title-keyed) | title |\n"
            "| CTF | CTF challenges, HackTheBox machines | name |\n"
            "| Course | Training courses delivered | name |\n"
            "| Client | Organisations commissioning training | name |\n"
            "| Exercise | Hands-on labs, practicals (title-keyed) | title |\n"
            "| Photo | Photographs, sessions (title-keyed) | title |\n"
            "| Location | Geographic locations, birding spots | name |\n"
            "| Species | Birds, mammals, insects, plants | name |\n"
            "| Gear | Camera bodies, lenses, equipment | name |\n"
            "| Model | AI/ML models — LLMs, classifiers | name |\n"
            "| Dataset | Training/evaluation datasets | name |\n"
            "| Experiment | ML experiments, evaluation runs (title-keyed) | title |\n"
            "| Pipeline | Data/ML pipelines | name |\n"
            "| Insight | Cross-entity patterns (created by reflect) | title |\n"
            "\n"
            "## Relationship types — which to use between which labels\n"
            "\n"
            "| rel_type | Typical from → to | Meaning |\n"
            "|----------|-------------------|--------|\n"
            "| USES | Project → Technology | Project uses a technology |\n"
            "| INFORMED_BY | Decision → Concept | Decision informed by a concept |\n"
            "| HAS | Project → Problem, Project → Decision | Project has a problem/decision |\n"
            "| APPLIES | Project → Concept | Project applies a concept |\n"
            "| SOLVED_BY | Problem → Decision | Problem resolved by a decision |\n"
            "| INVOLVES | Problem → Concept | Problem involves a concept |\n"
            "| IMPLEMENTS | Project → Concept | Project implements a concept |\n"
            "| EXPLOITS | Technique → Vulnerability | Technique exploits a vuln |\n"
            "| EXECUTED_WITH | Technique → Tool | Technique executed with a tool |\n"
            "| TARGETS | Vulnerability → Target | Vuln found on a target |\n"
            "| DOCUMENTS | CTF → Target | CTF documents a target |\n"
            "| COVERS | Course → Concept | Course covers a concept |\n"
            "| TEACHES | Person → Course | Person teaches a course |\n"
            "| FOR | Course → Client | Course delivered for a client |\n"
            "| INCLUDES | Course → Exercise | Course includes an exercise |\n"
            "| PRACTICES | Exercise → Concept | Exercise practises a concept |\n"
            "| REQUIRES | Exercise → Technology | Exercise requires a technology |\n"
            "| TAKEN_AT | Photo → Location | Photo taken at a location |\n"
            "| FEATURES | Photo → Species | Photo features a species |\n"
            "| SHOT_WITH | Photo → Gear | Photo shot with gear |\n"
            "| INHABITS | Species → Location | Species inhabits a location |\n"
            "| ORIGIN_OF | Location → Species | Location is origin of species |\n"
            "| TRAINS_ON | Model → Dataset | Model trained on a dataset |\n"
            "| RUNS | Pipeline → Model | Pipeline runs a model |\n"
            "| EVALUATES | Experiment → Model | Experiment evaluates a model |\n"
            "| FEEDS | Dataset → Pipeline | Dataset feeds a pipeline |\n"
            "\n"
            "## Tips\n"
            "\n"
            "- Use `engrama_search` before `engrama_remember` to avoid duplicates.\n"
            "- Title-keyed labels (Decision, Problem, Vulnerability, Exercise,\n"
            "  Photo, Experiment) use `title` instead of `name` as the merge key.\n"
            "- When the user mentions a person, technology, or project for the\n"
            "  first time, create the node proactively.\n"
            "- Keep node properties concise — the graph is for structure and\n"
            "  connections, not full documents.\n"
        )

    return mcp
