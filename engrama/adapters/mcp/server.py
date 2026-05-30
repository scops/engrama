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

import asyncio
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
from engrama.core.identity import resolve_local_sub
from engrama.core.schema import TITLE_KEYED_LABELS, NodeType, RelationType
from engrama.core.scope import MemoryScope
from engrama.core.security import Provenance, Sanitiser

logger = logging.getLogger("engrama_mcp")
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Per-request scope in logging context (Spec 001, T009a / NFR-2)
# ---------------------------------------------------------------------------
#
# Every tool body resolves a :class:`MemoryScope` from request headers and
# binds it to the logging context here. A :class:`logging.Filter` injects
# ``scope_org`` / ``scope_user`` onto every record produced inside the
# request, so a log aggregator can group lines by tenant without the tool
# bodies having to pass the scope through every ``logger.info`` call.
#
# Identifiers are **hashed** (sha256, truncated to 8 chars) before they hit
# the record, so logs never carry raw ``(org_id, user_id)`` even when
# verbose log levels are enabled. A truncated hash is enough for grouping
# and incident correlation; reversal needs the raw value from the request
# context anyway.
#
# The ContextVar is set per-request and reset on tool exit — ``contextvars``
# handles async-task boundaries correctly (asyncio copies the context on
# task creation), so concurrent requests cannot bleed scopes into each
# other's logs.

import contextvars as _ctxvars  # noqa: E402
import hashlib as _hashlib  # noqa: E402

_LOG_SCOPE: _ctxvars.ContextVar[tuple[str, str] | None] = _ctxvars.ContextVar(
    "engrama_mcp_scope", default=None
)


def _hash_id(value: str | None) -> str:
    """Stable 8-char sha256 prefix; ``-`` when no identity is bound.

    Hash is one-way and short; collisions inside a single tenant deployment
    are astronomically unlikely. Safe to surface in logs and tracing
    backends (Sentry, Loki, CloudWatch) without leaking customer IDs.
    """
    if not value:
        return "-"
    return _hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _bind_scope_to_logging(scope: MemoryScope) -> _ctxvars.Token:
    """Bind ``scope`` to the logging context for the current request.

    Returns the :class:`ContextVar.Token` so callers can ``reset(token)``
    on tool exit. Idempotent within a single contextvar context: re-binding
    overwrites the previous binding.
    """
    return _LOG_SCOPE.set((_hash_id(scope.org_id), _hash_id(scope.user_id)))


class _ScopeLogFilter(logging.Filter):
    """Inject the bound scope onto every :class:`logging.LogRecord`.

    Adds ``record.scope_org`` and ``record.scope_user`` (hashed). A format
    string like ``%(scope_org)s/%(scope_user)s`` will pick them up; older
    format strings just see the existing fields and the new ones are
    ignored — no log line gets dropped.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        bound = _LOG_SCOPE.get()
        if bound is None:
            record.scope_org = "-"
            record.scope_user = "-"
        else:
            record.scope_org, record.scope_user = bound
        return True


logger.addFilter(_ScopeLogFilter())


def _resolve_and_bind(ctx: Context) -> MemoryScope:
    """Resolve the request scope and bind it to the logging context.

    Single call site for the "resolve, then make every subsequent log line
    in this tool carry the tenant" pattern. Tool bodies should call this
    in place of :func:`resolve_scope`; a :class:`ScopeUnresolved` thrown
    here still propagates so the boundary rejection happens before any
    binding side-effect.
    """
    scope = resolve_scope(ctx)
    _bind_scope_to_logging(scope)
    return scope


# ---------------------------------------------------------------------------
# Valid labels / relations (used for validation)
# ---------------------------------------------------------------------------

_VALID_LABELS: set[str] = {member.value for member in NodeType}
_VALID_RELATIONS: set[str] = {member.value for member in RelationType}

# Inline-relation fuzzy resolution (#93). When a relation target doesn't match
# an existing node exactly, measured similarity drives a three-way decision
# (connect / ask / create) instead of silently minting a stub. Thresholds are
# deliberately conservative: a wrong auto-connection is harder to spot than an
# orphan stub, so anything short of near-certainty falls to "ask" (did_you_mean),
# never "connect". ``CONNECT`` must clearly beat the runner-up by ``MARGIN`` or
# the match is treated as ambiguous. Tunable as the graph grows.
_FUZZY_CONNECT_RATIO = 0.9
_FUZZY_SUGGEST_RATIO = 0.6
_FUZZY_CONNECT_MARGIN = 0.08
_FUZZY_SUGGEST_LIMIT = 5
_FUZZY_CANDIDATE_SCAN_LIMIT = 1000


def _rank_fuzzy_candidates(
    target: str, candidates: list[dict[str, str]]
) -> list[tuple[float, str, str]]:
    """Rank in-scope nodes by name similarity to ``target``.

    Pure and deterministic (``difflib`` ratio, no embeddings) so the same graph
    state always yields the same suggestion order — ties broken by name. Returns
    ``[(score, label, name), ...]`` sorted best-first, keeping only candidates at
    or above :data:`_FUZZY_SUGGEST_RATIO`. The caller (not this helper) owns the
    connect/ask/create decision so the policy lives in one place.
    """
    from difflib import SequenceMatcher

    target_cf = target.casefold()
    scored: list[tuple[float, str, str]] = []
    for cand in candidates:
        name = cand.get("name")
        label = cand.get("label")
        if not name or not label:
            continue
        ratio = SequenceMatcher(None, target_cf, name.casefold()).ratio()
        if ratio >= _FUZZY_SUGGEST_RATIO:
            scored.append((ratio, label, name))
    # Best score first; stable tiebreak on name so output is reproducible.
    scored.sort(key=lambda t: (-t[0], t[2]))
    return scored


# MCP talks to the store directly (it doesn't go through EngramaEngine
# because the server is async-first while the engine is sync), so the
# layer-1 sanitiser has to be applied at this boundary explicitly.
_SANITISER = Sanitiser(valid_labels=_VALID_LABELS, valid_relations=_VALID_RELATIONS)
_MCP_PROVENANCE_PROPS = Provenance(source="mcp").to_properties()

# Scope is resolved PER REQUEST from the inbound identity headers (Spec 001,
# FR-3) — never pinned at process/import time. A gateway in front sets the
# headers per request; bare OSS has no gateway and no headers, so it falls
# back to a single stable standalone identity (FR-7, computed once in the
# lifespan and read from the request context here).
_HDR_ORG = "x-engrama-org-id"
_HDR_USER = "x-engrama-user-id"

# Tools that are NOT isolated per tenant and that a multi-tenant gateway
# should consider gating so a normal tenant cannot reach them (Spec 001
# tenant-isolation audit, 2026-05-30). Surfaced verbatim in
# ``engrama_status.admin_tools`` so a gateway can discover them at runtime
# instead of hardcoding names. ``engrama`` OSS does not authenticate — it only
# declares this boundary; the gateway (engrama-saas) enforces it.
_ADMIN_TOOLS: tuple[dict[str, str], ...] = (
    {
        "name": "engrama_status",
        "reason": "runtime introspection (FR-11); counts are deployment-wide, "
        "no tenant isolation. No identity required.",
    },
    {
        "name": "engrama_reindex",
        "reason": "candidate scan is now scoped to the caller's tenant, so it "
        "leaks no cross-tenant data; still admin-flavoured (bulk re-embed cost) "
        "— a gateway may gate it for cost/abuse.",
    },
)


class ScopeUnresolved(Exception):
    """A request carried partial/malformed identity (exactly one of the two
    headers). Reads translate this to zero results; writes reject it.

    Both headers absent is NOT unresolved — that is the standalone
    single-user path (FR-7)."""


def _request_headers(ctx: Context) -> Any:
    """Return the inbound request headers, or ``{}`` under stdio/in-process."""
    try:
        request = ctx.request_context.request
    except Exception:
        request = None
    if request is None:
        return {}
    return getattr(request, "headers", None) or {}


def _standalone_sub(ctx: Context) -> str:
    """Stable single-user identity for a no-gateway (standalone) run."""
    try:
        sub = ctx.request_context.lifespan_context.get("standalone_sub")
        if sub:
            return sub
    except Exception:
        pass
    return resolve_local_sub()


def resolve_scope(ctx: Context) -> MemoryScope:
    """Resolve the active tenant scope for THIS request (Spec 001, R-3).

    Both headers present → that ``(org_id, user_id)``. Both absent →
    standalone single-user ``(sub_local, sub_local)``. Exactly one present →
    :class:`ScopeUnresolved` (fail-closed: malformed identity is never
    silently broadened).
    """
    headers = _request_headers(ctx)
    org = (headers.get(_HDR_ORG) or "").strip()
    user = (headers.get(_HDR_USER) or "").strip()
    if org and user:
        return MemoryScope(org_id=org, user_id=user)
    if not org and not user:
        sub = _standalone_sub(ctx)
        return MemoryScope(org_id=sub, user_id=sub)
    raise ScopeUnresolved(
        "incomplete identity: both X-Engrama-Org-Id and X-Engrama-User-Id are required"
    )


def _with_mcp_provenance(extra: dict[str, Any] | None, scope: MemoryScope) -> dict[str, Any]:
    """Sanitise an MCP-supplied extras dict and stamp provenance + scope.

    ``scope`` is the per-request resolved :class:`MemoryScope`; its
    ``org_id``/``user_id`` are written onto the node so every write carries
    identity (Spec 001, FR-4). The caller's extras are cleaned first
    (reserved provenance + scope keys stripped, control chars removed, long
    strings truncated) so a malicious agent cannot forge its own ``source``,
    ``trust_level`` or identity.
    """
    cleaned = _SANITISER.sanitise_properties(extra or {})
    return {**cleaned, **_MCP_PROVENANCE_PROPS, **scope.to_properties()}


# Opportunistic re-embeds per healthy write (see _sweep_pending_embeddings).
_SWEEP_LIMIT = 3


async def _embed_text(embedder: Any, text: str) -> list[float]:
    """Embed via the async API when available, else the sync one."""
    if hasattr(embedder, "aembed"):
        return await embedder.aembed(text)
    return embedder.embed(text)


async def _reembed_node(store: Any, embedder: Any, label: str, props: dict[str, Any]) -> bool:
    """Embed one node from its stored props and attach the vector.

    Returns ``True`` if a vector was stored, ``False`` if the node has no
    embeddable text or the embedder produced nothing. Raises on embedder /
    transport failure so callers can tell "nothing to embed" from
    "embedder unreachable".
    """
    from engrama.embeddings.text import node_to_text

    text = node_to_text(label, props)
    if not text or not text.strip():
        return False
    key_field = "title" if label in TITLE_KEYED_LABELS else "name"
    key_value = props.get(key_field) or props.get("name") or props.get("title")
    if not key_value:
        return False
    embedding = await _embed_text(embedder, text)
    if not embedding:
        return False
    await store.store_embedding(label, key_field, key_value, embedding)
    return True


async def _sweep_pending_embeddings(store: Any, embedder: Any, limit: int = _SWEEP_LIMIT) -> int:
    """Opportunistically re-embed up to ``limit`` vector-less nodes.

    Called **only after a write whose own embed succeeded** — i.e. there is
    live evidence the embedder is reachable right now, so this never piles up
    timeouts against a down embedder. Best-effort: per-node failures are
    logged and skipped, never raised. Returns how many nodes were healed.
    """
    healed = 0
    try:
        candidates = await store.list_unembedded_nodes(limit=limit)
    except Exception as e:  # noqa: BLE001 — sweep must never break the write
        logger.warning("Embedding sweep: could not list candidates: %s", e)
        return 0
    for cand in candidates:
        try:
            if await _reembed_node(store, embedder, cand["label"], cand["props"]):
                healed += 1
        except Exception as e:  # noqa: BLE001 — one bad node must not abort the sweep
            logger.warning(
                "Embedding sweep: re-embed failed for engrama_id=%s: %s",
                cand.get("engrama_id"),
                e,
            )
    if healed:
        logger.info("Embedding sweep: healed %d vector-less node(s)", healed)
    return healed


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
    relations: dict[str, list[Any]] = Field(
        default_factory=dict,
        description=(
            "Optional relations to create in the same call. "
            'Format: {"REL_TYPE": ["target_name", ...]}. '
            'Example: {"USES": ["BDK"], "IN_DOMAIN": ["teaching"], "FOR": ["Accenture"]}. '
            "Targets are matched by name; a missing target is created as a "
            "stub. By default the stub's label is inferred from the relation "
            "type, which is lossy. To pin it, pass an object instead of a "
            'string: {"RELATED_TO": [{"name": "BDK", "label": "Tool"}]}.'
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


class ReindexInput(BaseModel):
    """Input for engrama_reindex."""

    model_config = ConfigDict(extra="forbid")
    mode: str = Field(
        description="One of 'detect' | 'classify' | 'apply'. Run them in that order.",
    )
    dry_run: bool = Field(
        default=True,
        description=(
            "Only meaningful for mode='apply'. Default true (simulate). Pass "
            "false explicitly to actually re-embed and write."
        ),
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Maximum number of vector-less nodes to process in this call.",
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def create_engrama_mcp(
    backend: str | None = None,
    config: dict[str, Any] | None = None,
    vault_path: str | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    mcp_path: str = "/mcp",
    stateless_http: bool = False,
    allowed_origins: list[str] | None = None,
    auth_issuer: str | None = None,
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
        host: Bind address used by the Streamable HTTP transport and
            baked into the ``Host`` allow-list. Ignored under stdio.
        port: TCP port for the Streamable HTTP transport. Ignored under
            stdio.
        mcp_path: URL path for the MCP endpoint (default ``/mcp``).
        stateless_http: Run the HTTP transport statelessly (default
            ``False`` — stateful). Stateful is required by conversational
            MCP clients (claude.ai / Claude Desktop): ``initialize``
            returns an ``Mcp-Session-Id`` the client reuses, and the
            server lifespan runs **once per session** instead of once per
            request. Under ``stateless_http=True`` the SDK assigns no
            session id and re-enters the lifespan (re-opening the store,
            vault and embedder) on every POST, which those clients treat
            as a dead session and fail to register tools. Stateless is
            only useful for horizontally-scaled, fan-out request patterns
            with a shared event store — not our case. Ignored under stdio.
        allowed_origins: Origin header allow-list for DNS-rebinding
            protection. Defaults to loopback only. Ignored under stdio.
        auth_issuer: OAuth issuer URL advertised by the
            ``/.well-known/oauth-protected-resource`` stub. When ``None``
            the stub returns 404 (no auth configured).

    Returns:
        A :class:`FastMCP` instance ready to run.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    from engrama.adapters.mcp.http import (
        default_allowed_origins,
        derive_allowed_hosts,
        register_http_routes,
    )

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
                # Bootstrap the graph schema (fulltext/vector indexes +
                # constraints) idempotently. Neo4j needs this on a fresh
                # graph — e.g. a headless deploy installed as a dependency,
                # with no repo checkout to run `engrama init` — or
                # engrama_search has no index to hit.
                # SQLite applies its schema at connection time and exposes no
                # ensure_schema, so this is a Neo4j-only step.
                if hasattr(async_store, "ensure_schema"):
                    try:
                        await async_store.ensure_schema()
                    except Exception as e:
                        logger.warning("Schema bootstrap failed (non-fatal): %s", e)
                health = await async_store.health_check()
                logger.info("Engrama MCP backend ready: %s", health)
            # Standalone single-user identity (Spec 001, FR-7). A gateway's
            # X-Engrama-* headers override this per request; with no headers
            # we resolve one stable sub and persist it next to the DB.
            _db_path = cfg.get("ENGRAMA_DB_PATH") or os.environ.get("ENGRAMA_DB_PATH")
            _state_dir = os.path.dirname(os.path.expanduser(_db_path)) if _db_path else None
            standalone_sub = resolve_local_sub(state_dir=_state_dir or None)
            yield {
                "async_store": async_store,
                "obsidian": obsidian,
                "parser": NoteParser(),
                "embedder": embedder,
                "startup_error": startup_error,
                "standalone_sub": standalone_sub,
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

    # DNS-rebinding protection (only consulted by the Streamable HTTP
    # transport — inert under stdio). Bad Origin → 403, bad Host → 421.
    transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=derive_allowed_hosts(host, port),
        allowed_origins=(
            allowed_origins if allowed_origins is not None else default_allowed_origins()
        ),
    )

    mcp = FastMCP(
        "engrama_mcp",
        lifespan=lifespan,
        host=host,
        port=port,
        streamable_http_path=mcp_path,
        stateless_http=stateless_http,
        transport_security=transport_security,
    )

    # Register the HTTP-only custom routes (/health, OAuth metadata stub).
    # Harmless under stdio — they are simply never served.
    register_http_routes(
        mcp,
        cfg,
        auth_issuer=auth_issuer,
        host=host,
        port=port,
        mcp_path=mcp_path,
    )

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
          "admin_tools": [                               // gateway-gating hint
            {"name": "engrama_status",  "reason": "..."},
            {"name": "engrama_reindex", "reason": "..."}
          ],
          "startup_error": "..."   // present only when something failed at boot
        }
        ```

        **Identity (Spec 001).** No identity requirement — this is
        runtime introspection (FR-11), explicitly admin and CI-allowlisted
        as ``# scope-exempt``. Counts are deployment-wide so an operator
        can verify boot state without a tenant identity bound.

        ``admin_tools`` lists the tools that are not isolated per tenant
        (deployment-wide or admin-flavoured). A multi-tenant gateway can read
        this to decide which tools to gate for a normal tenant, instead of
        hardcoding names — engrama declares the boundary, the gateway enforces.
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
                # ``list_notes`` is sync I/O; punt to a worker thread so
                # a slow vault (e.g. on a cloud-sync drive) can't freeze
                # the MCP event loop on an otherwise read-only call.
                notes = await asyncio.to_thread(obsidian.list_notes, "")
                vault_info["note_count"] = len(notes)
            except Exception as e:
                vault_info["error"] = str(e)

        # --- Embedder ---
        # ``configured`` reports *capability*, not mere presence: a
        # NullProvider is wired up (``embedder is not None``) but produces no
        # vectors (dimensions == 0), so it must read ``configured: false``.
        # Otherwise status contradicts itself — configured:true / provider:none
        # / fulltext-only — which is exactly what confused testers (#2).
        embedder_dims = int(getattr(embedder, "dimensions", 0)) if embedder is not None else 0
        embedder_info: dict[str, Any] = {"configured": embedder_dims > 0}
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
        would_hybrid = embedder_dims > 0 and hasattr(embedder, "aembed")
        search_info: dict[str, Any] = {
            "mode": "hybrid" if would_hybrid else "fulltext_only",
            # Not degraded: no search has been *attempted* yet, and a
            # fulltext-only deploy (no embedder) is a configured mode, not a
            # failure. Runtime degradation (provider unreachable mid-search)
            # only ever surfaces on engrama_search's own response.
            "degraded": False,
            "reason": "" if would_hybrid else "no functional embedder; search is fulltext-only",
        }

        response: dict[str, Any] = {
            "version": engrama_version,
            "backend": backend_info,
            "vault": vault_info,
            "embedder": embedder_info,
            "search": search_info,
            "admin_tools": [dict(t) for t in _ADMIN_TOOLS],
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

        **Identity (Spec 001 FR-3).** Both ``X-Engrama-Org-Id`` and
        ``X-Engrama-User-Id`` request headers, or neither for standalone.
        Unresolved (exactly one) → 0 results (no error). **Degradation
        (NFR-5).** With ``EMBEDDING_PROVIDER=null`` the tool falls back to
        the fulltext path; the scope filter still applies on the fallback.
        """
        store = _store(ctx)
        state = ctx.request_context.lifespan_context

        # Fail-closed: an unresolved (partial-identity) request returns no
        # results rather than broadening the read (Spec 001, FR-5).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved:
            return f"No results found for '{params.query}'."

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
                hybrid = HybridSearchEngine(store, store, _embedder, scope=scope)
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
                response = await _build_search_response(results, params.query, store, scope)
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
        results = await store.fulltext_search(params.query, params.limit, scope=scope)
        if not results:
            return f"No results found for '{params.query}'."

        response = await _build_search_response(results, params.query, store, scope)
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
        scope: MemoryScope,
    ) -> dict[str, Any]:
        """Build the search response with optional proactivity hints."""
        # --- Proactivity: check for pending Insights related to search ---
        related_insights: list[dict[str, Any]] = []
        if _proactive_state.get("enabled", True):
            try:
                insight_results = await store.fulltext_search(query, limit=3, scope=scope)
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

        * ``name`` — or ``title`` for the title-keyed labels (Decision,
          Problem, Vulnerability, Exercise, Photo, Experiment, Insight):
          the unique merge key. Send the one that matches the label; the
          other is accepted as an alias and canonicalised.
        * ``summary``: 2–3 sentence overview — what this is, why it matters.
        * ``details``: comprehensive context — techniques used, decisions
          made, approaches taken, alternatives considered, key examples.
          The richer this field, the more useful the memory becomes.
        * ``tags``: freeform list for filtering, e.g.
          ``["active-directory", "credential-access", "windows"]``.
        * ``origin``: where this knowledge came from, *semantically*
          (``"conversation"``, ``"ingest"``, ``"manual"``). This is a free
          property you control, preserved on the node (and fulltext-indexed on
          the Neo4j backend). Note: ``source`` is **not** settable — it is a
          system-managed provenance/trust bucket stamped with the transport
          (always ``"mcp"`` here), so a semantic origin must go in ``origin``,
          not ``source``.
        * ``status``: current state
          (``"active"``, ``"resolved"``, ``"superseded"``).

        If a node with the same merge key already exists, its properties are
        updated (MERGE semantics).  ``description`` is still accepted for
        backward compatibility and is used as a fallback when ``summary`` is
        absent.

        Every node gets a stable ``engrama_id`` — minted on first write,
        unchanged on update — returned in the response and persisted on the
        node whether or not a vault is configured. When a vault is configured,
        a corresponding .md note is created (or updated) with full YAML
        frontmatter carrying that same ``engrama_id`` and an empty relations
        block (DDR-002).

        **Identity (Spec 001 FR-4).** Required: both ``X-Engrama-Org-Id``
        and ``X-Engrama-User-Id`` request headers, or neither for
        standalone (resolves to ``sub_local``). Unresolved (exactly one)
        → explicit error, graph untouched. The node persists with
        ``(org_id, user_id)`` stamped from the resolved scope. **Vault.**
        Writes target Engrama's own vault (``VAULT_PATH``) — distinct
        from any user-managed Obsidian vault exposed by a separate
        ``obsidian-mcp`` server.
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

        # Canonicalise the merge key per TITLE_KEYED_LABELS regardless of
        # what the caller put in the property bag. ``engrama_remember``
        # bypasses the engine and writes to the async store directly, so
        # the engine-level canonicalisation from #51 doesn't reach this
        # path — agents that send {"name": ...} for a title-keyed label
        # would otherwise create rows under the wrong column and diverge
        # from SDK writes. Drop the non-canonical alias when both are
        # present; canonical wins (matches Sanitiser behaviour with
        # reserved keys).
        canonical_key = "title" if label in TITLE_KEYED_LABELS else "name"
        other_key = "name" if canonical_key == "title" else "title"
        if other_key in props:
            if canonical_key in props:
                props.pop(other_key)
            else:
                props[canonical_key] = props.pop(other_key)

        if canonical_key not in props:
            return (
                f"Error: properties must include {canonical_key!r} as a merge key "
                f"for label {label!r}."
            )

        merge_key = canonical_key
        merge_value = props[merge_key]

        # Identity is mandatory on every write (Spec 001, FR-4). Reject a
        # partial/unresolved request before any graph or vault mutation.
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

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

            # Check if a note already exists. Vault I/O is sync — send
            # it to a worker thread so a slow cloud-sync drive can't
            # block the MCP event loop and stall every concurrent tool.
            note_data = await asyncio.to_thread(obsidian.read_note, vault_path)
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

                await asyncio.to_thread(target.write_text, new_content, encoding="utf-8")
                logger.info("Vault note written: %s", vault_path)
            except Exception as e:
                logger.warning("Could not write vault note for %s: %s", merge_value, e)
                vault_path = None

        # --- Graph write using async store ---
        if vault_path:
            props["obsidian_path"] = vault_path
        if engrama_id:
            # Vault is configured: hand the note's id to the store so the
            # graph node adopts it (vault and graph share one engrama_id).
            props["obsidian_id"] = engrama_id
            props["engrama_id"] = engrama_id

        extra = {k: v for k, v in props.items() if k not in {merge_key, "created_at", "updated_at"}}

        result = await store.merge_node(
            label, merge_key, merge_value, _with_mcp_provenance(extra, scope)
        )
        node = result["node"]

        # The store always mints a stable engrama_id (#6); without a vault we
        # had none to pass, so adopt the one the store returned so the
        # response contract holds whether or not a vault is configured.
        engrama_id = node.get("engrama_id", engrama_id)

        # --- DDR-003 Phase C: Embed on write (async) ---
        # The node is already persisted above; embedding is best-effort
        # enrichment. We track the outcome so the response is HONEST about it
        # — a transient embedder failure (cold-start, restart, network) must
        # not return status:ok while silently dropping the vector. A
        # vector-less node stays detectable by ``list_unembedded_nodes`` and is
        # healed by the opportunistic sweep below or by ``engrama_reindex``.
        _embedder = state.get("embedder")
        embed_attempted = _embedder is not None and getattr(_embedder, "dimensions", 0) > 0
        embedded = False
        if embed_attempted:
            try:
                embedded = await _reembed_node(store, _embedder, label, props)
                if not embedded:
                    logger.info(
                        "No embeddable text for %s/%s (engrama_id=%s); stored without vector",
                        label,
                        merge_value,
                        engrama_id,
                    )
            except Exception as e:
                logger.warning(
                    "Embed-on-write failed for %s/%s (engrama_id=%s): %s — "
                    "node stored without vector, will be reindexed",
                    label,
                    merge_value,
                    engrama_id,
                    e,
                )
            # Opportunistic sweep: only when THIS write embedded successfully,
            # i.e. the embedder is provably reachable right now.
            if embedded:
                await _sweep_pending_embeddings(store, _embedder)

        # --- BUG-005: Process inline relations ---
        # Normalise targets to (name, explicit_label) pairs. A target is
        # either a bare string (label inferred from the relation type) or an
        # object {"name": ..., "label": ...} that pins the stub's label
        # explicitly. Inference is lossy — one relation type maps to a single
        # default label — so callers that know the real type can say so (#3).
        all_relations: dict[str, list[tuple[str, str | None]]] = {}
        for src in (params.relations, inline_relations):
            for rtype, targets in (src or {}).items():
                merged = all_relations.setdefault(rtype, [])
                seen = {n for (n, _) in merged}
                for t in targets if isinstance(targets, list) else [targets]:
                    if isinstance(t, dict):
                        name = t.get("name") or t.get("title")
                        explicit_label = t.get("label")
                    else:
                        name, explicit_label = t, None
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    merged.append((name, explicit_label))

        relations_created = 0
        # Relation types the caller requested that aren't in the schema. The
        # explicit engrama_relate path returns a hard error for these; the
        # inline path used to skip them with a server-log-only warning, so the
        # caller saw status:ok with no idea a relation was dropped. Surface them
        # in the response (non-fatal — the node and valid relations still land).
        relations_rejected: list[str] = []
        # Per-target outcomes for inline relations whose target didn't match an
        # existing node exactly (#93). Similarity drives a three-way decision:
        #   - ``relations_resolved`` — a near-certain in-scope match; connected
        #     to it (``resolved_by: fuzzy_match``), reported, never silent.
        #   - ``relations_ambiguous`` — grey-zone candidates, none clearly wins;
        #     NOTHING created, ``did_you_mean`` lists in-scope names so the
        #     caller (or user) disambiguates. When in doubt, ask, don't connect.
        #   - ``relations_stubbed`` — no similar candidate, so a stub is a
        #     plausible intent; created and reported (``created_stub``).
        #   - ``relations_failed`` — no edge at all (exact match found but the
        #     MERGE matched nothing, or the stub couldn't be created, or error).
        relations_failed: list[dict[str, str]] = []
        relations_stubbed: list[dict[str, str]] = []
        relations_resolved: list[dict[str, Any]] = []
        relations_ambiguous: list[dict[str, Any]] = []
        if all_relations:
            from engrama.adapters.obsidian.sync import ObsidianSync

            for rel_type, targets in all_relations.items():
                rel_type_upper = rel_type.upper()
                if rel_type_upper not in _VALID_RELATIONS:
                    logger.warning("Skipping unknown relation type: %s", rel_type)
                    if rel_type_upper not in relations_rejected:
                        relations_rejected.append(rel_type_upper)
                    continue

                for target_name, explicit_label in targets:
                    # Find or create the target node. ``lookup_node_label``
                    # already COALESCEs ``name`` and ``title``, so an
                    # existing title-keyed target (Decision, Problem,
                    # Experiment, ...) resolves to the right label here.
                    target_label = await store.lookup_node_label(target_name, scope=scope)
                    created_as_stub = False
                    # ``resolved_name`` is what we actually connect to: the
                    # caller's spelling by default, or an existing node's real
                    # name when a fuzzy match wins.
                    resolved_name = target_name

                    if target_label is None:
                        # No exact match. Don't blindly mint a stub — measure
                        # similarity against in-scope nodes and let confidence
                        # pick connect / ask / create (#93). ``list_existing_nodes``
                        # is scope-filtered fail-closed, so candidates (and any
                        # ``did_you_mean``) can never leak another tenant's names.
                        candidates = await store.list_existing_nodes(
                            limit=_FUZZY_CANDIDATE_SCAN_LIMIT, scope=scope
                        )
                        ranked = _rank_fuzzy_candidates(target_name, candidates)
                        top = ranked[0] if ranked else None
                        runner_up = ranked[1][0] if len(ranked) > 1 else 0.0
                        unambiguous = (
                            top is not None
                            and top[0] >= _FUZZY_CONNECT_RATIO
                            and (top[0] - runner_up) >= _FUZZY_CONNECT_MARGIN
                        )

                        if unambiguous:
                            # Path 1 — near-certain match: connect to the
                            # existing node, never silently. Use its real name
                            # and label so the edge lands on the right node.
                            _, target_label, resolved_name = top
                            relations_resolved.append(
                                {
                                    "rel_type": rel_type_upper,
                                    "target": target_name,
                                    "resolved_to": resolved_name,
                                    "resolved_by": "fuzzy_match",
                                    "score": round(top[0], 3),
                                }
                            )
                        elif top is not None:
                            # Path 2 — grey zone: candidates exist but none
                            # clearly wins. Refuse to guess; a wrong edge is
                            # harder to detect than a missing one. Suggest and
                            # move on without touching the graph.
                            relations_ambiguous.append(
                                {
                                    "rel_type": rel_type_upper,
                                    "target": target_name,
                                    "did_you_mean": [
                                        {"name": n, "label": lbl, "score": round(r, 3)}
                                        for (r, lbl, n) in ranked[:_FUZZY_SUGGEST_LIMIT]
                                    ],
                                }
                            )
                            continue
                        else:
                            # Path 3 — nothing similar in scope: a stub is a
                            # plausible intent. Prefer the caller's explicit
                            # label (validated); otherwise infer from the
                            # relation type. Key the stub canonically so a later
                            # remember by the real key doesn't duplicate it.
                            if explicit_label and explicit_label in _VALID_LABELS:
                                target_label = explicit_label
                            else:
                                if explicit_label:
                                    logger.warning(
                                        "Ignoring invalid stub label %r for %s; "
                                        "inferring from relation %s",
                                        explicit_label,
                                        target_name,
                                        rel_type_upper,
                                    )
                                target_label = ObsidianSync._infer_stub_label(rel_type_upper)
                            target_key = "title" if target_label in TITLE_KEYED_LABELS else "name"
                            created_as_stub = True
                            try:
                                await store.merge_node(
                                    target_label,
                                    target_key,
                                    target_name,
                                    _with_mcp_provenance({"status": "stub"}, scope),
                                )
                            except Exception as e:
                                logger.warning("Could not create stub %s: %s", target_name, e)
                                relations_failed.append(
                                    {
                                        "rel_type": rel_type_upper,
                                        "target": target_name,
                                        "reason": "stub_creation_failed",
                                    }
                                )
                                continue

                    if not created_as_stub:
                        # Canonicalise the target merge key the same way
                        # the engine does for the source (#51 / #53).
                        # Hardcoding ``"name"`` here silently fails for
                        # title-keyed targets: the MATCH returns zero
                        # rows, the MERGE creates nothing, but the
                        # counter still increments because no exception
                        # is raised.
                        target_key = "title" if target_label in TITLE_KEYED_LABELS else "name"

                    # Create the relationship
                    try:
                        rel_result = await store.merge_relation(
                            label,
                            merge_key,
                            merge_value,
                            rel_type_upper,
                            target_label,
                            target_key,
                            resolved_name,
                            scope=scope,
                        )
                        if rel_result:
                            relations_created += 1
                            if created_as_stub:
                                # Edge landed, but on a node we just invented.
                                # The caller may have meant an existing node
                                # under a different name/key — surface it so a
                                # silent orphan stub isn't mistaken for a link
                                # to the intended target.
                                relations_stubbed.append(
                                    {
                                        "rel_type": rel_type_upper,
                                        "target": target_name,
                                        "stub_label": target_label,
                                    }
                                )
                        else:
                            logger.warning(
                                "Inline relation not created (target not found): %s -[%s]-> %s:%s",
                                merge_value,
                                rel_type_upper,
                                target_label,
                                target_name,
                            )
                            relations_failed.append(
                                {
                                    "rel_type": rel_type_upper,
                                    "target": target_name,
                                    "reason": "match_failed",
                                }
                            )
                    except Exception as e:
                        logger.warning(
                            "Could not create relation %s -[%s]-> %s: %s",
                            merge_value,
                            rel_type_upper,
                            target_name,
                            e,
                        )
                        relations_failed.append(
                            {
                                "rel_type": rel_type_upper,
                                "target": target_name,
                                "reason": "error",
                            }
                        )

                    # Write relation to vault frontmatter (DDR-002)
                    if obsidian is not None and vault_path:
                        try:
                            await asyncio.to_thread(
                                obsidian.add_relation,
                                vault_path,
                                rel_type_upper,
                                resolved_name,
                            )
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
            # Honest embedding outcome — never silently drop a vector.
            "embedded": embedded,
        }
        if embed_attempted and not embedded:
            result_data["embedding_note"] = (
                "embedder unavailable at write time; node stored without a vector. "
                "It stays fulltext-searchable and will be re-embedded on the next "
                "successful write or via engrama_reindex."
            )
        if relations_rejected:
            result_data["relations_rejected"] = relations_rejected
            valid = ", ".join(sorted(_VALID_RELATIONS))
            result_data["relations_rejected_note"] = (
                f"these relation types are not in the schema and were skipped: "
                f"{', '.join(relations_rejected)}. Valid types: {valid}."
            )
        if relations_failed:
            result_data["relations_failed"] = relations_failed
            targets = ", ".join(sorted({f["target"] for f in relations_failed}))
            result_data["relations_failed_note"] = (
                f"these relations were NOT created — the target did not resolve to a "
                f"reachable node: {targets}. Check the target name, label, and that it "
                f"exists within your scope."
            )
        if relations_resolved:
            result_data["relations_resolved"] = relations_resolved
            pairs = ", ".join(f"{r['target']!r}->{r['resolved_to']!r}" for r in relations_resolved)
            result_data["relations_resolved_note"] = (
                f"these targets didn't match exactly but a near-certain in-scope node "
                f"was found, so the edge was connected to it: {pairs}. If any is wrong, "
                f"re-relate with the exact name."
            )
        if relations_ambiguous:
            result_data["relations_ambiguous"] = relations_ambiguous
            targets = ", ".join(sorted({a["target"] for a in relations_ambiguous}))
            result_data["relations_ambiguous_note"] = (
                f"these targets were too uncertain to auto-connect, so NOTHING was "
                f"created for them: {targets}. Pick the intended node from "
                f"'did_you_mean' and re-relate with its exact name, or remember it "
                f"first if it's genuinely new."
            )
        if relations_stubbed:
            result_data["relations_stubbed"] = relations_stubbed
            targets = ", ".join(sorted({s["target"] for s in relations_stubbed}))
            result_data["relations_stubbed_note"] = (
                f"these relations linked to a NEWLY-created stub, not a pre-existing "
                f"node: {targets}. If you meant an existing node, it may be stored under "
                f"a different name/key — verify and re-relate to avoid an orphan stub."
            )
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

        **Identity (Spec 001 FR-4 / FR-1).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, graph
        untouched. The edge is stamped with ``(org_id, user_id)`` so a
        future relation-scoped read can filter without re-walking
        endpoints. Endpoint lookup is scoped fail-closed — an existing
        node owned by another tenant resolves to "not found".
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

        # Identity is mandatory on every write (Spec 001, FR-4).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        # Determine merge keys
        from_key = "title" if params.from_label in TITLE_KEYED_LABELS else "name"
        to_key = "title" if params.to_label in TITLE_KEYED_LABELS else "name"

        # Use the async store to create the relationship. ``scope`` is passed
        # so the endpoints are matched within the caller's tenant — an
        # endpoint owned by another tenant resolves to "not found" rather than
        # silently forming a cross-tenant edge or leaking its existence (#93).
        r = await store.merge_relation(
            params.from_label,
            from_key,
            params.from_name,
            params.rel_type,
            params.to_label,
            to_key,
            params.to_name,
            scope=scope,
        )
        if not r:
            # Pinpoint which endpoint failed instead of a vague "could not find
            # either" (#93). ``lookup_node_label`` is scope-filtered, so a node
            # owned by another tenant reads as missing here too. We probe by
            # name within scope: a hit under a different label points at a
            # label mismatch; a miss points at a wrong name or wrong scope.
            missing: list[str] = []
            for side, side_label, side_name in (
                ("from", params.from_label, params.from_name),
                ("to", params.to_label, params.to_name),
            ):
                found_label = await store.lookup_node_label(side_name, scope=scope)
                if found_label is None:
                    missing.append(
                        f"{side} (:{side_label} {{name: '{side_name}'}}) — not found "
                        f"in your scope (check the name, or that it exists for this tenant)"
                    )
                elif found_label != side_label:
                    missing.append(
                        f"{side} '{side_name}' exists but as :{found_label}, "
                        f"not :{side_label} — fix the label"
                    )
            detail = (
                "; ".join(missing)
                if missing
                else (
                    "both endpoints resolve by name but the relationship still "
                    "could not be created (possible key mismatch)"
                )
            )
            return json.dumps(
                {
                    "status": "error",
                    "error": f"No relationship created — {detail}.",
                },
                default=str,
                indent=2,
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
                # Vault I/O is sync — every file touch from this async
                # tool goes through ``asyncio.to_thread`` to keep the
                # MCP event loop free while a slow cloud-sync drive
                # finishes its write.
                note_data = await asyncio.to_thread(obsidian.read_note, from_path)
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
                        await asyncio.to_thread(
                            target_file.write_text,
                            "---\n" + fm_yaml + f"---\n\n# {params.from_name}\n",
                            encoding="utf-8",
                        )
                        # Update the graph node with obsidian metadata
                        await store.merge_node(
                            params.from_label,
                            from_key,
                            params.from_name,
                            _with_mcp_provenance(
                                {"obsidian_path": from_path, "obsidian_id": _eid}, scope
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            "Could not create vault note for %s: %s", params.from_name, e
                        )
                        from_path = None

            if from_path:
                try:
                    vault_written = await asyncio.to_thread(
                        obsidian.add_relation,
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

        **Identity (Spec 001 FR-2).** Both ``X-Engrama-Org-Id`` and
        ``X-Engrama-User-Id`` headers, or neither for standalone.
        Unresolved → "not found". The root lookup AND neighbour
        traversal are scope-filtered: an existing node owned by another
        tenant is invisible.
        """
        if params.label not in _VALID_LABELS:
            return f"Error: Invalid label '{params.label}'."

        store = _store(ctx)
        merge_key = "title" if params.label in TITLE_KEYED_LABELS else "name"

        # Fail-closed: unresolved request → node not visible (Spec 001, FR-5).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved:
            return f"No node found: (:{params.label} {{name: '{params.name}'}})."

        data = await store.get_node_with_neighbours(
            params.label, merge_key, params.name, params.hops, scope=scope
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

        **Identity (Spec 001 FR-4).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, vault and
        graph untouched. The merged node carries the resolved
        ``(org_id, user_id)``.
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

        # Identity is mandatory on every write (Spec 001, FR-4).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "message": str(e)}, indent=2)

        note_data = await asyncio.to_thread(obsidian.read_note, params.path)
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
            node_label, merge_key, merge_value, _with_mcp_provenance(extra, scope)
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
                await asyncio.to_thread(target.write_text, new_content, encoding="utf-8")
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

        **Identity (Spec 001 FR-4).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, vault and
        graph untouched. Every merged node carries the resolved
        ``(org_id, user_id)``.
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

        # Identity is mandatory on every write (Spec 001, FR-4).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "message": str(e)}, indent=2)

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
            notes = await asyncio.to_thread(
                obsidian.list_notes, params.folder if params.folder else ""
            )

            for note_entry in notes:
                note_path = note_entry["path"] if isinstance(note_entry, dict) else note_entry
                try:
                    note_data = await asyncio.to_thread(obsidian.read_note, note_path)
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
                        node_label, merge_key, merge_value, _with_mcp_provenance(extra, scope)
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
                            await asyncio.to_thread(
                                target.write_text, new_content, encoding="utf-8"
                            )
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

        **Identity (Spec 001 FR-4).** Required even though this tool
        doesn't itself write nodes: it precedes a wave of
        ``engrama_remember`` calls, and an unscoped caller would drive
        downstream writes that the engine guard would then reject. The
        rejection is surfaced here so the caller sees it immediately.
        """
        # Spec 001 T012/FR-4: ingest precedes a wave of writes (one
        # ``engrama_remember`` per extracted entity), so it must also
        # require a resolved identity — an unscoped caller cannot drive
        # downstream writes that would land identity-less in the graph.
        try:
            _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

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
            note_data = await asyncio.to_thread(obsidian.read_note, params.source)
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

        **Identity (Spec 001 FR-4 / FR-12).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, no
        Insights written. Reflect profiles, detects, and writes
        Insights **only** within the caller's scope: another tenant's
        graph is invisible end-to-end.
        """
        store = _store(ctx)

        # reflect both reads and writes Insights, so it needs a resolved
        # identity (Spec 001, FR-4/FR-12); unresolved → reject.
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        insights: list[dict[str, Any]] = []
        queries_run: list[str] = []
        queries_skipped: list[str] = []

        # --- Step 1: Profile the graph ---
        # Spec 001 FR-12: the profile must reflect only the caller's slice of
        # the graph so reflect doesn't react to other tenants' label counts.
        profile: dict[str, int] = {}
        try:
            profile = await store.count_labels(scope=scope)
        except Exception as e:
            logger.warning("Could not profile graph: %s", e)

        # --- Step 2: Get already-judged titles ---
        # ``judged`` covers both dismissed AND approved insights so a
        # re-run of reflect doesn't re-MERGE them back to status="pending"
        # (which would silently undo the user's review). Scoped: a tenant's
        # judgement of their own Insights doesn't suppress patterns for
        # another tenant.
        dismissed: set[str] = set()
        approved: set[str] = set()
        try:
            dismissed = await store.get_dismissed_titles(scope=scope)
        except Exception:
            pass
        try:
            approved = await store.get_approved_titles(scope=scope)
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
                        },
                        scope,
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
                        },
                        scope,
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
                        },
                        scope,
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
                        },
                        scope,
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
                        },
                        scope,
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
                        },
                        scope,
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
                    scope=scope,
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
                    },
                    scope,
                ),
            )
            insights.append({"query": "under_connected", "title": title, "confidence": 0.4})

        # --- Step 3: Run applicable patterns ---
        # Each detector is closed over the resolved scope so the pattern
        # match only sees the caller's nodes (Spec 001 FR-12).
        await _run_pattern(
            "cross_project_solution",
            lambda: store.detect_cross_project_solutions(scope=scope),
            required_labels=["Problem", "Project"],
            builder_fn=_build_cross_project,
        )
        await _run_pattern(
            "shared_technology",
            lambda: store.detect_shared_technology(scope=scope),
            required_labels=["Technology"],
            builder_fn=_build_shared_tech,
        )
        await _run_pattern(
            "training_opportunity",
            lambda: store.detect_training_opportunities(scope=scope),
            any_labels=[["Problem", "Vulnerability"], ["Course"]],
            builder_fn=_build_training,
        )
        await _run_pattern(
            "technique_transfer",
            lambda: store.detect_technique_transfer(scope=scope),
            required_labels=["Technique"],
            min_label_count={"Domain": 2},
            builder_fn=_build_technique_transfer,
        )
        await _run_pattern(
            "concept_clustering",
            lambda: store.detect_concept_clusters(scope=scope),
            required_labels=["Concept"],
            builder_fn=_build_concept_clustering,
        )
        await _run_pattern(
            "stale_knowledge",
            lambda: store.detect_stale_knowledge(scope=scope),
            any_labels=[["Project", "Course"]],
            builder_fn=_build_stale,
        )

        # Under-connected: always run if enough nodes
        total_nodes = sum(profile.values())
        if total_nodes >= 5:
            queries_run.append("under_connected")
            try:
                uc_records = await store.detect_under_connected_nodes(scope=scope)
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

        **Identity (Spec 001 FR-2).** Both ``X-Engrama-Org-Id`` and
        ``X-Engrama-User-Id`` headers, or neither for standalone.
        Unresolved → error. Only Insights owned by the caller are
        surfaced; another tenant's pending Insights remain invisible.
        """
        store = _store(ctx)

        # Spec 001 FR-2: Insights are tenant-scoped; surface only the
        # caller's pending ones.
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        try:
            results = await store.get_pending_insights(limit=params.limit, scope=scope)
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

        **Identity (Spec 001 FR-2 / FR-4).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, graph
        untouched. The Insight is looked up under the caller's scope;
        promoting another tenant's Insight is blocked at the read.
        """
        store = _store(ctx)

        # Spec 001 FR-2/FR-4: a caller can only act on their own Insights —
        # resolve scope before touching the graph, error fail-closed when
        # unresolved.
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        if params.action not in ("approve", "dismiss"):
            return json.dumps(
                {
                    "status": "error",
                    "error": f"Invalid action '{params.action}'. Use 'approve' or 'dismiss'.",
                }
            )

        new_status = "approved" if params.action == "approve" else "dismissed"

        # Guard against cross-tenant approval: the read fails closed when the
        # title is owned by a different scope, so the update never runs.
        try:
            owned = await store.get_insight_by_title(params.title, scope=scope)
            if not owned:
                return json.dumps(
                    {
                        "status": "error",
                        "error": f"Insight not found: {params.title}",
                    }
                )
        except Exception as e:
            logger.warning("Could not load Insight before approve: %s", e)
            return json.dumps(
                {
                    "status": "error",
                    "message": f"Could not load Insight: {str(e)}",
                },
                indent=2,
            )

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

        **Identity (Spec 001 FR-2 / FR-4).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone. Unresolved → explicit error, vault
        untouched. The Insight lookup is scope-filtered; writing another
        tenant's approved Insight is blocked at the read. **Vault.**
        Targets Engrama's own vault (``VAULT_PATH``) — never the
        user-managed external Obsidian vault.
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

        # Spec 001 FR-2: vault-write of an Insight is scoped to the caller —
        # the read fails closed when the Insight belongs to a different scope.
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        # Fetch the Insight
        try:
            insight = await store.get_insight_by_title(params.title, scope=scope)
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
        note_data = await asyncio.to_thread(obsidian.read_note, params.target_note)
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
            await asyncio.to_thread(target.write_text, new_content, encoding="utf-8")
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
            "  Photo, Experiment, Insight) use `title` instead of `name` as the\n"
            "  merge key.\n"
            "- When the user mentions a person, technology, or project for the\n"
            "  first time, create the node proactively.\n"
            "- Keep node properties concise — the graph is for structure and\n"
            "  connections, not full documents.\n"
        )

    @mcp.tool(
        name="engrama_reindex",
        annotations=ToolAnnotations(
            title="Reindex Embeddings",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    async def engrama_reindex(params: ReindexInput, ctx: Context) -> str:
        """Find and repair nodes that are missing their vector embedding.

        Nodes can end up without an embedding when the embedder was
        unreachable at write time (a write reports ``embedded: false``), or
        when the graph predates embeddings. Such nodes are invisible to
        semantic search — reachable only by fulltext. This tool heals them.

        **Three phases, run as separate calls in this order:**

        1. ``mode="detect"`` — scan for vector-less nodes. Read-only; returns a
           count and a sample of `engrama_id`s. Nothing is written.
        2. ``mode="classify"`` — split candidates into *re-embeddable* (they
           have summary/details/other text) vs *skip* (no embeddable text).
           Read-only; returns the plan.
        3. ``mode="apply"`` — re-embed the eligible nodes. ``dry_run`` defaults
           to ``true`` (simulate); pass ``dry_run=false`` to actually write.

        Run ``classify`` before ``apply`` in the same session — the server
        cannot enforce this, so the caller must respect it. Use ``limit`` to
        process in batches; if ``detect`` reports ``unembedded_found ==
        limit`` there may be more, so raise the limit or re-run after applying.

        **Identity (Spec 001 FR-4).** Required: both
        ``X-Engrama-Org-Id`` and ``X-Engrama-User-Id`` headers, or
        neither for standalone — every mode, even read-only ``detect``,
        enforces this. The candidate scan is **scoped to the calling
        tenant**: ``detect``/``classify`` only ever sample the caller's own
        vector-less nodes, and ``apply`` only re-embeds them, so a tenant
        never sees or touches another tenant's data. (The internal
        opportunistic sweep and the admin CLI keep the unscoped cross-tenant
        backfill via ``scope=None``.) ``engrama_status`` and this tool are
        still listed in ``engrama_status.admin_tools`` so a gateway may
        additionally gate them for cost/abuse reasons.
        """
        store = _store(ctx)
        state = ctx.request_context.lifespan_context
        embedder = state.get("embedder")

        # Spec 001 T012/FR-4: reindex writes embeddings to existing nodes; the
        # caller must declare identity for audit and to gate the write path. We
        # require it for every mode so the contract is uniform, and we pass the
        # resolved scope to the scan so detect/classify never reveal another
        # tenant's node names (tenant-isolation audit, 2026-05-30).
        try:
            scope = _resolve_and_bind(ctx)
        except ScopeUnresolved as e:
            return json.dumps({"status": "error", "error": str(e)})

        if params.mode not in {"detect", "classify", "apply"}:
            return f"Error: invalid mode {params.mode!r}. Use 'detect' | 'classify' | 'apply'."

        from engrama.embeddings.text import node_to_text

        candidates = await store.list_unembedded_nodes(limit=params.limit, scope=scope)

        def _sample(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [
                {"engrama_id": c["engrama_id"], "label": c["label"], "name": c["key_value"]}
                for c in items[:20]
            ]

        if params.mode == "detect":
            return json.dumps(
                {
                    "mode": "detect",
                    "scanned_limit": params.limit,
                    "unembedded_found": len(candidates),
                    "sample": _sample(candidates),
                    "next": "run mode='classify' to see which can be re-embedded",
                },
                default=str,
                indent=2,
            )

        def _has_text(c: dict[str, Any]) -> bool:
            text = node_to_text(c["label"], c["props"])
            return bool(text and text.strip())

        reembed = [c for c in candidates if _has_text(c)]
        skip = [c for c in candidates if not _has_text(c)]

        if params.mode == "classify":
            return json.dumps(
                {
                    "mode": "classify",
                    "candidates": len(candidates),
                    "to_reembed": len(reembed),
                    "skip_no_text": len(skip),
                    "sample_reembed": _sample(reembed),
                    "sample_skip": _sample(skip),
                    "next": "run mode='apply' dry_run=false to re-embed",
                },
                default=str,
                indent=2,
            )

        # mode == "apply"
        if embedder is None or getattr(embedder, "dimensions", 0) <= 0:
            return json.dumps(
                {
                    "mode": "apply",
                    "error": "no functional embedder configured (dimensions=0); "
                    "cannot re-embed. Configure an embedding provider first.",
                },
                indent=2,
            )

        if params.dry_run:
            return json.dumps(
                {
                    "mode": "apply",
                    "dry_run": True,
                    "would_reembed": len(reembed),
                    "would_skip_no_text": len(skip),
                    "sample": _sample(reembed),
                    "next": "re-run with dry_run=false to write",
                },
                default=str,
                indent=2,
            )

        reembedded = 0
        failed = 0
        for c in reembed:
            try:
                if await _reembed_node(store, embedder, c["label"], c["props"]):
                    reembedded += 1
                else:
                    failed += 1  # had text but the embedder returned nothing
            except Exception as e:  # noqa: BLE001 — keep going; report failures
                failed += 1
                logger.warning(
                    "Reindex: re-embed failed for engrama_id=%s: %s", c.get("engrama_id"), e
                )

        return json.dumps(
            {
                "mode": "apply",
                "dry_run": False,
                "reembedded": reembedded,
                "failed": failed,
                "skipped_no_text": len(skip),
                "note": "re-run detect to confirm the remaining count (0 = healed for this batch)",
            },
            default=str,
            indent=2,
        )

    return mcp
