"""
Engrama MCP server — high-level memory tools for AI agents.

Exposes four tools via the Model Context Protocol:

* **engrama_search** — fulltext search across the memory graph.
* **engrama_remember** — create or update a node (always MERGE).
* **engrama_relate** — create a relationship between two nodes.
* **engrama_context** — retrieve the neighbourhood of a node.

All writes use ``MERGE`` with automatic timestamps.  All queries use
Cypher parameters — never string formatting.

The server uses an **async** Neo4j driver managed through FastMCP's
lifespan hook, so the connection is shared across tool calls and
properly closed on shutdown.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from neo4j import AsyncGraphDatabase, AsyncDriver
from pydantic import BaseModel, ConfigDict, Field

from engrama.core.schema import NodeType, RelationType

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


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_engrama_mcp(
    db_url: str = "bolt://localhost:7687",
    username: str = "neo4j",
    password: str = "",
    database: str = "neo4j",
) -> FastMCP:
    """Create and return a configured Engrama MCP server.

    Parameters:
        db_url: Neo4j bolt URI.
        username: Neo4j username.
        password: Neo4j password.
        database: Neo4j database name.

    Returns:
        A :class:`FastMCP` instance ready to run.
    """

    # -- Lifespan: manage the async Neo4j driver --------------------------

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # noqa: ARG001
        driver: AsyncDriver = AsyncGraphDatabase.driver(
            db_url, auth=(username, password)
        )
        try:
            await driver.verify_connectivity()
            logger.info("Engrama MCP connected to Neo4j at %s", db_url)
            yield {"driver": driver, "database": database}
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

        cypher = (
            f"MATCH (a:{params.from_label} {{name: $from_name}}) "
            f"MATCH (b:{params.to_label} {{name: $to_name}}) "
            f"MERGE (a)-[r:{params.rel_type}]->(b) "
            "RETURN type(r) AS rel_type, a.name AS from_name, b.name AS to_name"
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

    return mcp
