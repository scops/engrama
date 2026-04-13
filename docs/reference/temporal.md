# Temporal Reasoning

**Purpose**: Reference for Engrama's temporal model — how knowledge evolves, ages, and gets superseded over time.

---

## Overview

Engrama tracks two temporal dimensions for every node:

- **System time** — when the node was created and last modified in the graph (`created_at`, `updated_at`). Managed automatically by the engine.
- **Fact time** — when the fact was true in the real world (`valid_from`, `valid_to`). Set by the caller or auto-populated on creation.

This bi-temporal model answers two distinct questions: "when did we learn this?" (system time) and "when was this true?" (fact time).

---

## Temporal fields

Every node carries these fields (managed by `merge_node`):

| Field | Type | Auto-set | Description |
|---|---|---|---|
| `created_at` | datetime | Yes (on CREATE) | When the node was first created |
| `updated_at` | datetime | Yes (on every MERGE) | Last modification timestamp |
| `valid_from` | datetime | Yes (on CREATE, defaults to now) | When the fact became true |
| `valid_to` | datetime | No (caller sets) | When the fact was superseded. `null` = still true |
| `confidence` | float | Yes (defaults to 1.0) | Trust score, 0.0–1.0. Decays over time |
| `decayed_at` | datetime | Yes (after decay) | Last time confidence was reduced by decay |

---

## Confidence

Confidence represents how much trust the system places in a piece of knowledge. It starts at 1.0 and decreases over time via two mechanisms:

### Exponential decay

Applied periodically (e.g. weekly) via the CLI:

```
new_confidence = confidence × exp(-rate × days_since_update)
```

With the default rate of 0.01, a node's confidence after N days:

| Days | Confidence |
|------|-----------|
| 0 | 1.000 |
| 7 | 0.932 |
| 30 | 0.741 |
| 60 | 0.549 |
| 90 | 0.407 |
| 180 | 0.165 |
| 365 | 0.026 |

**Running decay:**

```bash
# Preview what would change
uv run engrama decay --dry-run

# Gentle decay (default)
uv run engrama decay --rate 0.01

# Moderate decay
uv run engrama decay --rate 0.02

# Aggressive + archive nodes below 5% confidence
uv run engrama decay --rate 0.1 --min-confidence 0.05

# Restrict to a specific label
uv run engrama decay --rate 0.01 --label Technology
```

**Design choices:**

- Nodes updated today (0 days old) are never decayed — updating resets the clock.
- Nodes with confidence below 0.05 are excluded (already effectively dead).
- Archived nodes (`status: 'archived'`) are excluded.
- The `decayed_at` timestamp is set on each affected node for auditing.

### Supersession penalty

When `valid_to` is set on a node, confidence is automatically halved. A superseded fact is less trustworthy by definition. If the caller also provides an explicit confidence value, that value is halved too.

Example: setting `valid_to` with `confidence: 0.8` → stored as `0.4`.

---

## Supersession (valid_to)

`valid_to` marks the date when a fact stopped being true. This is different from archival (`status: 'archived'`), which means "we don't care about this anymore". A superseded fact is still historically relevant — it was true at some point.

**Setting valid_to:**

Pass `valid_to` as a property when calling `engrama_remember`:

```python
# Mark Python 2 as superseded
eng.remember("Technology", "Python 2", "Legacy interpreter",
             valid_to="2020-01-01T00:00:00Z")
```

**Revival (clearing valid_to):**

When you update a node that has `valid_to` set — without passing a new `valid_to` — the engine clears `valid_to` (revives the node) and logs a warning:

```
Node Python 2 was marked as superseded on 2020-01-01. Updating anyway — valid_to has been cleared (revival).
```

This warning is included in the MCP tool response so the agent can inform the user.

---

## Temporal queries

`query_at_date` returns all nodes that were valid at a specific point in time:

```
WHERE valid_from <= date AND (valid_to IS NULL OR valid_to >= date)
```

This answers questions like "what technologies were we using in January?" or "what was the state of our knowledge on 2025-06-15?".

Available via both the async store (`Neo4jAsyncStore.query_at_date`) and the sync store (`Neo4jGraphStore.query_at_date`).

---

## Temporal scoring in hybrid search

The hybrid search engine includes a temporal signal in its scoring formula:

```
final = α × vector + (1-α) × fulltext + β × graph_boost + γ × temporal
```

The temporal score combines confidence with recency:

```
temporal_score = confidence × 2^(-days / half_life)
```

With default settings (γ=0.1, half_life=30 days), the temporal signal gently favours recently-updated, high-confidence nodes without overwhelming the other signals.

Setting `HYBRID_TEMPORAL_GAMMA=0` in `.env` disables temporal scoring entirely.

---

## Stale knowledge detection

The reflect skill's `stale_knowledge` pattern identifies nodes connected to active Projects or Courses that may be outdated. A node is considered stale if:

1. It hasn't been updated in 90+ days, **OR**
2. Its confidence is below 0.3 (regardless of age)

Detected stale nodes are surfaced as Insight nodes with the recommendation "Consider updating or archiving this node." The Insight body includes the confidence value so the user can judge severity.

---

## Best practices

- **Run decay weekly or monthly**, not daily. Overly frequent decay makes confidence meaningless.
- **Use `valid_to` for facts that are genuinely superseded**, not for things that just became less relevant. For the latter, let natural decay handle it.
- **Archive instead of supersede** when a node is no longer useful at all. Archived nodes don't appear in search results and aren't decayed.
- **Check the dry-run first** before applying aggressive decay rates. `--dry-run` shows exactly what would change.
