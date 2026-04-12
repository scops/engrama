# Query Patterns for Common Questions

**Purpose**: Ready-to-use Cypher query patterns for common discovery and navigation tasks in Engrama.

---

## Pattern 1: Find Similar Problems Across Projects

**Question**: Where have similar problems been solved before?

**Use case**: You encounter a Problem. Search for other Problems that share the same Concept (identity), and retrieve their solutions.

```cypher
MATCH (a:Problem)-[:INSTANCE_OF]->(c:Concept)<-[:INSTANCE_OF]-(b:Problem)
WHERE a <> b
OPTIONAL MATCH (b)-[:SOLVED_BY]->(d:Decision)
RETURN a.title, b.title, c.name AS shared_concept, d.title AS solution
```

**Returns**:
- `a.title` — Your problem
- `b.title` — Similar problem found
- `c.name` — The concept they share (e.g., "type-safety-violation")
- `d.title` — The decision that solved the similar problem

---

## Pattern 2: Everything I Know About a Domain

**Question**: What entities belong to a particular field of knowledge?

**Use case**: You want a comprehensive view of everything in your graph related to cybersecurity, web-development, cooking, etc.

```cypher
MATCH (n)-[:IN_DOMAIN]->(d:Domain {name: $domain})
RETURN labels(n)[0] AS type, coalesce(n.name, n.title) AS name, n.status
ORDER BY type, name
```

**Parameters**:
- `$domain` — Domain name (e.g., "web-development", "cybersecurity")

**Returns**:
- `type` — Node label (Problem, Project, Decision, etc.)
- `name` — Entity name or title
- `status` — Current status (active, resolved, etc.)

---

## Pattern 3: What Technologies Do My Active Projects Share?

**Question**: Which tools and technologies appear in multiple active projects?

**Use case**: Identify cross-project dependencies, common tech stacks, or opportunities to reuse knowledge.

```cypher
MATCH (p1:Project {status:"active"})-[:USES]->(t:Technology)<-[:USES]-(p2:Project {status:"active"})
WHERE id(p1) < id(p2)
RETURN p1.name, p2.name, collect(t.name) AS shared_tech
```

**Returns**:
- `p1.name`, `p2.name` — Two active projects
- `shared_tech` — Array of technologies both use

**Note**: The `WHERE id(p1) < id(p2)` prevents duplicate pairs (avoids both p1-p2 and p2-p1).

---

## Pattern 4: Any Solved Problem Relevant to This New Bug?

**Question**: Have I already solved a problem like this?

**Use case**: You have a new open Problem. Search for resolved Problems with the same Concept, and fetch their solutions.

```cypher
MATCH (new:Problem {status:"open"})-[:INSTANCE_OF]->(c:Concept)<-[:INSTANCE_OF]-(old:Problem {status:"resolved"})
WHERE new <> old
OPTIONAL MATCH (old)-[:SOLVED_BY]->(d:Decision)
RETURN old.title, old.solution, d.title AS decision, c.name AS shared_concept
ORDER BY old.updated_at DESC
```

**Returns**:
- `old.title` — The resolved problem
- `old.solution` — How it was fixed
- `d.title` — The decision that fixed it
- `c.name` — The shared concept
- Results are ordered by most recently updated first

---

## Pattern 5: What Course Material Covers This Concept?

**Question**: Where can I learn about a concept I encountered in a problem?

**Use case**: A Problem you're working on is about INSTANCE_OF `injection-vulnerability`. Find courses and materials that teach this concept.

```cypher
MATCH (p:Problem {title: $title})-[:INSTANCE_OF]->(c:Concept)<-[:COVERS]-(course:Course)
RETURN course.name, course.cohort, c.name AS concept
```

**Parameters**:
- `$title` — Problem title

**Returns**:
- `course.name` — Course name
- `course.cohort` — Cohort (e.g., "2026-Q2")
- `c.name` — The concept covered

---

## Pattern 6: Show Me the Full Context of This Project

**Question**: What is everything connected to a project?

**Use case**: You want to understand a project's complete context: technologies used, problems, decisions, people, etc.

```cypher
MATCH (p:Project {name: $name})-[r]->(n)
RETURN type(r) AS relationship, labels(n)[0] AS node_type, 
       coalesce(n.name, n.title) AS node_name
ORDER BY node_type, node_name
```

**Parameters**:
- `$name` — Project name

**Returns**:
- `relationship` — Relationship type (USES, HAS, DEPENDS_ON, etc.)
- `node_type` — Label of connected node
- `node_name` — Name or title of connected node
- Results ordered by node type and name for readability

---

## Query Tips

### Using Parameters

Always use `$param` syntax to prevent Cypher injection:

```cypher
MATCH (n {name: $name})
```

NOT:

```cypher
MATCH (n {name: "hardcoded-name"})
```

### Handling Name vs Title

Different node types use different key fields:
- Most nodes use `name` (Technology, Concept, Project, Domain, etc.)
- Some use `title` (Problem, Decision, Experiment, Photo, etc.)

Use `coalesce()` to handle both:

```cypher
coalesce(n.name, n.title) AS entity_name
```

### Ordering Results

Use `ORDER BY` to make results human-readable:

```cypher
ORDER BY type, name  -- Group by type, then alphabetical
ORDER BY updated_at DESC  -- Most recent first
```

### Optional Relationships

Use `OPTIONAL MATCH` when a relationship may not exist:

```cypher
OPTIONAL MATCH (p)-[:SOLVED_BY]->(d:Decision)
RETURN p.title, d.title  -- d.title will be null if no decision
```
