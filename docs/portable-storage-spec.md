# Spec: Engrama portable storage (Neo4j-optional)

## Context

El refactor hexagonal (rama `refactor/cleanup`, mergeada 2026-05-09) extrajo todo el Cypher inline de skills/adapters/cli detrás de `GraphStore` / `VectorStore` / `EmbeddingProvider` (Protocol runtime-checkable, `engrama/core/protocols.py`). El protocolo está limpio, pero **swappear backend hoy no funciona**: el wiring (`cli.py`, `adapters/sdk`, `adapters/mcp/server.py`, `backends/__init__.py:create_async_store`) importa clases `Neo4j*` directamente, `EngramaEngine` devuelve `list[neo4j.Record]`, `core/client.py` envuelve el driver Neo4j, los tests requieren un Neo4j vivo, y `pyproject.toml` declara `neo4j>=5.26.0` como dependencia dura.

El objetivo del usuario es portabilidad real: que cualquiera pueda `pip install engrama` y tener una memoria funcional sin Docker, sin JVM, sin compilar binarios — base para forks verticales (ciberseguridad, etc.) y para ofrecer Engrama como memory-as-a-service. Neo4j queda como backend opcional para producción heavy.

Esta spec define **storage (graph + vector) + factory + MCP wiring + embedder por defecto**. Obsidian ya es opcional y queda fuera. Packaging avanzado (entry points para terceros) queda para una segunda spec.

---

## Goals

1. **Default zero-dep**: `pip install engrama` arranca con SQLite (stdlib) como graph store y `sqlite-vec` (1 wheel pip ~5MB) como vector store. Sin Neo4j, sin Docker, sin Ollama.
2. **Neo4j como extra**: `pip install engrama[neo4j]` reactiva el backend actual sin cambios funcionales.
3. **Backend-agnostic engine**: `EngramaEngine` y skills devuelven `list[dict]`, no `neo4j.Record`. Ningún caller fuera de `engrama/backends/neo4j/` toca tipos de Neo4j.
4. **Factory único**: cli, sdk, mcp, async-server, todos pasan por `create_stores()` / `create_async_stores()`. Cero `from engrama.backends.neo4j...` fuera del propio backend.
5. **Embedder LEANN-style**: `OpenAICompatibleProvider` con `base_url` configurable cubre Ollama (`/v1`), LM Studio, vLLM, llama.cpp, OpenAI, Jina. `NullProvider` por defecto cuando no hay red configurada.
6. **Test contract**: una suite parametrizada (`@pytest.mark.parametrize("backend", ["sqlite", "neo4j"])`) que valida que cualquier backend cumple el contrato del protocolo. Tests de SQLite corren sin servidor; tests de Neo4j se skipean si no hay `NEO4J_PASSWORD`.

## Non-goals

- Obsidian decoupling (ya es opcional).
- Migración automática de datos Neo4j → SQLite (manual para el usuario).
- ArcadeDB, LEANN, FAISS, Chroma como backends (adapters opcionales en spec posterior).
- Packaging plugin-based con entry points (spec posterior).
- Multi-scope / multi-tenancy real (`MemoryScope` sigue placeholder).
- Cambios de API pública en MCP tools.

---

## Architecture

```
                       ┌─────────────────────────┐
                       │   skills / mcp / cli    │
                       │   (consumen dicts)       │
                       └────────────┬────────────┘
                                    │ Protocol (no Neo4j types)
                       ┌────────────▼────────────┐
                       │   EngramaEngine          │  ← devuelve list[dict]
                       └────────────┬────────────┘
                                    │
                       ┌────────────▼────────────┐
                       │  create_stores(env)     │  ← único punto de wiring
                       └─┬────────┬───────────┬──┘
            GRAPH_BACKEND│        │           │
            ┌────────────▼─┐ ┌────▼─────┐ ┌───▼──────┐
            │ SQLiteGraph  │ │ Neo4j    │ │ Null     │
            │ (default)    │ │ (extra)  │ │ (test)   │
            └──────────────┘ └──────────┘ └──────────┘
            VECTOR_BACKEND
            ┌─────────────┐ ┌──────────┐ ┌──────────┐
            │ SqliteVec   │ │ Neo4jVec │ │ Null     │
            │ (default)   │ │ (extra)  │ │ (test)   │
            └─────────────┘ └──────────┘ └──────────┘
            EMBEDDING_PROVIDER
            ┌──────────────────────┐ ┌──────────┐
            │ OpenAICompatible     │ │ Null     │
            │ (Ollama/OpenAI/...)  │ │ (default)│
            └──────────────────────┘ └──────────┘
```

---

## Layer 1: GraphStore (SQLite)

### Files to create

- `engrama/backends/sqlite/__init__.py` — exports `SqliteGraphStore`, `SqliteAsyncStore`.
- `engrama/backends/sqlite/schema.sql` — DDL de las tablas (versionada por `PRAGMA user_version`).
- `engrama/backends/sqlite/store.py` — `SqliteGraphStore` (sync, usa `sqlite3` stdlib).
- `engrama/backends/sqlite/async_store.py` — `SqliteAsyncStore` (async, usa `aiosqlite`, dep nueva).
- `engrama/backends/sqlite/queries.py` — SQL central (ANALOGO a Cypher en neo4j/backend.py).

### Schema (file: `schema.sql`)

```sql
PRAGMA user_version = 1;
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- Núcleo
CREATE TABLE IF NOT EXISTS nodes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    label       TEXT NOT NULL,
    key_field   TEXT NOT NULL,        -- 'name' | 'title'
    key_value   TEXT NOT NULL,
    props       TEXT NOT NULL,        -- JSON blob
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    status      TEXT DEFAULT 'active',
    archived_at TEXT,
    UNIQUE(label, key_value)
);
CREATE INDEX idx_nodes_label    ON nodes(label);
CREATE INDEX idx_nodes_status   ON nodes(status);
CREATE INDEX idx_nodes_updated  ON nodes(updated_at);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    from_id     INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    rel_type    TEXT NOT NULL,
    to_id       INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    props       TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL,
    UNIQUE(from_id, rel_type, to_id)
);
CREATE INDEX idx_edges_from ON edges(from_id, rel_type);
CREATE INDEX idx_edges_to   ON edges(to_id, rel_type);

-- Fulltext (FTS5 viene en sqlite3 estándar de CPython)
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, title, description, notes, rationale, solution, context, body,
    content='', tokenize='unicode61'
);

-- Insights (subset que reflect/proactive consume)
CREATE TABLE IF NOT EXISTS insights (
    node_id     INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    confidence  REAL NOT NULL DEFAULT 0.0,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|approved|dismissed|synced
    source_query TEXT
);
CREATE INDEX idx_insights_status ON insights(status, confidence DESC);
```

### Method contract

`SqliteGraphStore` y `SqliteAsyncStore` deben implementar **el mismo set de 36 métodos** que `Neo4jAsyncStore` (`engrama/backends/neo4j/async_store.py`):

| Categoría | Métodos | Implementación |
|---|---|---|
| Nodos | `merge_node`, `get_node`, `delete_node`, `archive_node_by_name`, `archive_node_for_missing_note`, `list_existing_nodes` | `INSERT ... ON CONFLICT DO UPDATE` con `json_patch()` para props |
| Relaciones | `merge_relation`, `get_neighbours`, `get_node_with_neighbours`, `lookup_node_label` | `INSERT OR IGNORE` + JOINs |
| Search | `fulltext_search` | `nodes_fts MATCH` (BM25) |
| Esquema | `init_schema`, `health_check`, `close` | DDL del fichero, `PRAGMA quick_check` |
| Insights | `get_pending_insights`, `get_insight_by_title`, `update_insight_status`, `mark_insight_synced`, `find_insight_by_source_query`, `get_dismissed_titles`, `count_labels` | Consultas sobre tabla `insights` |
| Reflect (7 patterns) | `detect_cross_project_solutions`, `detect_shared_technology`, `detect_training_opportunities`, `detect_technique_transfer`, `detect_concept_clusters`, `detect_stale_knowledge`, `detect_under_connected_nodes` | SQL con CTEs recursivas para path-finding (3 hops máx) |
| Temporal | `decay_confidence`, `query_at_date` | `UPDATE ... WHERE updated_at < ?` |
| Vector hooks | `store_embedding`, `search_similar`, `delete_embedding`, `count_embeddings` | Delegan al `SqliteVecStore` adyacente (mismo fichero, otra tabla) |
| Cypher escape hatch | `run_pattern`, `run_cypher` | `raise NotImplementedError("SQLite backend doesn't support Cypher; use named methods")` |

### Mapeo de claves Neo4j → SQLite

- `MERGE (n:Label {name: $name})` → `INSERT INTO nodes(...) ON CONFLICT(label,key_value) DO UPDATE SET props=json_patch(props, excluded.props), updated_at=...`
- `MATCH (a)-[:REL]->(b)` (1 hop) → `JOIN edges ON edges.from_id=a.id AND edges.rel_type='REL' JOIN nodes b ON edges.to_id=b.id`
- `MATCH path = (a)-[*1..3]->(b)` → `WITH RECURSIVE` CTE limitando a 3 hops (límite duro para evitar runaways)
- Reflect patterns: cada `detect_*` se traduce a una vista SQL con scoring explícito (no se apoya en `apoc.path.*`). Documentar el trade-off: SQLite no hace shortest-path tan rápido, así que limitamos hops y dataset.

### Async path

- `aiosqlite` es la única dep nueva que añadir a las deps **base** del paquete (~10KB, pure-Python wrapper sobre `sqlite3` con thread-pool). Justificable porque MCP server es async-only.
- Alternativa rechazada: ejecutar el sync store en `asyncio.to_thread()` desde el wrapper async — añade 36 métodos de boilerplate al `SqliteAsyncStore`, peor de mantener.

---

## Layer 2: VectorStore (sqlite-vec)

### Files to create

- `engrama/backends/sqlite/vector.py` — `SqliteVecStore` (sync) y `SqliteVecAsyncStore` (async).

### Schema (extiende el mismo fichero SQLite)

```sql
-- sqlite-vec exposes vec0 virtual table
CREATE VIRTUAL TABLE IF NOT EXISTS node_embeddings USING vec0(
    node_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]   -- dims viene de EMBEDDING_DIMENSIONS
);
```

### Bootstrap de la extensión

```python
import sqlite3
import sqlite_vec

conn = sqlite3.connect(path)
conn.enable_load_extension(True)
sqlite_vec.load(conn)         # dep: sqlite-vec en pyproject extras 'sqlite' (default)
conn.enable_load_extension(False)
```

Crítico: el wheel `sqlite-vec` (~5 MB) viene precompilado para win/linux/mac. Para entornos donde la extensión no carga (Python distros raras), `SqliteVecStore.__init__` cae a `NullVectorStore` con un warning explícito en lugar de crashear.

### Contrato

Mismas firmas que `Neo4jVectorStore`: `store_vectors`, `search_vectors`, `delete_vectors`, `count`, `dimensions` (atributo).

`search_vectors` usa `vec_distance_cosine()` con `LIMIT k`. El `node_id` que devuelve sqlite-vec es el ID de `nodes`, así que un JOIN inmediato resuelve label/key sin tabla auxiliar.

---

## Layer 3: EmbeddingProvider (OpenAI-compatible)

### Files to create / modify

- `engrama/embeddings/openai_compat.py` (nuevo) — `OpenAICompatibleProvider`.
- `engrama/embeddings/ollama.py` — **deprecar** (mantener como wrapper que apunta a `OpenAICompatibleProvider(base_url="http://localhost:11434/v1")`, log warning).
- `engrama/embeddings/__init__.py` — `create_provider` despacha por `EMBEDDING_PROVIDER`:
  - `"none"` → `NullProvider` (default)
  - `"openai"` → `OpenAICompatibleProvider(base_url=$OPENAI_BASE_URL or https://api.openai.com/v1, api_key=$OPENAI_API_KEY, model=$EMBEDDING_MODEL)`
  - `"ollama"` → wrapper retrocompatible que devuelve OpenAICompatible apuntando al `/v1` de Ollama.

### `OpenAICompatibleProvider` API

```python
class OpenAICompatibleProvider:
    dimensions: int             # auto-detectado en primer embed o pasado en config
    model: str
    base_url: str
    api_key: str | None         # opcional para endpoints locales

    def embed(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...
    async def aembed(self, text: str) -> list[float]: ...
    async def aembed_batch(self, texts: list[str]) -> list[list[float]]: ...
    def health_check(self) -> bool: ...
    async def ahealth_check(self) -> bool: ...
```

Implementación: `httpx` (ya dep opcional en `[embeddings]`) hace POST a `{base_url}/embeddings` con `{model, input}`. Sin api_key si no está configurado (los locales no lo requieren).

`EMBEDDING_DIMENSIONS` se lee en este orden: env explícito → primer embed → 0 (NullProvider implícito).

---

## Layer 4: Factory & wiring

### `engrama/backends/__init__.py` (refactor)

```python
def create_stores(config=None) -> tuple[GraphStore, VectorStore]:
    backend = _resolve("GRAPH_BACKEND", "sqlite")     # cambia default
    vector  = _resolve("VECTOR_BACKEND", "sqlite-vec")  # cambia default

    if backend == "sqlite":
        from engrama.backends.sqlite import SqliteGraphStore
        path = _resolve("ENGRAMA_DB_PATH", "~/.engrama/engrama.db")
        graph = SqliteGraphStore(path)
    elif backend == "neo4j":
        from engrama.backends.neo4j import Neo4jGraphStore
        from engrama.core.client import EngramaClient
        graph = Neo4jGraphStore(EngramaClient(...))
    elif backend == "null":
        from engrama.backends.null import NullGraphStore
        graph = NullGraphStore()
    else: raise ValueError(...)

    if vector == "sqlite-vec":
        from engrama.backends.sqlite import SqliteVecStore
        v = SqliteVecStore(graph._conn, dimensions=...)   # comparte conexión
    elif vector == "neo4j":
        ...
    elif vector in ("none", "null"):
        v = NullVectorStore()
    return graph, v


def create_async_stores(config=None) -> tuple[GraphStore, VectorStore]:
    """Async variant — refactor de create_async_store actual."""
    backend = _resolve("GRAPH_BACKEND", "sqlite")
    if backend == "sqlite":
        from engrama.backends.sqlite import SqliteAsyncStore, SqliteVecAsyncStore
        ...
    elif backend == "neo4j":
        # actual create_async_store inline
        ...
```

### Callers a migrar

| File | Cambio |
|---|---|
| `engrama/cli.py:109,266-267,291-292` | Reemplazar `Neo4jGraphStore(client)` por `create_stores()`. |
| `engrama/adapters/sdk/__init__.py:89,96` | Reemplazar `Neo4jVectorStore(...)` por `create_stores()`. |
| `engrama/adapters/mcp/server.py:40,45,289,308-309` | Reemplazar `from neo4j import AsyncGraphDatabase, AsyncDriver` y `Neo4jAsyncStore` por `create_async_stores()`. CLI flags `--db-url`/`--username`/`--password` quedan como overrides opcionales (sólo se usan si `GRAPH_BACKEND=neo4j`). |
| `engrama/adapters/mcp/__init__.py:38-73` | Hacer flags Neo4j-específicos opcionales (no required cuando backend=sqlite). |
| `engrama/core/engine.py:78,136,147,155,209` | Cambiar firmas de `-> list[Record]` a `-> list[dict[str, Any]]`. Eliminar `from neo4j import Record`. Métodos internos consumen dicts del store directamente (Neo4jGraphStore ya devuelve dicts; sólo hay que tirar la conversión). |

### `EngramaClient` legacy

Mantener pero deprecar: `core/client.py` queda como wrapper Neo4j-específico, sólo importado desde `backends/neo4j/__init__.py` y desde `EngramaEngine.__init__` para back-compat (rama `if isinstance(client_or_store, EngramaClient)`). No expuesto en docs nuevas. En 6 meses se elimina.

---

## pyproject.toml

```toml
dependencies = [
    "aiosqlite>=0.19",         # nuevo: async SQLite
    "sqlite-vec>=0.1",         # nuevo: vector store default
    "pydantic>=2.0",
    "python-dotenv>=1.0",
    "pyyaml>=6.0",
    "httpx>=0.27",             # promovida de [embeddings] a base (necesaria para OpenAI-compat default)
]

[project.optional-dependencies]
neo4j      = ["neo4j>=5.26.0"]                                 # nuevo extra
embeddings = []                                                # vacía o eliminada (httpx ahora es base)
mcp        = ["mcp>=1.8", "fastmcp>=2.10.5,<3"]
langchain  = ["langchain>=0.2", "langchain-community>=0.2"]
rest       = ["fastapi>=0.110", "uvicorn>=0.29"]
dev        = ["pytest>=8.0", "pytest-asyncio>=0.23", "ruff>=0.4"]
all        = ["engrama[neo4j,mcp,langchain,rest,dev]"]
```

Resultado: `pip install engrama` baja `aiosqlite` + `sqlite-vec` + `httpx` + `pydantic` + `python-dotenv` + `pyyaml` (~10 MB total). Cero binarios externos requeridos. Neo4j entra sólo con `pip install engrama[neo4j]`.

---

## Test contract

### `tests/conftest.py` (refactor)

Elimina el `raise RuntimeError` si falta `NEO4J_PASSWORD`. Mueve fixtures Neo4j a `tests/backends/test_neo4j.py` con `pytest.skip(...)` si no hay credenciales.

Añade fixture parametrizado:

```python
@pytest.fixture(params=["sqlite", "neo4j"])
def graph_store(request, tmp_path):
    if request.param == "sqlite":
        from engrama.backends.sqlite import SqliteGraphStore
        return SqliteGraphStore(tmp_path / "test.db")
    elif request.param == "neo4j":
        if not os.getenv("NEO4J_PASSWORD"):
            pytest.skip("NEO4J_PASSWORD not set")
        # init Neo4j store
        ...
```

### `tests/contracts/test_graphstore_contract.py` (nuevo)

Tests parametrizados sobre `graph_store` que validan **comportamiento** (no sólo `hasattr`):
- `merge_node` es idempotente: dos llamadas con misma key devuelven mismo id, props se mergean.
- `merge_node` actualiza `updated_at` pero conserva `created_at`.
- `delete_node(soft=True)` setea `archived_at`, deja el nodo visible para `get_node`.
- `merge_relation` es idempotente.
- `fulltext_search` matchea sobre los 8 campos del schema.
- `get_neighbours(hops=2)` devuelve vecinos a 1 y 2 hops, no más.
- Cada uno de los 7 `detect_*` devuelve resultados con shape `{node, score, ...}` consistente.

Estos contratos garantizan que cualquier nuevo backend (ArcadeDB, LEANN-vector, etc.) puede subirse a Engrama con confianza.

---

## Phased delivery

Para evitar un único PR gigantesco, dividir en 4 hitos secuenciales, cada uno verde en CI antes del siguiente:

### Phase 1 — Engine devuelve dicts (no breaking)
Refactorizar `EngramaEngine.merge_node/run/search/get_context/merge_relation` para devolver `list[dict]` en lugar de `list[Record]`. `Neo4jGraphStore` ya devuelve dicts internamente; sólo hay que eliminar la conversión en `engine.py` y actualizar callers que hagan `record["x"]` → `dict["x"]` (mismo acceso). Tests verdes con Neo4j.

### Phase 2 — SQLite backend (graph + vector)
Crear `engrama/backends/sqlite/{store,async_store,vector,schema.sql,queries}.py`. Implementar los 36 métodos. Añadir `aiosqlite` y `sqlite-vec` a deps. Crear `tests/contracts/test_graphstore_contract.py` parametrizado y validar SQLite + Neo4j contra el mismo contrato.

### Phase 3 — Wiring por factory
Migrar `cli.py`, `adapters/sdk`, `adapters/mcp/server.py`, `adapters/mcp/__init__.py` a `create_stores()` / `create_async_stores()`. Cambiar default de `GRAPH_BACKEND` a `sqlite`. Actualizar `pyproject.toml` para mover Neo4j a extra. Validar manualmente: `engrama init` + `engrama-mcp` arrancan sin Neo4j.

### Phase 4 — OpenAI-compatible embedder
Crear `engrama/embeddings/openai_compat.py`. Convertir `OllamaProvider` en wrapper retrocompatible. Actualizar `create_provider`. Documentar en README la matriz de proveedores compatibles (Ollama, LM Studio, OpenAI, Jina, etc.).

Cada fase se mergea aparte. Si una fase descubre un blocker se replanifica sin tirar el trabajo previo.

---

## Critical files to modify

| Path | Cambio |
|---|---|
| `engrama/core/engine.py:22,78,136,147,155,209` | Tirar `from neo4j import Record`; cambiar firmas a `list[dict]`. |
| `engrama/backends/__init__.py` | Default `GRAPH_BACKEND=sqlite`; add `create_async_stores`. |
| `engrama/cli.py:109,266-267,291-292,447-466` | Pasar por factory; flags Neo4j sólo si `--backend neo4j`. |
| `engrama/adapters/sdk/__init__.py:64-100` | Pasar por factory. |
| `engrama/adapters/mcp/server.py:40,45,289,308-309` | Pasar por `create_async_stores`; quitar imports Neo4j directos. |
| `engrama/adapters/mcp/__init__.py:38-73` | Flags Neo4j opcionales. |
| `engrama/embeddings/__init__.py` | Despacho `EMBEDDING_PROVIDER` con default `none`, suporte `openai`. |
| `engrama/embeddings/ollama.py` | Wrapper deprecado sobre OpenAI-compat. |
| `tests/conftest.py` | Skipear Neo4j si no hay password (no crashear). |
| `pyproject.toml` | `aiosqlite`, `sqlite-vec`, `httpx` a base; `neo4j` a extra. |

## New files

| Path | Contenido |
|---|---|
| `engrama/backends/sqlite/__init__.py` | Exports |
| `engrama/backends/sqlite/schema.sql` | DDL |
| `engrama/backends/sqlite/store.py` | `SqliteGraphStore` (sync) |
| `engrama/backends/sqlite/async_store.py` | `SqliteAsyncStore` (aiosqlite) |
| `engrama/backends/sqlite/vector.py` | `SqliteVecStore` + async |
| `engrama/backends/sqlite/queries.py` | SQL central |
| `engrama/embeddings/openai_compat.py` | `OpenAICompatibleProvider` |
| `tests/contracts/__init__.py` | |
| `tests/contracts/test_graphstore_contract.py` | Suite parametrizada |
| `tests/backends/test_sqlite.py` | Tests específicos del backend SQLite |

## Reused utilities (no reimplementar)

- `engrama/core/protocols.py` — protocolos ya existen, no tocar firmas.
- `engrama/core/schema.py` `TITLE_KEYED_LABELS` — sigue siendo source of truth de qué labels usan `title` vs `name`.
- `engrama/embeddings/text.py` `node_to_text()` — ya backend-agnostic.
- `engrama/backends/null.py` — usado para tests CI sin DB; sigue siendo el zero-side-effects fallback.

---

## Verification (end-to-end)

1. **Phase 1 verify**: `pytest tests/ --backend=neo4j` verde. Un grep `Record` en `engrama/` devuelve 0 hits fuera de `backends/neo4j/`.
2. **Phase 2 verify**: `pytest tests/contracts/ -v` corre todos los tests dos veces (una con SQLite, una con Neo4j si está disponible) y ambos pasan. `pytest tests/backends/test_sqlite.py` pasa sin variables de entorno.
3. **Phase 3 verify**: en máquina limpia (sin Docker, sin Neo4j corriendo):
   - `pip install -e .` (sin `[neo4j]`).
   - `engrama init` crea `~/.engrama/engrama.db`.
   - `engrama-mcp` arranca sin errores y responde a `engrama_remember` + `engrama_search` vía cliente MCP.
   - `pip install -e .[neo4j]` + `engrama init --backend neo4j` sigue funcionando.
4. **Phase 4 verify**: con Ollama corriendo localmente:
   - `EMBEDDING_PROVIDER=openai OPENAI_BASE_URL=http://localhost:11434/v1 EMBEDDING_MODEL=nomic-embed-text engrama-mcp` arranca.
   - `engrama_remember` con texto largo genera embeddings y `engrama_search` devuelve por similaridad. Sin Ollama, fallback a fulltext silencioso.
5. **Smoke test final**: en una VM Windows limpia con Python 3.11, `pip install engrama` (~10 MB) → `engrama-mcp` arranca y un cliente MCP responde correctamente. Tiempo total esperado: <60s desde cero.

---

## Open questions (no bloqueantes)

- ¿Pre-empaquetar el `.engrama/engrama.db` con seed data del schema profile, o generarlo en `engrama init`? — Propuesta: `engrama init` genera, igual que hoy.
- ¿`SqliteGraphStore` debe soportar múltiples profiles en un mismo fichero (tabla `profile` + filtro), o un fichero por profile? — Propuesta: un fichero por profile (más simple, alineado con cómo Neo4j usa databases separados).
- ¿El log de migración Neo4j → SQLite (un script `engrama migrate neo4j-to-sqlite`) entra aquí o en una spec aparte? — Propuesta: spec aparte, no es bloqueante para portabilidad.
