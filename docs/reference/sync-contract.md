# Bidirectional Sync Contract (DDR-002)

**Purpose**: Complete specification for how Engrama keeps the Neo4j graph and Obsidian vault in sync, including recovery procedures.

---

## Overview

Engrama's persistence layer consists of two co-equal stores:

- **Neo4j Graph** — the relational index, optimised for querying and discovery.
- **Obsidian Vault** — the document store, optimised for portability and human readability.

Both stores contain the complete graph: node properties AND relations. The sync contract ensures they stay coherent.

### Why Two Stores?

- **Neo4j** is fast for complex queries (find similar problems, cross-domain discovery).
- **Obsidian** is portable (can move the vault folder to any machine, any cloud service) and human-readable (you can read and edit notes directly).
- **Bidirectional sync** means if one is lost, the other can fully reconstruct it.

---

## Sync Direction

### Forward Sync: Graph → Vault

When the model creates a node or relation in Neo4j, an Obsidian note is created with full frontmatter.

**Call sequence:**
1. `engrama_remember()` creates a node in the graph (optionally with relations in the `relations` parameter).
2. The node is automatically serialized to an Obsidian note in the vault with full YAML frontmatter.
3. Frontmatter includes: `engrama_id`, `type`, node properties, and all relations.

**Scope**: Only affects `has_document: true` node types (Project, Course).

### Reverse Sync: Vault → Graph

When `engrama_sync_vault` is called, all notes in the vault are scanned, parsed, and relations are restored to the graph.

**Call sequence:**
1. Scan all `.md` files in the vault.
2. Parse YAML frontmatter (including relations block).
3. Create or update nodes in Neo4j from frontmatter properties.
4. Restore all relations listed in the frontmatter.

**Scope**: Rebuilds the entire graph from vault notes.

---

## Frontmatter Format

Every Engrama note in the vault contains YAML frontmatter with this structure:

```yaml
---
engrama_id: 550e8400-e29b-41d4-a716-446655440000
type: Problem
name: implicit-type-coercion-bug
status: resolved
severity: high
solution: Enable strict null checks in tsconfig
relations:
  INSTANCE_OF: [type-safety-violation]
  COMPOSED_OF: [TypeScript]
  SOLVED_BY: [enable-strict-null-checks]
  BELONGS_TO: [EOElite]
  IN_DOMAIN: [web-development]
  SIMILAR_TO: [sql-injection-cast-bypass]
---
```

### Frontmatter Fields

| Field | Type | Required | Purpose |
|-------|------|----------|---------|
| `engrama_id` | UUID string | Yes | Unique identifier assigned by the graph. Used to track node identity across syncs. |
| `type` | string | Yes | Node label (Problem, Project, Decision, etc.). |
| `name` or `title` | string | Yes | The primary key for merging (name for most, title for Problem/Decision/Experiment/Photo/Vulnerability). |
| `status` | string | Optional | Current state (active, resolved, open, pending, etc.). |
| (other properties) | mixed | Optional | All other node properties (solution, severity, description, etc.). |
| `relations` | dict[str, list] | Optional | All relations as `{ RELATIONSHIP_TYPE: [target_names] }`. |
| `created_at` | ISO 8601 | Optional | Creation timestamp. |
| `updated_at` | ISO 8601 | Optional | Last modification timestamp. |

### Relations Format

Relations are stored as a flat dict where keys are relationship types and values are arrays of target node names:

```yaml
relations:
  INSTANCE_OF: [type-safety-violation]           # Single target
  COMPOSED_OF: [TypeScript, Node.js]             # Multiple targets
  BELONGS_TO: [EOElite]
  IN_DOMAIN: [web-development, cybersecurity]    # Multiple domains
```

**Important**:
- Relation values are the `name` (or `title`) of the target node, NOT the `engrama_id`.
- This ensures human readability and portability.
- If a target node does not yet exist when syncing, the sync creates it as a stub (minimal node with just the name).

---

## Sync Contract: The Core Rules

### Rule 1: When You Create a Node in the Graph

If the node has `has_document: true` (Project or Course):

1. Create the node in Neo4j with `engrama_remember()`.
2. Automatically generate an Obsidian note with full YAML frontmatter (including `engrama_id`).
3. The note goes to a predefined folder (typically `10-projects/` for projects, `50-courses/` for courses).

If the node has `has_document: false` (Technology, Concept, Decision, Problem, etc.):

1. Create the node in Neo4j.
2. No note is generated. The node exists only in the graph.
3. It can still be queried and related to other nodes.

### Rule 2: When You Create a Relation in the Graph

After any `engrama_relate()` call or when using the `relations` parameter in `engrama_remember()`:

1. The relation is created in Neo4j.
2. BOTH endpoint notes must be updated in the vault:
   - Add the relation to the `relations:` block of the source note's frontmatter.
   - If the target node has a document, optionally add the reverse relation (depends on relationship type).

**Example**: You create `Problem:bug-42` INSTANCE_OF `Concept:type-safety-violation`.
- Update `00-inbox/bug-42.md` to add `INSTANCE_OF: [type-safety-violation]` (if it has a document).
- Note: Concept nodes don't have documents, so you don't update a vault note for the target. But the source note always records the relation.

### Rule 3: When You Sync the Vault (Forward)

Call `engrama_sync_vault` to rebuild the graph from all vault notes:

1. Scan all `.md` files in the vault (optionally restricted to a folder).
2. For each note with valid `engrama_id` and `type` frontmatter:
   - Merge the node into Neo4j using name/title as key.
   - Parse the `relations:` block and restore all relations.
3. If a relation points to a target that doesn't exist, create a stub node for it.
4. Report counts: nodes created, updated, and skipped.

### Rule 4: When the Vault Is Lost or Corrupted

Run: `engrama_sync_vault --from-graph`

1. Read all nodes from Neo4j.
2. For each node with `has_document: true`, regenerate the `.md` file in the vault.
3. Serialize all relations into the frontmatter.
4. Vault is fully reconstructed with all properties and relations.

### Rule 5: When Neo4j Is Lost or Reset

Run: `engrama_sync_vault` (normal mode)

1. Scan all vault notes.
2. Create/merge all nodes in the graph from frontmatter.
3. Restore all relations from the `relations:` blocks.
4. Full graph is reconstructed — nodes, properties, AND relations.

### Rule 6: Never Hard-Delete

When forgetting entities:

1. Archive nodes in Neo4j by setting `status: "archived"` and `archived_at: datetime()`.
2. Archive the corresponding relations.
3. Delete the vault note if it exists.
4. Always confirm with the user.

---

## Resolution Rules

### Conflict Handling

In practice, conflicts are rare because the model writes to both stores in the same action. But if a mismatch is detected:

**If a node exists in Neo4j but NOT in the vault:**
- It was created before Engrama, or the note was deleted.
- During reverse sync, the node remains in Neo4j but is "invisible" (no vault note).
- To fix: Run `engrama_sync_vault --from-graph` to regenerate the note.

**If a note exists in the vault but NOT in Neo4j:**
- The graph was reset or the node was deleted.
- During forward sync, the node is reconstructed from the note's frontmatter.
- To fix: Run `engrama_sync_vault` (normal mode) to rebuild from the vault.

**If properties differ between graph and vault:**
- This should not happen if the model follows the sync contract.
- If it does, the most recent `updated_at` timestamp wins.
- Check the git history of the note for clues.

### Stub Nodes

When syncing, if a relation points to a target that doesn't exist, a **stub node** is created:

```yaml
relations:
  SOLVED_BY: [my-decision]  # "my-decision" doesn't exist yet
```

During sync:
1. `Decision:my-decision` is created as a stub with only the name and a placeholder status.
2. You can later fill in the full details (rationale, date, etc.).

Stub nodes are normal nodes — they're just incomplete. You can relate to them, query them, and fill them in later.

---

## Portability

The vault is designed to be fully portable. To migrate Engrama to a new machine:

### Migration Steps

1. **Copy the vault folder** to the new machine using any sync service (OneDrive, Google Drive, Proton Drive, iCloud, Syncthing, etc.).
2. **Install Engrama** on the new machine (CLI, Claude Desktop, etc.).
3. **Point Obsidian at the synced vault folder** in the Obsidian app settings.
4. **Run `engrama_sync_vault`** to rebuild Neo4j from the vault notes.
5. **Done.** The full graph is reconstructed: nodes, properties, and all relations.

### What Gets Preserved

- All node properties (name, description, status, etc.)
- All relations (recorded in frontmatter)
- Full history (notes can be git-tracked)
- Human readability (vault notes are plain Markdown)

### What Needs Reconfiguration

- Neo4j database (will be rebuilt)
- Obsidian vault path (point to synced folder)
- Engrama CLI credentials (if using cloud Neo4j)

---

## Recovery Procedures

### If the Vault Is Lost

**Scenario**: You deleted the vault folder by accident, but Neo4j is intact.

**Recovery**:
```bash
engrama_sync_vault --from-graph
```

1. Engrama reads all nodes and relations from Neo4j.
2. For each node with `has_document: true`, generates a `.md` file with full frontmatter.
3. All properties and relations are serialized into YAML.
4. New vault folder is created with all notes.

**Result**: Vault is fully reconstructed. Nothing is lost.

### If Neo4j Is Lost

**Scenario**: Neo4j database crashed or was reset, but the vault is intact.

**Recovery**:
```bash
engrama_sync_vault
```

1. Engrama scans all `.md` files in the vault.
2. Parses each note's frontmatter (type, name, properties, relations).
3. Creates/merges all nodes into Neo4j.
4. Restores all relations from the `relations:` block.
5. Rebuilds all indices.

**Result**: Graph is fully reconstructed from the vault. Nothing is lost.

### If Both Are Lost

**Scenario**: Catastrophic failure — both vault and graph are gone.

**Recovery**: Impossible. But you have options:

1. **If you have git history of the vault** — clone the repo and run `engrama_sync_vault`.
2. **If you have Neo4j backups** — restore the backup and run `engrama_sync_vault --from-graph`.
3. **If you have neither** — the data is lost. Start fresh.

**Lesson**: Keep the vault on cloud sync (OneDrive, Google Drive, etc.) and maintain Neo4j backups (even auto-incremental snapshots).

---

## Implementation Checklist

When writing code that interacts with Engrama:

```
□ When creating a node with `engrama_remember()`:
  □ If it has `has_document: true`, does the vault note get generated?
  □ Does the frontmatter include engrama_id, type, and relations?

□ When creating a relation with `engrama_relate()`:
  □ Is the relation created in Neo4j?
  □ Are both endpoint notes updated in the vault (if they exist)?

□ When syncing with `engrama_sync_vault`:
  □ Are all notes scanned and parsed?
  □ Are stub nodes created for missing targets?
  □ Are counts reported (created, updated, skipped)?

□ When recovering from lost vault:
  □ Does `--from-graph` flag regenerate notes?
  □ Is full frontmatter preserved?

□ When recovering from lost graph:
  □ Does normal sync rebuild from vault notes?
  □ Are all relations restored from frontmatter?

□ Are timestamps (created_at, updated_at) always set?

□ Are UUIDs used for engrama_id (never duplicated)?
```

---

## Example Workflow

1. **Create a problem**: `engrama_remember(type=Problem, title="null pointer crash", solution="add null check")`
   - Node is created in Neo4j
   - Vault note is generated at `00-inbox/null-pointer-crash.md`
   - Frontmatter includes engrama_id and empty relations block

2. **Add relations**: `engrama_relate(source=problem, rel=INSTANCE_OF, target=Concept:memory-safety)`
   - Relation is created in Neo4j
   - Vault note is updated: `relations: INSTANCE_OF: [memory-safety]`

3. **Sync vault**: `engrama_sync_vault`
   - All notes are scanned
   - Relations are verified
   - Concept stub is created if it doesn't exist

4. **Move vault to new machine**:
   - Sync vault folder via cloud
   - Point Obsidian at new location
   - Run `engrama_sync_vault`
   - Full graph is rebuilt on new machine

---

## See Also

- **faceted-classification.md** — How to classify nodes using the six facets
- **node-schema.md** — Complete reference for all node and relation types
- **query-patterns.md** — Cypher patterns for common discovery tasks
