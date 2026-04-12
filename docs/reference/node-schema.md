# Node Schema and Relationship Types

**Purpose**: Complete reference for all node types, their properties, and all relationship types (both faceted and structural) in Engrama.

---

## Node Types with Documents (has_document: true)

These node types create Obsidian notes with full frontmatter. The vault note is the primary document; the graph is indexed for querying.

| Label | Key | Required properties | Optional properties |
|-------|-----|---------------------|---------------------|
| **Project** | name | status, description, repo, stack[] | created_at, updated_at |
| **Course** | name | cohort, date, level, client | status, description, created_at, updated_at |

---

## Node Types without Documents (graph-only)

These node types exist only in the graph and can be queried directly. They do not generate Obsidian notes.

| Label | Key | Required properties | Optional properties |
|-------|-----|---------------------|---------------------|
| **Technology** | name | version, type (framework/infra/language/protocol) | notes, created_at, updated_at |
| **Concept** | name | domain, notes | created_at, updated_at |
| **Decision** | title | rationale, date, status, alternatives_considered | created_at, updated_at |
| **Problem** | title | solution, status (open/resolved), context, severity | created_at, updated_at |
| **Material** | name | type (cheatsheet/slides/exercise/reference), format (md/pdf/jsx/pptx), notes | status, created_at, updated_at |
| **Client** | name | sector, contact | notes, created_at, updated_at |
| **Domain** | name | description | created_at, updated_at |
| **Insight** | title | body, confidence (0.0–1.0), status (pending/approved/dismissed) | source_query, created_at, updated_at |
| **Person** | name | role, organisation, contact | notes, created_at, updated_at |
| **Target** | name | ip, os, status, scope | notes, created_at, updated_at |
| **Vulnerability** | title | cve, severity, status | notes, created_at, updated_at |
| **Technique** | name | mitre_id, tactic | notes, created_at, updated_at |
| **Tool** | name | version, type | notes, created_at, updated_at |
| **CTF** | name | platform, difficulty, status, writeup_path | created_at, updated_at |
| **Exercise** | title | difficulty, duration, status | notes, created_at, updated_at |
| **Photo** | title | date, location, species, camera, lens, status | notes, created_at, updated_at |
| **Location** | name | region, coordinates, habitat | notes, created_at, updated_at |
| **Species** | name | family, conservation_status | notes, created_at, updated_at |
| **Gear** | name | type, brand | notes, created_at, updated_at |
| **Model** | name | type, provider, version | notes, created_at, updated_at |
| **Dataset** | name | source, size, format | notes, created_at, updated_at |
| **Experiment** | title | status, metric, result, date | notes, created_at, updated_at |
| **Pipeline** | name | status, steps | notes, created_at, updated_at |

---

## Relationship Types

### Faceted Relationships (Universal Dimensions)

These relationships encode the six facets of classification (see faceted-classification.md).

| Relationship | Source | Target | Facet | Purpose |
|--------------|--------|--------|-------|---------|
| `INSTANCE_OF` | * | Concept | **identity** | What is this? Mandatory for Problem, Decision, Vulnerability. Recommended for others. |
| `COMPOSED_OF` | * | Technology, Concept | **composition** | What is it made of? |
| `PERFORMS` | * | Concept | **action** | What process does it execute? |
| `SOLVED_BY` | Problem | Decision | **action** | How was this problem solved? |
| `SERVES` | * | Concept | **purpose** | What purpose does this serve? |
| `BELONGS_TO` | * | Project, Client | **context** | Where does this live? Who does this belong to? MANDATORY. |
| `IN_DOMAIN` | * | Domain | **domain** | Which field of knowledge? RECOMMENDED. |

**Note**: Faceted and structural relationships coexist. Faceted relationships capture universal classification dimensions applicable to any entity. They enable cross-domain discovery.

---

### Structural Relationships (Domain-Specific Semantics)

These relationships capture specific, domain-semantic connections.

| Relationship | Source | Target | Purpose |
|--------------|--------|--------|---------|
| `USES` | Project, Course, Material | Technology | A project/course uses this technology. |
| `INFORMED_BY` | Project | Decision | A project was guided by a decision. |
| `HAS` | Project | Problem | A project has encountered this problem. |
| `DEPENDS_ON` | Project, Technique, Vulnerability, Exercise, Pipeline | Project, Tool, Technique, Vulnerability, Model, Dataset | A project depends on another project, tool, technique, etc. |
| `SIMILAR_TO` | Problem, Vulnerability, Technique | Problem, Vulnerability, Technique | Similarity relationship for cross-entity discovery. |
| `CAUSED_BY` | Problem, Vulnerability | Problem, Vulnerability, Technique | Root cause relationship. |
| `REPLACES` | Decision, Tool | Decision, Tool | Precedence and evolution: new replaces old. |
| `TEACHES` | Course | Technology | A course teaches this technology. |
| `COVERS` | Course, Material | Concept | A course or material covers this concept. |
| `INCLUDES` | Course, Exercise | Exercise, Material | Structural containment. |
| `PREREQUISITE_OF` | Course, Exercise | Course, Exercise | Learning order. |
| `HAS_MATERIAL` | Course | Material | A course has associated teaching materials. |
| `RELATED_TO` | Concept, Technology | Concept, Technology | General semantic relationship. |
| `SUBSET_OF` | Concept | Concept | Hierarchy: this concept is a subset of another. |
| `CONTRADICTS` | Concept | Concept | Logical opposition. |
| `LINKS_TO` | * | * | Generic cross-reference. |
| `EXPLOITS` | Technique, Tool | Vulnerability, Target, Technique | An attack technique exploits a vulnerability. |
| `EXECUTED_WITH` | Technique | Tool | A technique is executed with this tool. |
| `TARGETS` | Vulnerability, Technique, CTF | Target | A vulnerability or technique targets this machine/network. |
| `DOCUMENTS` | Material | * | A material documents an entity. |
| `APPLIES` | Technique, Vulnerability | Target | A technique or vulnerability applies to this target. |
| `IMPLEMENTS` | Tool, Technology | Technique | A tool implements this technique. |
| `INVOLVES` | * | Person, Technology | Involvement relationship. |
| `FOR` | Tool, Exercise | Person, CTF, Project | A tool is for a person or project; an exercise is for a CTF. |
| `TAKEN_AT` | Person | Course | A person has taken/taught a course. |
| `PRACTICES` | Person | Technique, Technology, Domain | A person practices this technique, technology, or domain. |
| `REQUIRES` | Exercise, Experiment | Technology, Tool, Dataset, Model | An exercise or experiment requires this technology. |
| `ORIGIN_OF` | Location, Person | Photo, Species | Provenance relationship. |
| `FEATURES` | Photo | Species, Location, Gear | A photo features this species or location. |
| `SHOT_WITH` | Photo | Gear | A photo was shot with this equipment. |
| `INHABITS` | Species | Location | A species inhabits this location. |
| `TRAINS_ON` | Model | Dataset | A model is trained on this dataset. |
| `RUNS` | Pipeline | Experiment | A pipeline runs this experiment. |
| `EVALUATES` | Experiment | Model, Dataset | An experiment evaluates this model or dataset. |
| `FEEDS` | Pipeline | Model, Dataset | A pipeline feeds data into this model or dataset. |

---

## Complete Node Type List (from schema.py)

The following 25 node types are defined in the `base+hacking+teaching+photography+ai` profile:

1. **Project** — A project, product, or major initiative.
2. **Concept** — A concept, idea, or knowledge area. The bridge between domains.
3. **Decision** — A decision with rationale and alternatives considered.
4. **Problem** — A problem, challenge, or blocker encountered.
5. **Technology** — A language, framework, tool, or infrastructure component.
6. **Person** — A person — colleague, client, collaborator, or contact.
7. **Domain** — A field of knowledge — web-development, cybersecurity, cooking, photography, etc.
8. **Client** — An organisation that commissions work or training.
9. **Target** — A machine, network, or service being assessed.
10. **Vulnerability** — A vulnerability or misconfiguration found during assessment.
11. **Technique** — An attack technique — maps to MITRE ATT&CK where applicable.
12. **Tool** — A security tool — scanner, exploit framework, utility.
13. **CTF** — A CTF challenge or HackTheBox machine.
14. **Course** — A training course or workshop delivered.
15. **Exercise** — A hands-on lab, exercise, or practical challenge.
16. **Material** — A teaching artifact: cheatsheet, slides, exercise sheet, or reference card.
17. **Photo** — A photograph or photo session.
18. **Location** — A geographic location — birding spot, nature reserve, trail.
19. **Species** — A species of bird, mammal, insect, or plant.
20. **Gear** — Camera body, lens, tripod, or other photography equipment.
21. **Model** — An AI/ML model — LLM, classifier, embedding model, etc.
22. **Dataset** — A dataset used for training, evaluation, or analysis.
23. **Experiment** — An ML experiment or evaluation run.
24. **Pipeline** — A data or ML pipeline — preprocessing, training, inference.
25. **Insight** — A cross-entity pattern detected by the reflect skill.

---

## Complete Relationship Type List (from schema.py)

The following 39 relationship types are defined in the profile:

**Faceted (7):**
1. INSTANCE_OF
2. COMPOSED_OF
3. PERFORMS
4. SOLVED_BY
5. SERVES
6. BELONGS_TO
7. IN_DOMAIN

**Structural (32):**
8. USES
9. INFORMED_BY
10. HAS
11. APPLIES
12. IMPLEMENTS
13. INVOLVES
14. FOR
15. DEPENDS_ON
16. SIMILAR_TO
17. CAUSED_BY
18. REPLACES
19. RELATED_TO
20. SUBSET_OF
21. CONTRADICTS
22. LINKS_TO
23. EXPLOITS
24. EXECUTED_WITH
25. TARGETS
26. DOCUMENTS
27. COVERS
28. TEACHES
29. INCLUDES
30. ORIGIN_OF
31. PRACTICES
32. REQUIRES
33. PREREQUISITE_OF
34. HAS_MATERIAL
35. TAKEN_AT
36. FEATURES
37. SHOT_WITH
38. INHABITS
39. TRAINS_ON
40. RUNS
41. EVALUATES
42. FEEDS

---

## Key-to-Merge Rules

When creating or updating nodes, use the correct field as the unique key (merge key):

**Nodes using `name` as key:**
- Project, Technology, Concept, Person, Domain, Client, Target, Technique, Tool, CTF, Course, Exercise, Material, Photo, Location, Species, Gear, Model, Dataset, Pipeline

**Nodes using `title` as key:**
- Decision, Problem, Vulnerability, Experiment, Photo (title is also a valid key for Photo)

Always merge on the correct key to avoid creating duplicates:

```cypher
MERGE (n:Problem {title: $title})
ON CREATE SET n.solution = $solution, n.status = "open", n.created_at = datetime()
ON MATCH SET n.updated_at = datetime()
```
