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
- **Embeddings are optional** — local embeddings via Ollama enhance search when enabled (DDR-003 Phase B); vector index storage planned for Phase C
- **Always use Cypher parameters** — never string-format queries
