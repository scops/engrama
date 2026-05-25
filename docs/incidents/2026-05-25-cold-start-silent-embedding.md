# Incident: silent embedding corruption on write

**Date:** 2026-05-25
**Severity:** high (data quality — silent, permanent)
**Status:** diagnosed (Phase 0); fix pending review before implementation
**Affected:** `engrama_remember` write path when an embedder is configured but
transiently unreachable (observed first on a SaaS pod whose TEI embedder was
cold-starting).

## Summary

When an embedder is configured (`dimensions > 0`) but a write happens while it
is unreachable, `engrama_remember` **persists the node without a vector and
returns `status: "ok"`**. The node is then permanently invisible to semantic
search (reachable only by fulltext), with no signal in the tool response. The
node is never retried.

"Cold-start" is the trigger we observed, but the root cause is the embed-on-write
error handling — **any** transient embedder failure (restart, network blip, OOM,
rate-limit, 5xx) produces the same permanently-unembedded node.

### Live reproduction (real writes on the pod, not synthetic)

| write | timing | `vector_score` on paraphrastic query | embedding |
|---|---|---|---|
| `lightning-network` (Concept) | 1st write after TEI deploy | `0.0` (even on literal English) | **lost** |
| `pagos-instantaneos-btc` | ~9s later | `1.0` | ok |
| `liquidez-entrante-lightning` | ~2min later | `0.3726` | ok |

Writes 2 and 3 prove the embedder works; write 1 hit TEI before it was ready and
lost its vector silently.

## Phase 0 — answers

### 1. Exact write path

`engrama/adapters/mcp/server.py`, `engrama_remember`:

1. Merge key canonicalised; (optional) vault note written.
2. **Node persisted** — `result = await store.merge_node(...)` (`server.py:951`).
   This mints/keeps the `engrama_id` and writes the node. **The node exists from
   this point on, regardless of what happens next.**
3. **Embed-on-write** (`server.py:959-980`):
   ```python
   if _embedder is not None and getattr(_embedder, "dimensions", 0) > 0:
       try:
           text = node_to_text(label, props)
           if hasattr(_embedder, "aembed"):
               embedding = await _embedder.aembed(text)   # (A) network call
           else:
               embedding = _embedder.embed(text)
           if embedding:                                  # (B) falsy guard
               await store.store_embedding(label, merge_key, merge_value, embedding)
       except Exception as e:
           logger.warning("Embed-on-write failed for %s/%s: %s", label, merge_value, e)  # (C)
   ```
4. Inline relations processed; response assembled with `status: "ok"`.

`store_embedding` is what attaches the vector: it does `SET n.embedding = $e,
n:Embedded` on Neo4j (`backends/neo4j/async_store.py:833-848`) and the equivalent
on SQLite (`backends/sqlite/async_store.py:393` → vector store). If it never
runs, the node has **no `embedding` property and no `:Embedded` label**.

### 2. Exact failure mode

Two silent paths, both leaving a vector-less node after the node is already
persisted:

- **(A)+(C) — exception swallowed.** `aembed` raises on a transient failure. The
  openai-compatible provider uses `httpx` with a timeout (`openai_compat.py`;
  `raise_for_status()` + timeout), so a cold/unreachable TEI surfaces as
  `ConnectTimeout` / `ConnectError` / `HTTPStatusError`. The `except` at (C)
  catches it, logs a **WARNING** (so it is *not* invisible in logs) **but the
  warning carries `label/merge_value`, not the `engrama_id`**, and the tool
  still returns `status: "ok"`.
- **(B) — fully silent.** If `aembed` returns a falsy value (empty list), the
  `if embedding:` guard skips `store_embedding` with **no log at all**.

Nothing is persisted in place of the vector — the property is simply omitted
(not zeros, not `None`).

### 3. How an absent embedding persists

- **Neo4j:** the node lacks the `embedding` property **and** the `:Embedded`
  label (both are set together, only by `store_embedding`). So it is absent from
  the vector index `memory_vectors` (`FOR (n:Embedded) ON (n.embedding)`).
- **SQLite:** no row is written to the vector store for that node.

Absence, not a degenerate zero-vector.

### 4. How hybrid search treats it

`engrama/core/search.py` is explicit (module docstring, line 13): *"Node has no
embedding → appears in fulltext results only."* The vector branch
(`search_similar`) is backed by the `(:Embedded)` index, so an unembedded node is
**never returned by the vector path** — it can only enter results via fulltext,
contributing `0` to the blended score (default `alpha = 0.6` on the vector
signal). It does **not** filter the node out, and it does **not** assign an
arbitrary score; it simply never appears as a vector hit. Net effect: the node is
semantically invisible and ranks low unless its literal text matches.

Note: `SearchMode.degraded` (`search.py:101-108`) reports *per-search* runtime
degradation, not per-node corruption — so a search over a corrupted node looks
healthy (`degraded: false`) even though that specific node lost its vector. The
corruption is invisible from the search side too.

### 5. Confirmation (local test)

`tests/test_cold_start_embedding_repro.py` injects an embedder with
`dimensions = 768` whose `aembed` always raises (cold-start), then calls
`engrama_remember` and spies on `store_embedding`. Result (`1 passed`):

- `status == "ok"` ✅ (tool reports success)
- `engrama_id` present ✅ (node persisted)
- `store_embedding` call count `== 0` ✅ (**vector silently dropped**)

Hypothesis confirmed: a transient embedder failure on write yields a persisted,
vector-less, success-reported node. SQLite backend; no network; the bug is in the
MCP handler and is backend-agnostic.

## Recommended fix (pending review — gate before Phase 1)

Three options were proposed during the incident. Recommendation: **C (honest
status) + `engrama_reindex` as the recovery engine** — which also achieves the
intent behind B without a fragile in-process worker.

- **A — fail-fast (reject the write): NO, not as default.** Engrama supports
  fulltext-only as a first-class mode (`NullProvider`) and its model is proactive
  writes + graceful degradation. Failing writes because the embedder is cold/down
  is worse than losing a vector — it would drop writes entirely and break
  no-embedder deployments unless special-cased to `dimensions > 0`.
- **B — pending + async retry: right goal, fragile mechanism.** The goal (never
  lose the node, embed it eventually) is correct, but a background retry worker
  inside a request-driven, per-session FastMCP lifespan is fragile: the task dies
  with the session, and durable cross-restart retry needs a persistent queue —
  which is the graph itself.
- **C — persist + honest status: recommended.** The node is *already* persisted
  before embedding today, so keep that, but:
  1. Stamp the node with an explicit `embedding_status` (`ok` | `pending` |
     `skipped`) — `pending` when the embed failed/returned falsy, `skipped` when
     there is no embeddable text.
  2. Surface it in the response (`embedded: false` / `embedding_status`) so the
     write is **never silent**.
  3. Log the WARNING with the **`engrama_id`**, and cover the falsy `if
     embedding:` path that currently logs nothing.
  4. Backward compatible: nodes without `embedding_status` keep working.
- **`engrama_reindex` (Phase 2)** is the durable retry/recovery: it detects
  vector-less / degenerate nodes (incl. already-corrupted ones like
  `lightning-network`), classifies which to re-embed, and applies. This delivers
  B's "eventually embedded, never lost" guarantee without a daemon, and there is
  already partial machinery to build on (`list_nodes_for_embedding` in the SQLite
  store).

So **C + reindex** subsumes B pragmatically. Optional later: an opportunistic
sweep that re-embeds a few `pending` nodes on subsequent writes.

## Out of scope (per incident triage)

Inline-relation title→name resolution, `vector_score` min-max normalization,
full `engrama_vacuum`, search-side `degraded` reporting, openai-compatible client
changes (e.g. embed retries), vault routing. Tracked separately.

## Resolution

Fixed via **C (honest status) + `engrama_reindex` + opportunistic sweep** — no
in-process retry daemon (its robust form converges to a graph sweep anyway).

- **Honest write path** (`adapters/mcp/server.py`): the embed-on-write outcome
  is tracked; `engrama_remember` now returns `embedded: true|false` plus an
  `embedding_note` when a vector was deferred. Failures log a WARNING with the
  `engrama_id`, and the previously-silent empty-embedding path now logs too.
  The node still always persists (proactive writes are never lost).
- **Opportunistic sweep**: after a write whose own embed succeeded (live proof
  the embedder is reachable), up to `_SWEEP_LIMIT` vector-less nodes are
  re-embedded in the same request — auto-healing without a daemon and without
  piling timeouts against a down embedder.
- **`engrama_reindex` tool** (`detect`/`classify`/`apply`, `dry_run` default
  true): on-demand repair that also fixes legacy/already-corrupted nodes. Backed
  by `list_unembedded_nodes` on both async stores (`:Embedded` label on Neo4j /
  vec-table presence on SQLite as the source of truth — no parallel marker).

**Tests** (`tests/test_cold_start_embedding_repro.py`,
`tests/test_reindex_tool.py`, SQLite-only): failed embed is surfaced not silent;
healthy write embeds; the sweep heals a prior pending node on the next healthy
write; reindex detect/classify/apply heals vector-less nodes; apply without an
embedder errors cleanly.

Note: this fix uses the existing `:Embedded` / vec-table signal rather than a
new `embedding_status` property (the earlier recommendation) — it is less
invasive and consistent with the codebase. The response still carries the
honest `embedded` flag.
