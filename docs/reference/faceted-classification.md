# Faceted Classification

**Purpose**: Complete reference for the six-facet classification system that powers cross-domain discovery in Engrama.

---

## The Six Facets

Every entity in the graph is classified using a system of six facets, adapted from Ranganathan's PMEST classification (1933) and enriched with concepts from BFO (Basic Formal Ontology).

Each facet answers a universal question about the entity:

| Facet | Question | Graph relationship | Target node |
|-------|----------|--------------------|-------------|
| **identity** | What is it? | `INSTANCE_OF` | Concept |
| **composition** | What is it made of? | `COMPOSED_OF` | Technology, Concept |
| **action** | What does it do / what process? | `PERFORMS` / `SOLVED_BY` | Concept, Decision |
| **purpose** | What is it for? | `SERVES` | Concept |
| **context** | Where and when? | `BELONGS_TO` + timestamps | Project, Client |
| **domain** | Which field of knowledge? | `IN_DOMAIN` | Domain |

---

## Minimum Classification Rule

### INSTANCE_OF is MANDATORY for:

- **Problem**
- **Decision**
- **Vulnerability**

These types are meaningless without a Concept anchor — cross-domain discovery depends on it.

### INSTANCE_OF is RECOMMENDED for:

All other node types, when it adds discovery value beyond what the label already provides. A Course or Project is self-describing; a Problem is not.

### Every entity MUST have:

- **context** (`BELONGS_TO`) — without it, the node is unanchored

### Every entity SHOULD have:

- **domain** (`IN_DOMAIN`) — enables field-level filtering

Apply composition, action, and purpose when they are relevant to the entity's nature. Do not force facets that don't apply.

---

## Concept Nodes: The Bridge Between Domains

Concept nodes are the key to cross-domain discovery. They must be:

- **Domain-specific but project-agnostic**: `type-safety-violation` yes, `eoelite-bug-42` no.
- **At the right abstraction level**: not too broad (`programming`), not too narrow (`line-47-fix`).
- **Consistent**: ALWAYS search for existing Concepts before creating new ones.

### Recommended Concept Prefixes

Use these prefixes for consistency:

| Prefix | Examples | Use for |
|--------|----------|---------|
| `pattern:` | `pattern:retry-with-backoff`, `pattern:circuit-breaker` | Design patterns |
| `anti-pattern:` | `anti-pattern:god-object`, `anti-pattern:implicit-any` | Known bad practices |
| `vulnerability:` | `vulnerability:sql-injection`, `vulnerability:xss-stored` | Security concepts |
| `technique:` | `technique:memoization`, `technique:debounce` | Implementation techniques |
| `principle:` | `principle:least-privilege`, `principle:separation-of-concerns` | Design principles |
| (no prefix) | `type-safety`, `error-handling`, `authentication` | General domain concepts |

---

## Domain Nodes

Domains are the highest-level classification. A node can belong to multiple domains.

**Examples**: `web-development`, `cybersecurity`, `cooking`, `photography`, `system-design`, `machine-learning`, `ethical-hacking`, `devops`.

Create new domains as needed. Keep them broad enough to group related work but specific enough to be meaningful.

---

## Faceted Classification Examples

### Example 1: Software Bug

```
identity:    INSTANCE_OF → Concept:type-safety-violation
composition: COMPOSED_OF → Technology:TypeScript
action:      SOLVED_BY → Decision:enable-strict-null-checks
purpose:     SERVES → Concept:runtime-error-prevention
context:     BELONGS_TO → Project:EOElite
domain:      IN_DOMAIN → Domain:web-development
```

### Example 2: Security Vulnerability (Ethical Hacking Course)

```
identity:    INSTANCE_OF → Concept:injection-vulnerability
composition: COMPOSED_OF → Technology:PostgreSQL, Technology:Python
action:      SOLVED_BY → Decision:parameterized-queries
purpose:     SERVES → Concept:input-validation
context:     BELONGS_TO → Course:ethical-hacking-2026-Q2
domain:      IN_DOMAIN → Domain:cybersecurity
```

### Example 3: Cooking Technique

```
identity:    INSTANCE_OF → Concept:wild-yeast-fermentation
composition: COMPOSED_OF → Concept:flour, Concept:water
action:      PERFORMS → Concept:anaerobic-fermentation
purpose:     SERVES → Concept:leavening
context:     BELONGS_TO → Project:sourdough-experiments
domain:      IN_DOMAIN → Domain:cooking
```

---

## Classification Checklist

Run this checklist every time you create a node:

```
□ INSTANCE_OF — what is this? (search existing Concepts first!)
□ BELONGS_TO — where does this live?
□ IN_DOMAIN — which field of knowledge?
□ COMPOSED_OF — what technologies or materials? (if applicable)
□ PERFORMS / SOLVED_BY — what process or solution? (if applicable)
□ SERVES — what purpose does this serve? (if applicable)
□ Did I search for existing Concepts before creating new ones?
□ Is each Concept at the right abstraction level?
```
