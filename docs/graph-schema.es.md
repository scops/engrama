# Esquema del grafo

> Referencia canónica del esquema del grafo de Engrama. El mismo esquema
> se aplica a ambos backends: en Neo4j se impone mediante las
> restricciones Cypher de `scripts/init-schema.cypher`; en SQLite se
> impone mediante las tablas `nodes` / `edges` / `nodes_fts` definidas en
> `engrama/backends/sqlite/schema.sql` (aplicadas automáticamente). Los
> fragmentos Cypher que aparecen a continuación también se aplican a
> Neo4j; las consultas equivalentes en SQLite están encapsuladas por los
> métodos del protocolo `GraphStore` — los consumidores no necesitan
> escribir en ninguno de los dos dialectos a mano.

## Nodos — perfil `developer`

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

## Campos temporales (todos los nodos)

Cada nodo lleva metadatos temporales gestionados por el motor (DDR-003 Fase D):

```
{
  created_at:  datetime,   // auto-asignado en el primer MERGE
  updated_at:  datetime,   // auto-actualizado en cada MERGE
  valid_from:  datetime,   // cuándo el hecho pasó a ser verdadero (auto-asignado al crear)
  valid_to:    datetime,   // cuándo fue supersedido (null = sigue vigente)
  confidence:  float,      // 0.0–1.0, decae con el tiempo (por defecto 1.0)
  decayed_at:  datetime,   // última vez que se aplicó decaimiento a la confianza
  embedding:   [float],    // vector de 768 dimensiones (cuando EMBEDDING_PROVIDER != none)
}
```

Los nodos con embeddings también llevan la etiqueta secundaria `:Embedded` para la indexación vectorial.

## Campos de identidad (todos los nodos y relaciones)

Desde **0.13.0** (Spec 001) cada nodo **y cada relación** lleva la identidad
de propiedad que lo acota:

```
{
  org_id:  string,   // organización propietaria — requerido
  user_id: string,   // usuario propietario — requerido
}
```

- Sellados por el motor en cada escritura; nunca tomados de propiedades de
  nodo suministradas por el llamante.
- Las lecturas son **fail-closed**: el filtro de scope restringe cada query a
  la identidad `(org_id, user_id)` del llamante. Un nodo o arista de otro
  tenant es invisible — un scope ausente o parcial no matchea *nada*, nunca
  todo.
- Una instalación de un solo proceso corre como una **identidad standalone**
  estable, así que estos campos están presentes y son uniformes sin ninguna
  configuración.
- Índices compuestos sobre `(org_id, user_id, <clave>)` respaldan las
  búsquedas acotadas en ambos backends.

Consultad [security.es.md](security.es.md#aislamiento-por-tenant-multi-tenant)
para el modelo de aislamiento completo y la ruta de migración de grafos
pre-0.13.

## Relaciones

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

## Consultas comunes

### Contexto completo de un proyecto (1 salto)
```cypher
MATCH (p:Project {name: $name})-[r]-(n)
RETURN p, r, n
```

### Búsqueda semántica
```cypher
CALL db.index.fulltext.queryNodes("memory_search", $query)
YIELD node, score
RETURN labels(node)[0] AS type, node.name AS name, score
ORDER BY score DESC LIMIT 10
```

### Proyectos activos con su stack tecnológico
```cypher
MATCH (p:Project {status: "active"})-[:USES]->(t:Technology)
RETURN p.name AS project, collect(t.name) AS stack
```

### Cadena Problema → solución → decisión
```cypher
MATCH (pr:Problem)-[:SOLVED_BY]->(d:Decision)<-[:INFORMED_BY]-(p:Project)
RETURN pr.title, pr.solution, d.title, d.rationale, p.name
```

### Exploración a dos saltos
```cypher
MATCH path = (start {name: $name})-[*1..2]-(end)
RETURN path LIMIT 50
```

## Notas de diseño

- **`MERGE` siempre** — el motor nunca usa `CREATE` directamente
- **Timestamps automáticos** — el motor gestiona `created_at` / `updated_at`
- **Las relaciones llevan identidad** — cada arista se sella con `(org_id, user_id)` para que una lectura acotada por relación filtre sin re-recorrer ambos extremos (Spec 001). Más allá de la identidad, las propiedades de relación se añaden solo cuando surge una necesidad demostrada.
- **Acotado fail-closed** — cada lectura se restringe a la identidad `(org_id, user_id)` del llamante; un scope ausente o parcial no matchea nada. Ver [security.es.md](security.es.md#aislamiento-por-tenant-multi-tenant).
- **Los embeddings son opcionales** — la búsqueda semántica a través de cualquier servicio compatible con la API de OpenAI (Ollama, OpenAI, LM Studio, vLLM, llama.cpp, Jina) mejora la búsqueda cuando está habilitada (DDR-003 Fase B+C, DDR-004). En Neo4j el índice vectorial sobre `(:Embedded)` cubre todos los tipos de nodo; en SQLite los vectores residen en la tabla virtual `vec0` `node_embeddings`.
- **Siempre parametrizar consultas** — nunca formatear cadenas en Cypher (Neo4j) ni en SQL (SQLite). Ambos backends usan vinculación de parámetros.
- **Campos temporales auto-gestionados** — `valid_from`, `confidence` se asignan al crear; `valid_to` se limpia al revivir (MATCH). El decaimiento se aplica mediante `engrama decay` en la CLI.
- **El esquema es agnóstico al backend** — las mismas etiquetas y relaciones definidas en `profiles/*.yaml` se aplican a cualquiera de los dos backends. Consulta [backends.es.md](backends.es.md) para la guía de decisión entre SQLite y Neo4j.
