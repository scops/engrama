# Graph Schema

> Canonical reference for the Neo4j schema. `scripts/init-schema.cypher` must stay in sync with this document.

## Nodes — `developer` profile

### Project
```
(:Project {
  name:        string,    // UNIQUE, required
  status:      string,    // "active" | "paused" | "archived"
  repo:        string,
  stack:       [string],
  description: string,
  created_at:  datetime,
  updated_at:  datetime
})
```

### Technology
```
(:Technology {
  name:       string,    // UNIQUE, required
  version:    string,
  type:       string,    // "framework"|"infra"|"language"|"protocol"|"tool"
  notes:      string,
  created_at: datetime,
  updated_at: datetime
})
```

### Decision
```
(:Decision {
  title:        string,  // UNIQUE, required
  rationale:    string,
  date:         date,
  alternatives: string,
  created_at:   datetime,
  updated_at:   datetime
})
```

### Problem
```
(:Problem {
  title:      string,  // UNIQUE, required
  solution:   string,
  status:     string,  // "open"|"resolved"|"blocked"
  context:    string,
  created_at: datetime,
  updated_at: datetime
})
```

### Course
```
(:Course {
  name:       string,  // UNIQUE, required
  cohort:     string,
  date:       date,
  level:      string,  // "basic"|"intermediate"|"advanced"
  client:     string,
  created_at: datetime,
  updated_at: datetime
})
```

### Concept
```
(:Concept {
  name:       string,  // UNIQUE, required
  domain:     string,
  notes:      string,
  created_at: datetime,
  updated_at: datetime
})
```

### Client
```
(:Client {
  name:       string,  // UNIQUE, required
  sector:     string,
  contact:    string,
  created_at: datetime,
  updated_at: datetime
})
```

### Insight
```
(:Insight {
  title:        string,  // UNIQUE, required
  body:         string,
  confidence:   float,   // 0.0–1.0
  status:       string,  // "pending"|"approved"|"dismissed"
  source_query: string,
  created_at:   datetime,
  updated_at:   datetime,
  approved_at:  datetime,
  dismissed_at: datetime,
  synced_at:    datetime,
  obsidian_path: string
})
```

### Material
```
(:Material {
  name:       string,  // UNIQUE, required
  type:       string,  // "cheatsheet"|"slides"|"exercise"|"reference"
  format:     string,
  status:     string,
  notes:      string,
  created_at: datetime,
  updated_at: datetime
})
```

## Temporal fields (all nodes)

Every node carries temporal metadata managed by the engine (DDR-003 Phase D):

```
{
  created_at:  datetime,   // auto-set on first MERGE
  updated_at:  datetime,   // auto-updated on every MERGE
  valid_from:  datetime,   // when the fact became true (auto-set on creation)
  valid_to:    datetime,   // when superseded (null = still true)
  confidence:  float,      // 0.0–1.0, decays over time (default 1.0)
  decayed_at:  datetime,   // last time confidence was decayed
  embedding:   [float],    // 768-dim vector (when EMBEDDING_PROVIDER != none)
}
```

Nodes with embeddings also carry the `:Embedded` secondary label for vector indexing.

## Relationships

```
(Project)    -[:USES]----------> (Technology)
(Project)    -[:INFORMED_BY]---> (Decision)
(Project)    -[:HAS]-----------> (Problem)
(Project)    -[:FOR]-----------> (Client)
(Project)    -[:ORIGIN_OF]-----> (Course)
(Project)    -[:APPLIES]-------> (Concept)
(Problem)    -[:SOLVED_BY]-----> (Decision)
(Course)     -[:COVERS]--------> (Concept)
(Course)     -[:TEACHES]-------> (Technology)
(Technology) -[:IMPLEMENTS]----> (Concept)
(Course)     -[:HAS_MATERIAL]-> (Material)
```

## Common queries

### Full context of a project (1-hop)
```cypher
MATCH (p:Project {name: $name})-[r]-(n)
RETURN p, r, n
```

### Semantic search
```cypher
CALL db.index.fulltext.queryNodes("memory_search", $query)
YIELD node, score
RETURN labels(node)[0] AS type, node.name AS name, score
ORDER BY score DESC LIMIT 10
```

### Active projects with tech stack
```cypher
MATCH (p:Project {status: "active"})-[:USES]->(t:Technology)
RETURN p.name AS project, collect(t.name) AS stack
```

### Problem → solution → decision chain
```cypher
MATCH (pr:Problem)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(p:Project)
RETURN pr.title, pr.solution, d.title, d.rationale, p.name
```

### Two-hop exploration
```cypher
MATCH path = (start {name: $name})-[*1..2]-(end)
RETURN path LIMIT 50
```

## Design notes

- **`MERGE` always** — engine never uses bare `CREATE`
- **Automatic timestamps** — engine manages `created_at` / `updated_at`
- **No relationship properties in v1** — added only when demonstrated need arises
- **Embeddings are optional** — local embeddings via Ollama enhance search when enabled (DDR-003 Phase B+C). Vector index on `(:Embedded)` covers all node types.
- **Always use Cypher parameters** — never string-format queries
- **Temporal fields auto-managed** — `valid_from`, `confidence` set on creation; `valid_to` cleared on revival (MATCH). Decay applied via `engrama decay` CLI.
