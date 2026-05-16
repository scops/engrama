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

This refactored version uses ``Neo4jAsyncStore`` exclusively — all inline
Cypher has been moved to the async_store backend module.
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
from pydantic import BaseModel, ConfigDict, Field

from engrama.adapters.obsidian import NoteParser, ObsidianAdapter
from engrama.core.schema import TITLE_KEYED_LABELS, NodeType, RelationType
from engrama.core.scope import MemoryScope
from engrama.core.security import Provenance, Sanitiser

logger = logging.getLogger("engrama_mcp")
logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Valid labels / relations (used for validation)
# ---------------------------------------------------------------------------

_VALID_LABELS: set[str] = {member.value for member in NodeType}
_VALID_RELATIONS: set[str] = {member.value for member in RelationType}

# MCP talks to the store directly (it doesn't go through EngramaEngine
# because the server is async-first while the engine is sync), so the
# layer-1 sanitiser has to be applied at this boundary explicitly.
_SANITISER = Sanitiser(valid_labels=_VALID_LABELS, valid_relations=_VALID_RELATIONS)
_MCP_PROVENANCE_PROPS = Provenance(source="mcp").to_properties()

# Process-wide scope for the MCP server — one scope per running
# process, populated at import time from the operator's env vars
# (ENGRAMA_ORG_ID, ENGRAMA_USER_ID, ENGRAMA_AGENT_ID, ENGRAMA_SESSION_ID).
# Empty when nothing is exported, so single-user deployments behave
# exactly as before this PR.
#
# **Stability requirement:** the scope is captured ONCE at module
# import time. Operators must export these env vars *before* the MCP
# process starts (e.g. in the launching shell or the service unit
# definition). Mutating ``os.environ`` after the server is loaded has
# no effect on the live scope — the snapshot is already taken, and
# the helpers below reuse :data:`_MCP_SCOPE_PROPS` directly. Restart
# the MCP server to pick up changes.
_MCP_SCOPE: MemoryScope = MemoryScope.from_env()
_MCP_SCOPE_PROPS: dict[str, Any] = _MCP_SCOPE.to_properties()


def _with_mcp_provenance(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Sanitise an MCP-supplied extras dict and stamp it with MCP metadata.

    The caller's extras are cleaned first (reserved provenance + scope
    keys stripped, control chars removed, long strings truncated) and
    then the system-managed properties are applied — they always win,
    so a malicious agent cannot forge its own ``source``, ``trust_level``
    or scope dimensions.
    """
    cleaned = _SANITISER.sanitise_properties(extra or {})
    return {**cleaned, **_MCP_PROVENANCE_PROPS, **_MCP_SCOPE_PROPS}


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
        description=(f"Node label — one of: {', '.join(sorted(_VALID_LABELS))}."),
    )
    properties: dict[str, Any] = Field(
        ...,
        description=(
            "Node properties. Must include 'name' (or 'title' for Decision/Problem). "
            "For rich retrieval, include enrichment fields:\n"
            "  - 'summary': 2–3 sentence overview — what this is, why it matters\n"
            "  - 'details': comprehensive context — techniques, decisions, "
            "approaches, alternatives, key examples. The richer this field, "
            "the more useful the memory becomes.\n"
            "  - 'tags': freeform list for filtering, e.g. "
            '["active-directory", "credential-access", "windows"]\n'
            "  - 'source': how this knowledge was captured "
            '("conversation", "ingest", "manual", "sync")\n'
            '  - \'status\': current state ("active", "resolved", "superseded")\n'
            "Example of a GOOD node:\n"
            '{"name": "kerberoasting-lab", '
            '"summary": "Hands-on lab demonstrating Kerberoasting against AD service '
            'accounts using Rubeus and Hashcat, with detection via Event 4769.", '
            '"details": "Students request TGS tickets for SPNs with Rubeus kerberoast, '
            "crack offline with Hashcat. Covers RC4 vs AES crackability, detection via "
            'Event 4769 anomalies. Defense: gMSA, AES-only policy, honey tokens.", '
            '"tags": ["active-directory", "credential-access", "lab"], '
            '"source": "conversation"}\n'
            "Example of a BAD (too thin) node: "
            '{"name": "kerberoasting-lab", "description": "Lab about kerberoasting"}'
        ),
    )
    relations: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Optional relations to create in the same call. "
            'Format: {"REL_TYPE": ["target_name", ...]}. '
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
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, do not write to the graph or inject `engrama_id` "
            "into the note's frontmatter. Return the same JSON shape as a "
            "real sync, with a `would_*` view of what the operation would "
            "have done. Useful to preview a sync against a different vault "
            "before committing."
        ),
    )


class SyncVaultInput(BaseModel):
    """Input for ``engrama_sync_vault``."""

    model_config = ConfigDict(str_strip_whitespace=True)

    folder: str = Field(
        default="",
        description="Optional folder to restrict the scan (vault-relative). "
        "Empty string scans the entire vault.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, do not write to the graph or inject `engrama_id` "
            "into any note's frontmatter. Return the same JSON shape as a "
            "real sync, with counts of what *would* be created/updated/"
            "skipped and the list of files that would receive an "
            "`engrama_id` injection. Useful to preview the impact before "
            "committing to a full scan."
        ),
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
    backend: str | None = None,
    config: dict[str, Any] | None = None,
    vault_path: str | None = None,
) -> FastMCP:
    """Create and return a configured Engrama MCP server.

    Parameters:
        backend: Optional ``GRAPH_BACKEND`` override. Defaults to env
            (``sqlite`` if unset).
        config: Optional configuration dict forwarded to
            :func:`engrama.backends.create_async_stores` (recognised
            keys: ``ENGRAMA_DB_PATH``, ``NEO4J_URI``,
            ``NEO4J_USERNAME``, ``NEO4J_PASSWORD``,
            ``NEO4J_DATABASE``, ``EMBEDDING_DIMENSIONS``).
        vault_path: Absolute path to the Obsidian vault root.
            Falls back to ``VAULT_PATH`` env var.

    Returns:
        A :class:`FastMCP` instance ready to run.
    """
    cfg: dict[str, Any] = dict(config or {})
    if backend is not None:
        cfg["GRAPH_BACKEND"] = backend

    @asynccontextmanager
    async def lifespan(server: FastMCP):  # noqa: ARG001
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

        # Create the backend-agnostic async store via the factory.
        from engrama.backends import create_async_stores, create_embedding_provider

        async_store = None
        embedder = None
        startup_error = ""
        try:
            async_store, _ = create_async_stores(cfg)
            embedder = create_embedding_provider()
            logger.info(
                "Async store: %r, embedder: %r (dims=%d)",
                async_store,
                embedder,
                getattr(embedder, "dimensions", 0),
            )
        except Exception as e:
            logger.warning("Store factory failed (non-fatal): %s", e)
            async_store = None
            startup_error = str(e)

        try:
            if async_store is not None:
                health = await async_store.health_check()
                logger.info("Engrama MCP backend ready: %s", health)
            yield {
                "async_store": async_store,
                "obsidian": obsidian,
                "parser": NoteParser(),
                "embedder": embedder,
                "startup_error": startup_error,
            }
        finally:
            if embedder is not None and hasattr(embedder, "aclose"):
                try:
                    await embedder.aclose()
                except Exception:
                    pass
            if async_store is not None and hasattr(async_store, "close"):
                try:
                    await async_store.close()
                except Exception:
                    pass
            logger.info("Engrama MCP shut down cleanly")

    mcp = FastMCP("engrama_mcp", lifespan=lifespan)

    # -- Helper: get async store from context -----

    def _store(ctx: Context) -> Any:
        """Extract the async store from the lifespan state.

        Returned type is the protocol-shaped store; caller doesn't need
        to know whether it's the SQLite or Neo4j implementation.
        """
        state = ctx.request_context.lifespan_context
        store = state.get("async_store")
        if store is None:
            startup_error = state.get("startup_error", "")
            if startup_error:
                raise RuntimeError(f"Async store not initialised: {startup_error}")
            raise RuntimeError("Async store not initialised")
        return store

    # -- Tool: engrama_status -----

    @mcp.tool(
        name="engrama_status",
        annotations=ToolAnnotations(
            title="Engrama Status",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_status(ctx: Context) -> str:
        """Return a snapshot of the running Engrama MCP server's configuration.

        **Call this at session start** when Engrama is one of multiple
        Obsidian-capable MCP servers connected to the agent — the
        response identifies Engrama's own vault path (`VAULT_PATH`),
        so the agent can disambiguate which server "the vault" refers
        to before any sync or ingest call. It is also useful for
        confirming the active backend, embedding model, and whether
        hybrid search is wired up.

        Pure introspection — no graph writes, no embedding calls.

        Returns JSON with this shape (fields may be absent when the
        corresponding subsystem is disabled):

        ```
        {
          "version": "0.10.0",
          "backend": {
            "name": "sqlite" | "neo4j",
            "ok": bool,
            "node_count": int   // present when the backend reports it
          },
          "vault": {
            "configured": bool,
            "path": "/absolute/path/to/engrama/vault",   // when configured
            "note_count": int                            // when configured
          },
          "embedder": {
            "configured": bool,
            "provider": "ollama" | "openai-compatible" | "none" | ...,
            "model": "nomic-embed-text",
            "dimensions": 768
          },
          "search": {
            "mode": "hybrid" | "fulltext_only",
            "degraded": bool,
            "reason": ""   // non-empty when degraded or fulltext-only
          },
          "startup_error": "..."   // present only when something failed at boot
        }
        ```
        """
        state = ctx.request_context.lifespan_context
        store = state.get("async_store")
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        embedder = state.get("embedder")
        startup_error = state.get("startup_error", "")

        # --- Version ---
        from engrama import __version__ as engrama_version

        # --- Backend ---
        # health_check() returns ``sqlite-async`` / ``neo4j-async`` because
        # the MCP server only ever talks to the async flavours. Strip the
        # ``-async`` suffix in the response — agents care about which
        # database is on the other end, not the SDK shape.
        backend_info: dict[str, Any] = {"ok": False}
        if store is not None:
            try:
                h = await store.health_check()
                raw_name = h.get("backend") or ""
                backend_info["name"] = raw_name.removesuffix("-async") if raw_name else None
                backend_info["ok"] = bool(h.get("status") == "ok" or h.get("ok"))
                if "node_count" in h:
                    backend_info["node_count"] = h["node_count"]
            except Exception as e:
                backend_info["error"] = str(e)
        else:
            backend_info["error"] = "store not initialised"

        # --- Vault ---
        vault_info: dict[str, Any] = {"configured": obsidian is not None}
        if obsidian is not None:
            vault_info["path"] = str(obsidian.vault_path.resolve())
            try:
                notes = obsidian.list_notes("")
                vault_info["note_count"] = len(notes)
            except Exception as e:
                vault_info["error"] = str(e)

        # --- Embedder ---
        embedder_info: dict[str, Any] = {"configured": embedder is not None}
        if embedder is not None:
            cls_name = type(embedder).__name__
            provider_label = {
                "OllamaProvider": "ollama",
                "OpenAICompatibleProvider": "openai-compatible",
                "NullProvider": "none",
            }.get(cls_name, cls_name)
            embedder_info["provider"] = provider_label
            model = getattr(embedder, "model", None)
            if model is not None:
                embedder_info["model"] = model
            embedder_info["dimensions"] = int(getattr(embedder, "dimensions", 0))

        # --- Search mode (what the next engrama_search would attempt) ---
        # Mirrors the gate in engrama_search above: hybrid only when a
        # functional async embedder is wired up. ``degraded`` here is
        # always False because no search has been *attempted* yet at the
        # time of the status call — runtime degradation (provider
        # unreachable mid-search) only surfaces on engrama_search's
        # response.
        would_hybrid = (
            embedder is not None
            and int(getattr(embedder, "dimensions", 0)) > 0
            and hasattr(embedder, "aembed")
        )
        search_info: dict[str, Any] = {
            "mode": "hybrid" if would_hybrid else "fulltext_only",
            "degraded": False,
            "reason": "" if would_hybrid else "embedder unavailable or dimensions=0",
        }

        response: dict[str, Any] = {
            "version": engrama_version,
            "backend": backend_info,
            "vault": vault_info,
            "embedder": embedder_info,
            "search": search_info,
        }
        if startup_error:
            response["startup_error"] = startup_error

        return json.dumps(response, default=str, indent=2)

    # -- Tool: engrama_search -----

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

        Uses hybrid search (fulltext + vector similarity) when embeddings
        are configured, otherwise falls back to fulltext only.

        Returns a JSON array of matches with ``type``, ``name``, ``score``
        and — for every node that has been enriched — ``summary`` (2–3
        sentence overview, falling back to ``description`` for older nodes)
        and ``tags`` (freeform list).  This lets you act on search results
        without a second ``engrama_context`` call.  ``details`` is *not*
        returned here; call ``engrama_context`` when you need the full
        content of a node.
        """
        store = _store(ctx)
        state = ctx.request_context.lifespan_context

        # --- DDR-003 Phase C: Hybrid search when available ---
        _embedder = state.get("embedder")
        use_hybrid = (
            _embedder is not None
            and getattr(_embedder, "dimensions", 0) > 0
            and hasattr(_embedder, "aembed")
        )

        if use_hybrid:
            try:
                from engrama.core.search import HybridSearchEngine

                # Use the async store as both graph and vector backend
                hybrid = HybridSearchEngine(store, store, _embedder, scope=_MCP_SCOPE)
                hybrid_results = await hybrid.asearch(params.query, limit=params.limit)
                results = [
                    {
                        "type": r.label,
                        "name": r.name,
                        "score": round(r.final_score, 4),
                        "vector_score": round(r.vector_score, 4),
                        "fulltext_score": round(r.fulltext_score, 4),
                        "temporal_score": round(r.temporal_score, 4),
                        # Node-enrichment: surface summary + tags so callers
                        # can act on search results without a second
                        # engrama_context round-trip. ``details`` is
                        # intentionally excluded to keep responses compact.
                        "summary": r.properties.get("summary", "") or "",
                        "tags": r.properties.get("tags") or [],
                    }
                    for r in hybrid_results
                ]
                if not results:
                    return f"No results found for '{params.query}'."

                # --- Proactivity: check for pending Insights ---
                response = await _build_search_response(results, params.query, store)
                # Surface the actual execution mode so the caller can
                # tell a healthy hybrid run from a silent fallback when
                # the embeddings provider is unreachable (issue #17).
                response["search_mode"] = {
                    "mode": hybrid.last_mode.mode,
                    "degraded": hybrid.last_mode.degraded,
                    "reason": hybrid.last_mode.reason,
                }
                return json.dumps(response, default=str, indent=2)
            except Exception as e:
                logger.warning("Hybrid search failed, falling back to fulltext: %s", e)

        # --- Fulltext fallback ---
        results = await store.fulltext_search(params.query, params.limit, scope=_MCP_SCOPE)
        if not results:
            return f"No results found for '{params.query}'."

        response = await _build_search_response(results, params.query, store)
        # Same degradation signal as the hybrid branch above (issue #17).
        # If hybrid was not even attempted (``use_hybrid`` False) we
        # mark it as a non-degraded fulltext-only run; if hybrid was
        # attempted but raised, the ``except`` above already logged the
        # reason — surface a generic "hybrid_search_failed" marker so
        # callers see *something*.
        response["search_mode"] = {
            "mode": "fulltext_only",
            "degraded": use_hybrid,
            "reason": "hybrid path raised; see server logs" if use_hybrid else "",
        }
        return json.dumps(response, default=str, indent=2)

    async def _build_search_response(
        results: list[dict[str, Any]],
        query: str,
        store: Any,
    ) -> dict[str, Any]:
        """Build the search response with optional proactivity hints."""
        # --- Proactivity: check for pending Insights related to search ---
        related_insights: list[dict[str, Any]] = []
        if _proactive_state.get("enabled", True):
            try:
                insight_results = await store.fulltext_search(query, limit=3, scope=_MCP_SCOPE)
                # Filter for pending Insights
                related_insights = [
                    {k: v for k, v in r.items() if k in ("title", "body", "confidence", "score")}
                    for r in insight_results
                    if r.get("type") == "Insight"
                ]
            except Exception:
                pass

        response: dict[str, Any] = {"results": results}
        if related_insights:
            response["pending_insights"] = related_insights
            response["proactive_hint"] = (
                "There are pending Insights related to your search. "
                "Consider presenting them to the user with engrama_surface_insights."
            )
        return response

    # -- Tool: engrama_remember ---

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
        """Store a piece of knowledge as a node in the memory graph.

        Use this whenever you learn something new, solve a problem, or
        encounter an important entity (person, technology, project, etc.)
        that is not yet in the graph.  **Immediately after calling this,
        call engrama_relate** to connect the new node to its context —
        isolated nodes are much less useful.

        Properties should include rich context for future retrieval:

        * ``name`` (required): unique identifier for the node.
        * ``summary``: 2–3 sentence overview — what this is, why it matters.
        * ``details``: comprehensive context — techniques used, decisions
          made, approaches taken, alternatives considered, key examples.
          The richer this field, the more useful the memory becomes.
        * ``tags``: freeform list for filtering, e.g.
          ``["active-directory", "credential-access", "windows"]``.
        * ``source``: how this knowledge was captured
          (``"conversation"``, ``"ingest"``, ``"manual"``, ``"sync"``).
        * ``status``: current state
          (``"active"``, ``"resolved"``, ``"superseded"``).

        If a node with the same ``name`` (or ``title``) already exists, its
        properties are updated (MERGE semantics).  ``description`` is still
        accepted for backward compatibility and is used as a fallback when
        ``summary`` is absent.

        When a vault is configured, a corresponding .md note is created (or
        updated) with full YAML frontmatter including engrama_id and an empty
        relations block (DDR-002).
        """
        import re as _re

        store = _store(ctx)
        label = params.label
        props = dict(params.properties)

        # Extract relations before MERGE — Neo4j can't store dicts as properties.
        inline_relations: dict[str, list[str]] = props.pop("relations", {}) or {}
        if not isinstance(inline_relations, dict):
            inline_relations = {}

        if label not in _VALID_LABELS:
            valid = ", ".join(sorted(_VALID_LABELS))
            return f"Error: Invalid label '{label}'. Must be one of: {valid}."

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

            # Check if a note already exists
            note_data = obsidian.read_note(vault_path)
            if note_data["success"]:
                engrama_id = note_data["frontmatter"].get("engrama_id")

            if not engrama_id:
                engrama_id = str(uuid.uuid4())

            # Build frontmatter for the note
            fm: dict[str, Any] = {"engrama_id": engrama_id, "type": label}
            for k, v in props.items():
                if k not in ("created_at", "updated_at"):
                    fm[k] = v
            if "relations" not in fm:
                fm["relations"] = {}

            try:
                import yaml as _yaml

                fm_yaml = _yaml.dump(
                    fm,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
                target = obsidian._resolve(vault_path)

                if note_data["success"]:
                    # Update existing note — replace frontmatter, keep body
                    content = note_data["content"]
                    if content.startswith("---"):
                        end_idx = content.index("---", 3)
                        body = content[end_idx + 3 :]
                    else:
                        body = "\n\n" + content
                    new_content = "---\n" + fm_yaml + "---" + body
                else:
                    # Create new note
                    new_content = "---\n" + fm_yaml + f"---\n\n# {merge_value}\n"
                    desc = props.get("notes") or props.get("description")
                    if desc:
                        new_content += f"\n> {desc}\n"

                target.write_text(new_content, encoding="utf-8")
                logger.info("Vault note written: %s", vault_path)
            except Exception as e:
                logger.warning("Could not write vault note for %s: %s", merge_value, e)
                vault_path = None

        # --- Graph write using async store ---
        if vault_path:
            props["obsidian_path"] = vault_path
        if engrama_id:
            props["obsidian_id"] = engrama_id

        extra = {k: v for k, v in props.items() if k not in {merge_key, "created_at", "updated_at"}}

        result = await store.merge_node(label, merge_key, merge_value, _with_mcp_provenance(extra))
        node = result["node"]

        # --- DDR-003 Phase C: Embed on write (async) ---
        _embedder = state.get("embedder")
        if _embedder is not None and getattr(_embedder, "dimensions", 0) > 0:
            try:
                from engrama.embeddings.text import node_to_text

                text = node_to_text(label, props)
                # Use async embed if available (non-blocking), fallback to sync
                if hasattr(_embedder, "aembed"):
                    embedding = await _embedder.aembed(text)
                else:
                    embedding = _embedder.embed(text)
                if embedding:
                    # Store via async store (preferred) or sync vector store
                    await store.store_embedding(
                        label,
                        merge_key,
                        merge_value,
                        embedding,
                    )
            except Exception as e:
                logger.warning("Embed-on-write failed for %s/%s: %s", label, merge_value, e)

        # --- BUG-005: Process inline relations ---
        all_relations: dict[str, list[str]] = {}
        for src in (params.relations, inline_relations):
            for rtype, targets in (src or {}).items():
                merged = all_relations.setdefault(rtype, [])
                for t in targets if isinstance(targets, list) else [targets]:
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
                    target_label = await store.lookup_node_label(target_name)

                    if target_label is None:
                        # Infer label from relation type and create stub
                        target_label = ObsidianSync._infer_stub_label(rel_type_upper)
                        try:
                            await store.merge_node(
                                target_label,
                                "name",
                                target_name,
                                _with_mcp_provenance({"status": "stub"}),
                            )
                        except Exception as e:
                            logger.warning("Could not create stub %s: %s", target_name, e)
                            continue

                    # Create the relationship
                    try:
                        await store.merge_relation(
                            label,
                            merge_key,
                            merge_value,
                            rel_type_upper,
                            target_label,
                            "name",
                            target_name,
                        )
                        relations_created += 1
                    except Exception as e:
                        logger.warning(
                            "Could not create relation %s -[%s]-> %s: %s",
                            merge_value,
                            rel_type_upper,
                            target_name,
                            e,
                        )

                    # Write relation to vault frontmatter (DDR-002)
                    if obsidian is not None and vault_path:
                        try:
                            obsidian.add_relation(vault_path, rel_type_upper, target_name)
                        except Exception:
                            pass

        # --- Proactivity: increment counter and check threshold ---
        result_data: dict[str, Any] = {
            "status": "ok",
            "label": label,
            "node": node,
            "vault_path": vault_path,
            "engrama_id": engrama_id,
            "relations_created": relations_created,
        }
        # DDR-003 Phase D: propagate valid_to conflict warning
        if result.get("warning"):
            result_data["warning"] = result["warning"]

        if _proactive_state.get("enabled", True):
            _proactive_state["remember_count"] = _proactive_state.get("remember_count", 0) + 1
            since_last = _proactive_state["remember_count"] - _proactive_state.get(
                "last_reflect_at", 0
            )
            if since_last >= 10:
                result_data["proactive_hint"] = (
                    f"You've stored {since_last} entities since the last reflect. "
                    "Consider running engrama_reflect to detect cross-domain patterns, "
                    "then engrama_surface_insights to present findings to the user."
                )

        return json.dumps(result_data, default=str, indent=2)

    # -- Tool: engrama_relate ---

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
            valid = ", ".join(sorted(_VALID_RELATIONS))
            return f"Error: Invalid rel_type '{params.rel_type}'. Must be one of: {valid}."

        store = _store(ctx)
        state = ctx.request_context.lifespan_context

        # Determine merge keys
        from_key = "title" if params.from_label in TITLE_KEYED_LABELS else "name"
        to_key = "title" if params.to_label in TITLE_KEYED_LABELS else "name"

        # Use the async store to create the relationship
        r = await store.merge_relation(
            params.from_label,
            from_key,
            params.from_name,
            params.rel_type,
            params.to_label,
            to_key,
            params.to_name,
        )
        if not r:
            return (
                f"No relationship created — could not find "
                f"(:{params.from_label} {{name: '{params.from_name}'}}) "
                f"or (:{params.to_label} {{name: '{params.to_name}'}})."
            )

        # DDR-002: dual-write — also record the relation in vault frontmatter
        vault_written = False
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        from_path = r.get("from_obsidian_path")

        if obsidian is not None:
            import re as _re

            # If the source node has no vault note yet, create one now
            if not from_path:
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
                            fm,
                            default_flow_style=False,
                            allow_unicode=True,
                            sort_keys=False,
                        )
                        target_file = obsidian._resolve(from_path)
                        target_file.write_text(
                            "---\n" + fm_yaml + f"---\n\n# {params.from_name}\n",
                            encoding="utf-8",
                        )
                        # Update the graph node with obsidian metadata
                        await store.merge_node(
                            params.from_label,
                            from_key,
                            params.from_name,
                            _with_mcp_provenance({"obsidian_path": from_path, "obsidian_id": _eid}),
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not create vault note for %s: %s", params.from_name, e
                        )
                        from_path = None

            if from_path:
                try:
                    vault_written = obsidian.add_relation(
                        from_path,
                        params.rel_type,
                        params.to_name,
                    )
                except Exception as e:
                    logger.warning(
                        "DDR-002 vault write failed for %s -[%s]-> %s: %s",
                        params.from_name,
                        params.rel_type,
                        params.to_name,
                        e,
                    )

        return json.dumps(
            {"status": "ok", **r, "vault_written": vault_written},
            default=str,
            indent=2,
        )

    # -- Tool: engrama_context ---

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

        The root node returns **all** enrichment fields (``summary``,
        ``details``, ``tags``, ``source``), giving you the full content of
        the requested node in one call.  Neighbour dicts strip ``details``
        (it can be long) but keep ``summary`` and ``tags`` so you can
        decide whether a neighbour is worth exploring — call
        ``engrama_context`` on that neighbour if you need its full details.
        """
        if params.label not in _VALID_LABELS:
            return f"Error: Invalid label '{params.label}'."

        store = _store(ctx)
        merge_key = "title" if params.label in TITLE_KEYED_LABELS else "name"

        data = await store.get_node_with_neighbours(
            params.label, merge_key, params.name, params.hops, scope=_MCP_SCOPE
        )
        if data is None:
            return f"No node found: (:{params.label} {{name: '{params.name}'}})."

        return json.dumps(data, default=str, indent=2)

    # -- Tool: engrama_sync_note ---

    @mcp.tool(
        name="engrama_sync_note",
        annotations=ToolAnnotations(
            title="Sync Single Note to Graph",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_sync_note(params: SyncNoteInput, ctx: Context) -> str:
        """Sync a single note from Engrama's internal Obsidian vault to the graph.

        **Vault scope.** This tool operates on the Obsidian vault that
        Engrama owns — the path configured via ``VAULT_PATH`` (or the
        ``--vault-path`` flag on ``engrama-mcp``). It is distinct from
        any user-managed Obsidian vault exposed by a separate
        ``obsidian-mcp`` (or equivalent) server: never use this tool to
        sync notes that live outside Engrama's vault. ``params.path``
        is resolved relative to ``VAULT_PATH``.

        Reads the note via ObsidianAdapter, parses entities via NoteParser,
        merges the node into the active backend, and injects
        ``engrama_id`` back into the note's YAML frontmatter.

        Pass ``dry_run=True`` to preview the effect without writing to
        the graph or to the note's frontmatter. The response keeps the
        same envelope (``status``, ``label``, ``name``, ``engrama_id``,
        ``dry_run``) and replaces ``created`` / ``node`` with
        ``would_create`` and ``would_inject_engrama_id`` booleans, so a
        caller can pre-check the impact of a sync against a vault it is
        not yet sure belongs to Engrama.

        Returns JSON with status, label, name, engrama_id, dry_run, and
        either ``created`` + ``node`` (real run) or ``would_create`` +
        ``would_inject_engrama_id`` (dry run).
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        parser: NoteParser | None = state.get("parser")

        if obsidian is None or parser is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Obsidian adapter not initialised — vault sync disabled.",
                },
                indent=2,
            )

        note_data = obsidian.read_note(params.path)
        if not note_data["success"]:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not read note: {params.path}",
                    "details": note_data.get("error", "Unknown error"),
                },
                indent=2,
            )

        frontmatter = note_data["frontmatter"]
        engrama_id = frontmatter.get("engrama_id")
        if not engrama_id:
            engrama_id = str(uuid.uuid4())

        node_label = frontmatter.get("type", "Concept")
        if node_label not in _VALID_LABELS:
            node_label = "Concept"

        # Extract merge key from frontmatter
        props = dict(frontmatter)
        props.pop("relations", None)
        if "engrama_id" in props:
            props.pop("engrama_id")
        if "type" in props:
            props.pop("type")

        # Determine merge key
        merge_key = "title" if node_label in TITLE_KEYED_LABELS else "name"
        if merge_key not in props:
            # Try to use the filename as a fallback
            filename = os.path.basename(params.path).replace(".md", "")
            props[merge_key] = filename

        merge_value = props[merge_key]
        store = _store(ctx)

        # Dry-run path: predict create/inject decisions without writing.
        # Uses ``get_node`` (read-only) to figure out whether a real run
        # would create or update the row; reports whether ``engrama_id``
        # would be injected based on the note's current frontmatter.
        if params.dry_run:
            existing = await store.get_node(node_label, merge_key, merge_value)
            return json.dumps(
                {
                    "status": "ok",
                    "dry_run": True,
                    "label": node_label,
                    "name": merge_value,
                    "engrama_id": engrama_id,
                    "would_create": existing is None,
                    "would_inject_engrama_id": not frontmatter.get("engrama_id"),
                },
                default=str,
                indent=2,
            )

        # Merge the node
        props["obsidian_path"] = params.path
        props["obsidian_id"] = engrama_id

        extra = {k: v for k, v in props.items() if k not in {merge_key, "created_at", "updated_at"}}
        result = await store.merge_node(
            node_label, merge_key, merge_value, _with_mcp_provenance(extra)
        )
        node = result["node"]
        created = result["created"]

        # --- DDR-003 Phase C: Embed on sync ---
        _embedder = state.get("embedder")
        if _embedder is not None and getattr(_embedder, "dimensions", 0) > 0:
            try:
                from engrama.embeddings.text import node_to_text

                text = node_to_text(node_label, props)
                if hasattr(_embedder, "aembed"):
                    embedding = await _embedder.aembed(text)
                else:
                    embedding = _embedder.embed(text)
                if embedding:
                    await store.store_embedding(
                        node_label,
                        merge_key,
                        merge_value,
                        embedding,
                    )
            except Exception as e:
                logger.warning("Embed-on-sync failed for %s/%s: %s", node_label, merge_value, e)

        # Update frontmatter with engrama_id if new
        if not frontmatter.get("engrama_id"):
            try:
                import yaml as _yaml

                fm = dict(frontmatter)
                fm["engrama_id"] = engrama_id
                fm_yaml = _yaml.dump(
                    fm,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
                content = note_data["content"]
                if content.startswith("---"):
                    end_idx = content.index("---", 3)
                    body = content[end_idx + 3 :]
                else:
                    body = "\n\n" + content
                new_content = "---\n" + fm_yaml + "---" + body
                target = obsidian._resolve(params.path)
                target.write_text(new_content, encoding="utf-8")
            except Exception as e:
                logger.warning("Could not update note frontmatter: %s", e)

        return json.dumps(
            {
                "status": "ok",
                "dry_run": False,
                "label": node_label,
                "name": merge_value,
                "engrama_id": engrama_id,
                "created": created,
                "node": node,
            },
            default=str,
            indent=2,
        )

    # -- Tool: engrama_sync_vault ---

    @mcp.tool(
        name="engrama_sync_vault",
        annotations=ToolAnnotations(
            title="Sync Entire Vault to Graph",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def engrama_sync_vault(params: SyncVaultInput, ctx: Context) -> str:
        """Scan Engrama's internal Obsidian vault and reconcile all notes.

        **Vault scope.** This tool operates on the Obsidian vault that
        Engrama owns — the path configured via ``VAULT_PATH`` (or the
        ``--vault-path`` flag on ``engrama-mcp``). It is distinct from
        any user-managed Obsidian vault exposed by a separate
        ``obsidian-mcp`` (or equivalent) server: if the user says
        "sync the vault" while both MCPs are connected, disambiguate
        before running this tool — never assume it refers to the
        external vault. ``params.folder`` is resolved relative to
        ``VAULT_PATH``.

        Iterates over all ``.md`` files (optionally restricted to a folder),
        parses entities, and merges nodes into the active backend.  Injects
        ``engrama_id`` into notes that don't have one yet.

        Pass ``dry_run=True`` to preview the impact of a full scan
        without touching the graph or any frontmatter. The response keeps
        the ``status`` / ``errors`` envelope and replaces the
        ``created`` / ``updated`` counts with ``would_create`` /
        ``would_update`` plus ``would_inject_engrama_id`` (count) and
        ``files_would_receive_engrama_id`` (list of paths that don't
        yet carry an ``engrama_id`` and would have one injected on a
        real run). Use this to confirm a sync targets the right vault.

        Returns JSON with status, dry_run, and either the live counts
        or the ``would_*`` projection.
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")
        parser: NoteParser | None = state.get("parser")

        if obsidian is None or parser is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Obsidian adapter not initialised — vault sync disabled.",
                },
                indent=2,
            )

        store = _store(ctx)
        created_count = 0
        updated_count = 0
        skipped_count = 0
        would_create_count = 0
        would_update_count = 0
        files_would_receive_engrama_id: list[str] = []
        errors: list[str] = []

        try:
            # Get list of notes in the vault. ``list_notes`` returns
            # ``[{"path": ..., "name": ...}, ...]``; pull the relative
            # path string out of each entry. (Pre-existing iteration
            # over the dict treated it as a path, so the real-sync code
            # path had been silently producing "Error syncing note
            # {'path': ...}: unsupported operand type(s) for / ..." for
            # every note — surfaced by the dry-run tests added with
            # this change.)
            notes = obsidian.list_notes(params.folder if params.folder else "")

            for note_entry in notes:
                note_path = note_entry["path"] if isinstance(note_entry, dict) else note_entry
                try:
                    note_data = obsidian.read_note(note_path)
                    if not note_data["success"]:
                        skipped_count += 1
                        continue

                    frontmatter = note_data["frontmatter"]
                    engrama_id = frontmatter.get("engrama_id")
                    if not engrama_id:
                        engrama_id = str(uuid.uuid4())

                    node_label = frontmatter.get("type", "Concept")
                    if node_label not in _VALID_LABELS:
                        skipped_count += 1
                        continue

                    props = dict(frontmatter)
                    props.pop("relations", None)
                    if "engrama_id" in props:
                        props.pop("engrama_id")
                    if "type" in props:
                        props.pop("type")

                    merge_key = "title" if node_label in TITLE_KEYED_LABELS else "name"
                    if merge_key not in props:
                        filename = os.path.basename(note_path).replace(".md", "")
                        props[merge_key] = filename

                    merge_value = props[merge_key]

                    # Dry-run path: peek at the store to predict create vs
                    # update, record which files would gain an engrama_id,
                    # and skip every write.
                    if params.dry_run:
                        existing = await store.get_node(node_label, merge_key, merge_value)
                        if existing is None:
                            would_create_count += 1
                        else:
                            would_update_count += 1
                        if not frontmatter.get("engrama_id"):
                            files_would_receive_engrama_id.append(note_path)
                        continue

                    props["obsidian_path"] = note_path
                    props["obsidian_id"] = engrama_id

                    extra = {
                        k: v
                        for k, v in props.items()
                        if k not in {merge_key, "created_at", "updated_at"}
                    }
                    result = await store.merge_node(
                        node_label, merge_key, merge_value, _with_mcp_provenance(extra)
                    )
                    if result["created"]:
                        created_count += 1
                    else:
                        updated_count += 1

                    # Update note with engrama_id if needed
                    if not frontmatter.get("engrama_id"):
                        try:
                            import yaml as _yaml

                            fm = dict(frontmatter)
                            fm["engrama_id"] = engrama_id
                            fm_yaml = _yaml.dump(
                                fm,
                                default_flow_style=False,
                                allow_unicode=True,
                                sort_keys=False,
                            )
                            content = note_data["content"]
                            if content.startswith("---"):
                                end_idx = content.index("---", 3)
                                body = content[end_idx + 3 :]
                            else:
                                body = "\n\n" + content
                            new_content = "---\n" + fm_yaml + "---" + body
                            target = obsidian._resolve(note_path)
                            target.write_text(new_content, encoding="utf-8")
                        except Exception as e:
                            logger.warning("Could not update note %s: %s", note_path, e)

                except Exception as e:
                    logger.warning("Error syncing note %s: %s", note_path, e)
                    errors.append(f"{note_path}: {str(e)}")
                    skipped_count += 1

        except Exception as e:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not scan vault: {str(e)}",
                },
                indent=2,
            )

        if params.dry_run:
            return json.dumps(
                {
                    "status": "ok",
                    "dry_run": True,
                    "would_create": would_create_count,
                    "would_update": would_update_count,
                    "skipped": skipped_count,
                    "would_inject_engrama_id": len(files_would_receive_engrama_id),
                    "files_would_receive_engrama_id": files_would_receive_engrama_id,
                    "errors": errors,
                },
                default=str,
                indent=2,
            )

        return json.dumps(
            {
                "status": "ok",
                "dry_run": False,
                "created": created_count,
                "updated": updated_count,
                "skipped": skipped_count,
                "errors": errors,
            },
            default=str,
            indent=2,
        )

    # -- Tool: engrama_ingest ---

    @mcp.tool(
        name="engrama_ingest",
        annotations=ToolAnnotations(
            title="Ingest Content",
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

        **Vault scope (when ``source_type='note'``).** ``params.source``
        is resolved relative to Engrama's internal vault
        (``VAULT_PATH``). To pull content from a user-managed Obsidian
        vault exposed by a separate ``obsidian-mcp`` server, read it
        with that server's tools first and pass the result to this
        one as ``source_type='text'``.

        This is the primary way to populate the graph from existing content.
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")

        source_path = None
        content = ""

        if params.source_type == "note":
            if obsidian is None:
                return json.dumps(
                    {
                        "status": "error",
                        "message": "Obsidian adapter not initialised — cannot read notes.",
                    },
                    indent=2,
                )
            note_data = obsidian.read_note(params.source)
            if not note_data["success"]:
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Could not read note: {params.source}",
                        "details": note_data.get("error"),
                    },
                    indent=2,
                )
            source_path = params.source
            content = note_data["content"]
        elif params.source_type == "text":
            content = params.source
        elif params.source_type == "conversation":
            content = params.source
        else:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Unknown source_type: {params.source_type}",
                },
                indent=2,
            )

        # Generate extraction guidance
        extraction_prompt = (
            "You have just read the above content. Your task is to extract entities "
            "and relationships that would be useful to remember.\n\n"
            "For each entity you identify:\n"
            "1. Call `engrama_remember` with the entity's label, name, and rich "
            "properties — thin nodes are nearly useless to future-you.\n"
            "2. Immediately call `engrama_relate` to connect it to related entities.\n\n"
            "When extracting entities from this content, include for each:\n"
            "- 'summary': 2–3 sentence overview of what this entity is and why it matters\n"
            "- 'details': comprehensive context — techniques used, decisions made, "
            "approaches taken, alternatives considered, key examples\n"
            "- 'tags': relevant freeform tags for filtering (e.g. domain, tactic, status)\n"
            "- 'source': set to 'ingest' so later callers know the provenance\n\n"
            "Prefer specific relation types (EXPLOITS, EXECUTED_WITH, PREREQUISITE_OF, "
            "TEACHES, COVERS, TARGETS, USES, COMPOSED_OF, …) over the generic RELATED_TO; "
            "specific edges make pattern detection (engrama_reflect) much more useful.\n\n"
            "Focus on:\n"
            "- People (Person nodes)\n"
            "- Projects and products (Project nodes)\n"
            "- Technologies mentioned (Technology nodes)\n"
            "- Concepts and ideas (Concept nodes)\n"
            "- Decisions made (Decision nodes — use title)\n"
            "- Problems or challenges (Problem nodes — use title)\n"
            "- Lessons learned\n"
            "- Cross-references between entities\n\n"
            f"Context hint: {params.context_hint if params.context_hint else '(none provided)'}\n"
        )

        return json.dumps(
            {
                "status": "ok",
                "source_type": params.source_type,
                "source_path": source_path,
                "content_length": len(content),
                "content": content,
                "extraction_prompt": extraction_prompt,
            },
            default=str,
            indent=2,
        )

    # -- Tool: engrama_reflect ---

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
        store = _store(ctx)
        insights: list[dict[str, Any]] = []
        queries_run: list[str] = []
        queries_skipped: list[str] = []

        # --- Step 1: Profile the graph ---
        profile: dict[str, int] = {}
        try:
            profile = await store.count_labels()
        except Exception as e:
            logger.warning("Could not profile graph: %s", e)

        # --- Step 2: Get already-judged titles ---
        # ``judged`` covers both dismissed AND approved insights so a
        # re-run of reflect doesn't re-MERGE them back to status="pending"
        # (which would silently undo the user's review).
        dismissed: set[str] = set()
        approved: set[str] = set()
        try:
            dismissed = await store.get_dismissed_titles()
        except Exception:
            pass
        try:
            approved = await store.get_approved_titles()
        except Exception:
            pass
        judged = dismissed | approved

        # --- Helper to run a query and create Insights ---
        async def _run_pattern(
            query_name: str,
            detect_fn,
            required_labels: list[str] | None = None,
            any_labels: list[list[str]] | None = None,
            min_label_count: dict[str, int] | None = None,
            builder_fn=None,
        ):
            # Check if ALL required labels have data
            for label in required_labels or []:
                if not profile.get(label):
                    queries_skipped.append(query_name)
                    return
            # Check any_labels: each entry is an OR-group — at least one must exist
            for group in any_labels or []:
                if not any(profile.get(lbl) for lbl in group):
                    queries_skipped.append(query_name)
                    return
            if min_label_count:
                for label, min_cnt in min_label_count.items():
                    if profile.get(label, 0) < min_cnt:
                        queries_skipped.append(query_name)
                        return

            queries_run.append(query_name)
            try:
                records = await detect_fn()
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
                if title in judged:
                    continue
                body = (
                    f'The open problem "{r["open_problem"]}" in project '
                    f'"{r["target_project"]}" shares the concept '
                    f'"{r["concept"]}" with a resolved problem in project '
                    f'"{r["source_project"]}". The decision '
                    f'"{r["decision"]}" may apply here.'
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": 0.85,
                            "status": "pending",
                            "source_query": "cross_project_solution",
                        }
                    ),
                )
                insights.append(
                    {"query": "cross_project_solution", "title": title, "confidence": 0.85}
                )

        async def _build_shared_tech(records):
            for r in records:
                a_desc = f"{r['type_a']}:{r['entity_a']}"
                b_desc = f"{r['type_b']}:{r['entity_b']}"
                title = f"Shared technology: {r['technology']} ({a_desc} & {b_desc})"
                if title in judged:
                    continue
                confidence = 0.75 if r["type_a"] != r["type_b"] else 0.6
                body = (
                    f"{a_desc} and {b_desc} both use {r['technology']}. "
                    f"Consider sharing knowledge or materials between them."
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": confidence,
                            "status": "pending",
                            "source_query": "shared_technology",
                        }
                    ),
                )
                insights.append(
                    {"query": "shared_technology", "title": title, "confidence": confidence}
                )

        async def _build_training(records):
            for r in records:
                issue_desc = f"{r['issue_type']}:{r['issue']}"
                title = (
                    f"Training opportunity: {r['course']} "
                    f"covers {r['concept']} (relates to: {issue_desc})"
                )
                if title in judged:
                    continue
                body = (
                    f'The {r["issue_type"].lower()} "{r["issue"]}" involves '
                    f'the concept "{r["concept"]}", which is covered by the '
                    f'course "{r["course"]}". Reviewing this material may help.'
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": 0.65,
                            "status": "pending",
                            "source_query": "training_opportunity",
                        }
                    ),
                )
                insights.append(
                    {"query": "training_opportunity", "title": title, "confidence": 0.65}
                )

        async def _build_technique_transfer(records):
            for r in records:
                title = (
                    f"Technique transfer: {r['technique']} "
                    f"({r['source_domain']} → {r['target_domain']})"
                )
                if title in judged:
                    continue
                related = r["related_entities"]
                confidence = min(0.5 + (related * 0.1), 0.9)
                body = (
                    f'The technique "{r["technique"]}" is used in '
                    f'"{r["source_domain"]}" but not in '
                    f'"{r["target_domain"]}". There are {related} '
                    f"entities in {r['target_domain']} sharing concepts "
                    f"with this technique."
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": confidence,
                            "status": "pending",
                            "source_query": "technique_transfer",
                        }
                    ),
                )
                insights.append(
                    {"query": "technique_transfer", "title": title, "confidence": confidence}
                )

        async def _build_concept_clustering(records):
            for r in records:
                concept = r["concept"]
                count = r["entity_count"]
                sample = r["sample"]
                title = f"Concept cluster: {concept} ({count} entities)"
                if title in judged:
                    continue
                sample_desc = ", ".join(f"{s['label']}:{s['name']}" for s in (sample or [])[:5])
                confidence = min(0.5 + (count * 0.05), 0.9)
                body = (
                    f'The concept "{concept}" connects {count} entities: '
                    f"{sample_desc}. This cluster may reveal a pattern."
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": confidence,
                            "status": "pending",
                            "source_query": "concept_clustering",
                        }
                    ),
                )
                insights.append(
                    {"query": "concept_clustering", "title": title, "confidence": confidence}
                )

        async def _build_stale(records):
            for r in records:
                name = r["name"]
                title = f"Stale knowledge: {r['label']}:{name} (linked to {r['project']})"
                if title in judged:
                    continue
                last_updated = r["last_updated"]
                if hasattr(last_updated, "isoformat"):
                    last_updated = last_updated.isoformat()[:10]
                body = (
                    f'The {r["label"]} "{name}" is connected to the active '
                    f'project "{r["project"]}" via {r["rel"]}, but hasn\'t been '
                    f"updated since {last_updated}. Consider reviewing or archiving."
                )
                await store.merge_node(
                    "Insight",
                    "title",
                    title,
                    _with_mcp_provenance(
                        {
                            "body": body,
                            "confidence": 0.5,
                            "status": "pending",
                            "source_query": "stale_knowledge",
                        }
                    ),
                )
                insights.append({"query": "stale_knowledge", "title": title, "confidence": 0.5})

        async def _build_under_connected(records):
            if not records:
                return
            # BUG-007: use a stable title (no count) to avoid uniqueness
            # constraint collisions when the node count changes between runs.
            title = "Under-connected nodes need more relationships"

            # Skip if already dismissed
            if title in judged:
                return
            try:
                dismissed_sq = await store.find_insight_by_source_query(
                    "under_connected",
                    statuses=["dismissed"],
                )
                if dismissed_sq:
                    return
            except Exception:
                pass

            names = [f"{r['label']}:{r['name']}" for r in records[:10]]
            total = len(records)
            body = (
                f"Found {total} nodes with fewer than 2 relationships. "
                f"Candidates for enrichment: {', '.join(names)}."
            )
            # MERGE on stable title — idempotent, updates body on repeat runs
            await store.merge_node(
                "Insight",
                "title",
                title,
                _with_mcp_provenance(
                    {
                        "body": body,
                        "confidence": 0.4,
                        "status": "pending",
                        "source_query": "under_connected",
                    }
                ),
            )
            insights.append({"query": "under_connected", "title": title, "confidence": 0.4})

        # --- Step 3: Run applicable patterns ---
        await _run_pattern(
            "cross_project_solution",
            store.detect_cross_project_solutions,
            required_labels=["Problem", "Project"],
            builder_fn=_build_cross_project,
        )
        await _run_pattern(
            "shared_technology",
            store.detect_shared_technology,
            required_labels=["Technology"],
            builder_fn=_build_shared_tech,
        )
        await _run_pattern(
            "training_opportunity",
            store.detect_training_opportunities,
            any_labels=[["Problem", "Vulnerability"], ["Course"]],
            builder_fn=_build_training,
        )
        await _run_pattern(
            "technique_transfer",
            store.detect_technique_transfer,
            required_labels=["Technique"],
            min_label_count={"Domain": 2},
            builder_fn=_build_technique_transfer,
        )
        await _run_pattern(
            "concept_clustering",
            store.detect_concept_clusters,
            required_labels=["Concept"],
            builder_fn=_build_concept_clustering,
        )
        await _run_pattern(
            "stale_knowledge",
            store.detect_stale_knowledge,
            any_labels=[["Project", "Course"]],
            builder_fn=_build_stale,
        )

        # Under-connected: always run if enough nodes
        total_nodes = sum(profile.values())
        if total_nodes >= 5:
            queries_run.append("under_connected")
            try:
                uc_records = await store.detect_under_connected_nodes()
                await _build_under_connected(uc_records)
            except Exception as e:
                logger.warning("Under-connected query failed: %s", e)
        else:
            queries_skipped.append("under_connected")

        # --- Proactivity: reset counter ---
        _proactive_state["last_reflect_at"] = _proactive_state.get("remember_count", 0)

        return json.dumps(
            {
                "status": "ok",
                "graph_profile": profile,
                "queries_run": queries_run,
                "queries_skipped": queries_skipped,
                "dismissed_count": len(dismissed),
                "approved_count": len(approved),
                "insights_count": len(insights),
                "insights": insights,
            },
            default=str,
            indent=2,
        )

    # -- Tool: engrama_surface_insights ---

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
        store = _store(ctx)
        try:
            results = await store.get_pending_insights(limit=params.limit)
            insights = [
                {
                    "title": r["title"],
                    "body": r["body"],
                    "confidence": r["confidence"],
                    "source_query": r["source_query"],
                }
                for r in results
            ]
        except Exception as e:
            logger.warning("Could not fetch pending insights: %s", e)
            insights = []

        if not insights:
            return json.dumps(
                {
                    "status": "ok",
                    "message": "No pending Insights.",
                    "count": 0,
                    "insights": [],
                },
                indent=2,
            )

        return json.dumps(
            {
                "status": "ok",
                "count": len(insights),
                "insights": insights,
            },
            default=str,
            indent=2,
        )

    # -- Tool: engrama_approve_insight ---

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
        store = _store(ctx)

        if params.action not in ("approve", "dismiss"):
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Invalid action '{params.action}'. Use 'approve' or 'dismiss'.",
                }
            )

        new_status = "approved" if params.action == "approve" else "dismissed"

        try:
            updated = await store.update_insight_status(params.title, new_status)
            if not updated:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Insight not found: {params.title}",
                    }
                )
        except Exception as e:
            logger.warning("Could not update Insight status: %s", e)
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not update Insight: {str(e)}",
                },
                indent=2,
            )

        return json.dumps(
            {
                "status": "ok",
                "title": params.title,
                "action": params.action,
                "new_status": new_status,
            },
            indent=2,
        )

    # -- Tool: engrama_write_insight_to_vault ---

    @mcp.tool(
        name="engrama_write_insight_to_vault",
        annotations=ToolAnnotations(
            title="Write Approved Insight to Note",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
    )
    async def engrama_write_insight_to_vault(
        params: WriteInsightInput,
        ctx: Context,
    ) -> str:
        """Append an approved Insight as a section in an Obsidian note.

        Only Insights with ``status: "approved"`` are written.  The Insight
        is appended as a Markdown section with a horizontal rule separator,
        including confidence, source query, and approval timestamp.
        """
        state = ctx.request_context.lifespan_context
        obsidian: ObsidianAdapter | None = state.get("obsidian")

        if obsidian is None:
            return json.dumps(
                {
                    "status": "error",
                    "message": "Obsidian adapter not initialised.",
                },
                indent=2,
            )

        store = _store(ctx)

        # Fetch the Insight
        try:
            insight = await store.get_insight_by_title(params.title)
            if not insight:
                return json.dumps(
                    {
                        "status": "error",
                        "message": f"Insight not found: {params.title}",
                    },
                    indent=2,
                )
        except Exception as e:
            logger.warning("Could not fetch Insight: %s", e)
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not fetch Insight: {str(e)}",
                },
                indent=2,
            )

        if insight.get("status") != "approved":
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Insight is not approved: {params.title}",
                },
                indent=2,
            )

        # Read the target note
        note_data = obsidian.read_note(params.target_note)
        if not note_data["success"]:
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not read target note: {params.target_note}",
                },
                indent=2,
            )

        # Append the Insight
        content = note_data["content"]
        body = insight.get("body", "")
        confidence = insight.get("confidence", "")
        source_query = insight.get("source_query", "")

        insight_section = (
            f"\n\n---\n\n## Insight: {params.title}\n\n"
            f"{body}\n\n"
            f"_Confidence: {confidence} | Source: {source_query}_"
        )

        try:
            target = obsidian._resolve(params.target_note)
            new_content = content + insight_section
            target.write_text(new_content, encoding="utf-8")
            logger.info("Insight written to vault: %s", params.target_note)
        except Exception as e:
            logger.warning("Could not write insight to vault: %s", e)
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not write to vault: {str(e)}",
                },
                indent=2,
            )

        # Mark as synced in Neo4j
        try:
            await store.mark_insight_synced(params.title, params.target_note)
        except Exception as e:
            logger.warning("Could not mark insight as synced: %s", e)

        return json.dumps(
            {
                "status": "ok",
                "title": params.title,
                "target_note": params.target_note,
                "written": True,
            },
            indent=2,
        )

    # -- Prompt: engrama_session_guide ---

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
