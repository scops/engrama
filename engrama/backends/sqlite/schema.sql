-- Engrama SQLite schema (graph + vector + fulltext in one file)
-- Versioned via PRAGMA user_version; bump on incompatible changes.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA user_version = 2;

-- Core node table. props is a JSON blob carrying every domain property
-- (description, status, summary, tags, confidence, valid_from, ...) so
-- we don't need a column per profile field.
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    key_field   TEXT NOT NULL,        -- 'name' | 'title'
    key_value   TEXT NOT NULL,
    props       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(label, key_value)
);
CREATE INDEX IF NOT EXISTS idx_nodes_label   ON nodes(label);
CREATE INDEX IF NOT EXISTS idx_nodes_updated ON nodes(updated_at);

-- Directed edges between nodes. Idempotent via UNIQUE constraint.
-- org_id/user_id are stamped at write time (Spec 001, FR-1) so a relation
-- carries the writer's identity independently of its endpoints. Both are
-- nullable for back-compat with pre-Spec-001 rows; the migration CLI
-- backfills them. The runtime guard at ``EngramaEngine.merge_relation``
-- refuses to write a fresh edge without them.
CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,
    to_id       INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    org_id      TEXT,
    user_id     TEXT,
    UNIQUE(from_id, rel_type, to_id)
);
CREATE INDEX IF NOT EXISTS idx_edges_from ON edges(from_id, rel_type);
CREATE INDEX IF NOT EXISTS idx_edges_to   ON edges(to_id, rel_type);
-- idx_edges_scope is created at runtime in ``SqliteGraphStore._init_schema_from_file``
-- because pre-Spec-001 DBs lack the org_id/user_id columns at the time
-- ``executescript`` runs (the ALTER TABLE backfill happens just after).

-- FTS5 index over the searchable text fields. We let FTS5 store its own
-- content (no `content=''` flag) so DELETE/UPDATE work without the
-- "delete-all" sentinel pattern. The cost is duplicated text storage —
-- acceptable for a default zero-dep store. rowid maps 1:1 to nodes.id.
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, title, description, notes, rationale, solution, context, body,
    summary, tags
);
