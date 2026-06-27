# Hoja de ruta

## Fase 0 · Configuración y diseño ✅

- [x] Diseño completo de la arquitectura
- [x] Nombre elegido — `engrama` (disponible en PyPI)
- [x] Documentación inicial redactada
- [x] Estructura del proyecto creada en `C:\Proyectos\engrama`
- [x] Neo4j funcionando vía Docker Desktop
- [x] Integración MCP con Obsidian diseñada
- [x] Primeras memorias cargadas desde notas de Obsidian
- [x] Bug encontrado: `engrama_relate` falla para nodos Decision/Problem
- [ ] Crear repositorio `github.com/scops/engrama` → ver Fase 7 para la checklist de publicación

## Fase 1 · Núcleo (MVP) ✅

> Objetivo: Claude Desktop lee y escribe el grafo desde la propia conversación.

- [x] `engrama/core/client.py` — driver Neo4j, pool de conexiones, health check
- [x] `scripts/init-schema.cypher` — restricciones + índice fulltext
- [x] `engrama/core/engine.py` — pipeline de escritura (MERGE + timestamps), consulta básica
- [x] `engrama/core/schema.py` — dataclasses Python para nodos y relaciones
- [x] `profiles/developer.yaml` — perfil completo con descripciones de nodos
- [x] Tests de integración básicos contra Neo4j real

## Fase 2 · Adaptador MCP ✅

> Objetivo: usar el grafo desde Claude Desktop vía MCP sin escribir Cypher manualmente.

- [x] `engrama/adapters/mcp/server.py` — servidor MCP nativo vía FastMCP + driver asíncrono de Neo4j
- [x] Diez herramientas MCP: search, remember, relate, context, sync_note, sync_vault, reflect, surface_insights, approve_insight, write_insight_to_vault (el conjunto ha crecido desde entonces a catorce — `engrama_ingest` llegó en la Fase 9, `engrama_status` en #52, y `engrama_reindex` / `engrama_gdpr_forget` después; véase architecture.md para la lista actual)
- [x] `examples/claude_desktop/config.json` — configuración lista para copiar y pegar
- [x] `examples/claude_desktop/system-prompt.md` — system prompt de memoria
- [ ] Test extremo a extremo: Claude Desktop → MCP → Neo4j → respuesta (verificación manual hecha, test automatizado pendiente)

> **Decisión arquitectónica (2026-04-11):** descartado el fork `scops/mcp-neo4j`.
> El servidor habla directamente con Neo4j a través del driver asíncrono oficial `neo4j` —
> no se necesita una capa intermedia MCP-a-Cypher. Esto elimina una dependencia,
> suprime un nivel de indirección y da control total sobre la lógica MERGE,
> la gestión de parámetros y la distinción entre clave title y name.

## Fase 3 · Sincronización con Obsidian ✅

> Objetivo: el vault de Obsidian ↔ el grafo de Neo4j se mantienen sincronizados automáticamente.

- [x] `engrama/adapters/obsidian/adapter.py` — wrapper de E/S de archivos del vault
- [x] `engrama/adapters/obsidian/parser.py` — extracción de entidades desde notas
- [x] `engrama/adapters/obsidian/sync.py` — sincronización bidireccional vía engrama_id
- [x] `tests/test_obsidian_sync.py` — tests del adaptador + parser (11 tests)
- [x] Herramientas MCP `engrama_sync_note` + `engrama_sync_vault`
- [x] ~~flags `has_document`~~ — supersedido: la sincronización parsea el contenido de la nota directamente, no se necesita flag por tipo
- [x] ~~`full_scan()`~~ — supersedido por la herramienta MCP `engrama_sync_vault` (itera todas las notas)
- [x] ~~`archive_missing()`~~ — supersedido por `ForgetSkill` (Fase 4) que gestiona el archivado por nombre o TTL

## Fase 4 · Skills base ✅

> Objetivo: cuatro clases de skill componibles que los agentes pueden invocar directamente.

- [x] `skills/remember.py` — `RememberSkill.run(engine, label, name, observation, extra)`
  - Auto-detecta la clave de merge (name vs title) vía `TITLE_KEYED_LABELS`
  - Devuelve estado creado/actualizado
- [x] `skills/recall.py` — `RecallSkill.run(engine, query, limit, hops)`
  - Búsqueda fulltext → nodos semilla → expansión de grafo hasta N saltos
  - Deduplica vecinos, devuelve dataclasses `RecallResult` con propiedades + cadena de vecinos
- [x] `skills/associate.py` — `AssociateSkill.run(engine, from_name, from_label, rel_type, to_name, to_label)`
  - Valida etiquetas y tipos de relación contra los enums del esquema
  - Delega en `engine.merge_relation()`
- [x] `skills/forget.py` — `ForgetSkill.forget_by_name()` + `forget_by_ttl()`
  - Borrado suave (archivo) por defecto — establece `status: "archived"` + `archived_at`
  - `purge=True` para `DETACH DELETE` permanente
  - Modo TTL: archiva/purga nodos más antiguos de N días por `updated_at`
- [x] Tests de integración: 19 tests en `tests/test_phase4_skills.py`

## Fase 5 · reflect ✅

> Objetivo: detección de patrones cruzando entidades sin que nadie lo pida.

- [x] `skills/reflect.py` — `ReflectSkill` con tres consultas Cypher multi-salto:
  - Transferencia de soluciones entre proyectos (Problem ↔ Concept ↔ Decision)
  - Tecnología compartida entre Projects activos
  - Oportunidad de formación (Problem ↔ Concept ↔ Course)
- [x] Tipo de nodo `Insight` añadido al esquema + índice fulltext + restricción
- [x] reflect escribe nodos Insight con puntuación de confianza + status: "pending"
- [x] Herramienta MCP `engrama_reflect` — los agentes pueden lanzar reflect bajo demanda
- [x] Tests con datos de grafo precargados (4 tests en `test_skills.py`)

### Bugs corregidos durante la Fase 5

- [x] `engrama_relate` — ahora busca por `title` para nodos Decision/Problem
      (corregido tanto en `server.py` como en `engine.py`)
- [x] `test_obsidian_sync.py` — corregido error de sintaxis con operador walrus en la línea 62
- [x] Tests: 3 tests en `test_adapters.py` para la corrección de clave title en relate

## Fase 6 · proactive ✅

> Objetivo: mostrar Insights al agente + escribirlos de vuelta en Obsidian.

- [x] `skills/proactive.py` — `ProactiveSkill` con cuatro métodos:
  - `surface(engine, limit)` — lee Insights pendientes, más recientes primero
  - `approve(engine, title)` — establece status a "approved" + `approved_at`
  - `dismiss(engine, title)` — establece status a "dismissed" + `dismissed_at`
  - `write_to_vault(engine, obsidian, title, target_note)` — añade Insight aprobado como sección markdown
- [x] Tres nuevas herramientas MCP:
  - `engrama_surface_insights` — lee Insights pendientes para presentarlos al agente
  - `engrama_approve_insight` — el humano aprueba o descarta (action: "approve" | "dismiss")
  - `engrama_write_insight_to_vault` — añade Insight aprobado a una nota de Obsidian
- [x] Ciclo de vida del Insight aplicado: solo los Insights aprobados pueden escribirse en el vault
- [x] Los Insights sincronizados reciben `obsidian_path` + `synced_at` en Neo4j
- [x] Tests de integración: 12 tests en `tests/test_proactive.py`

## Fase 7 · SDK Python + PyPI ✅

> Objetivo: API pública limpia + CLI para uso sin MCP.

- [x] `engrama/adapters/sdk/__init__.py` — clase `Engrama` que envuelve todos los skills:
  - `remember()`, `recall()`, `search()`, `associate()`
  - `forget()`, `forget_by_ttl()`
  - `reflect()`, `surface_insights()`, `approve_insight()`, `dismiss_insight()`
  - `write_insight_to_vault()` (requiere Obsidian)
  - Context manager, `verify()`, `has_vault`, `repr()`
- [x] `engrama/__init__.py` — re-exportación de primer nivel: `from engrama import Engrama`
- [x] `engrama/cli.py` — cuatro comandos CLI:
  - `engrama init --profile developer [--dry-run] [--no-apply]` — generación de código + aplicación del esquema
  - `engrama verify` — comprobación de conectividad con Neo4j
  - `engrama reflect` — ejecutar detección de patrones, imprimir resultados
  - `engrama search <query>` — búsqueda de texto completo
- [x] Tests de integración: `test_sdk.py` (14 tests) + `test_cli.py` (6 tests)
- [ ] Test manual extremo a extremo: `engrama init --profile developer`, verificar herramientas MCP en Claude Desktop
- [ ] Push a `github.com/scops/engrama` (fases 1–7 acumuladas, aún sin subir)
- [ ] Publicar `engrama` en PyPI (v0.1.0) — después del push al repositorio

## Fase 8 · Perfiles componibles ✅

> Objetivo: dar soporte a usuarios multidisciplinares con esquemas de grafo modulares y componibles.

- [x] `profiles/base.yaml` — base universal con Project, Concept, Decision, Problem, Technology, Person
- [x] `profiles/modules/hacking.yaml` — Target, Vulnerability, Technique, Tool, CTF
- [x] `profiles/modules/teaching.yaml` — Course, Client, Exercise, Material
- [x] `profiles/modules/photography.yaml` — Photo, Location, Species, Gear
- [x] `profiles/modules/ai.yaml` — Model, Dataset, Experiment, Pipeline
- [x] `scripts/generate_from_profile.py` — función `merge_profiles()` + flag `--modules`
  - Fusiona nodos por etiqueta (unión de propiedades, gana la descripción más larga)
  - Deduplica relaciones por (type, from, to)
  - Valida que todos los extremos de las relaciones existan en el conjunto de nodos fusionado
- [x] CLI: `uv run engrama init --profile base --modules hacking teaching photography ai`
- [x] Compatible con versiones anteriores: `--profile developer` sigue funcionando por separado
- [x] Skill onboard actualizado: documenta el enfoque componible + plantilla YAML de módulo
- [x] `example-profiles.md` actualizado con sección componible
- [x] Tests de integración: `tests/test_composable.py` — lógica de merge (9), codegen (3), archivos reales (5), CLI (4)

## Fase 9 · Funcionalidades principales ✅

> Objetivo: hacer que Engrama descubra lo que el usuario no sabía que sabía.

### 9a — Ingesta ✅
- [x] Herramienta MCP `engrama_ingest` — lee nota del vault, texto plano o transcripción de conversación
- [x] Devuelve contenido + guía de extracción con pistas de deduplicación contra nodos existentes
- [x] Guiado por el agente (Opción B): la herramienta lee, el agente extrae y llama a `engrama_remember`

### 9b — Reflect adaptativo ✅
- [x] Reflect inspecciona el perfil del grafo antes de generar consultas
- [x] Cuatro nuevos patrones de detección: transferencia de técnicas, agrupamiento de conceptos, conocimiento obsoleto, nodos poco conectados
- [x] Los Insights descartados nunca vuelven a mostrarse
- [x] Puntuación de confianza: basada en caminos, escalada por fuerza de conexión y número de entidades
- [x] Skill reflect y herramienta MCP actualizados

### 9c — Proactividad ✅
- [x] El estado de sesión rastrea las llamadas a `engrama_remember`
- [x] Sugerencia proactiva tras 10+ entidades almacenadas desde el último reflect
- [x] `engrama_search` muestra Insights pendientes relacionados con la consulta
- [x] Reflect reinicia el contador de proactividad

### 9d — Corrección de errores ✅
- [x] Contador de proactividad movido del contexto lifespan al estado a nivel de módulo `_proactive_state` (persistencia entre llamadas)
- [x] `_run_pattern` soporta `any_labels` para activación con lógica OR (Problem OR Vulnerability + Course, Project OR Course)
- [x] `training_opportunity` ampliado: coincide con Vulnerability OR Problem
- [x] `shared_technology` ampliado: cualquier entidad vía USES/TEACHES/COMPOSED_OF, la activación solo necesita Technology
- [x] `stale_knowledge` ampliado: se activa con Project OR Course

### System prompt v0.5 + documentación de referencia ✅
- [x] System prompt reducido a ~100 líneas (eficiente en tokens)
- [x] Contenido detallado extraído a `docs/reference/` (faceted-classification, query-patterns, node-schema, sync-contract)
- [x] Enrutamiento dual-vault (obsidian-mcp vs engrama) añadido al prompt

## Fase 10 · Adaptadores adicionales

- [ ] `adapters/langchain/` — Memory + Tool para LangChain
- [ ] `adapters/rest/` — endpoints HTTP con FastAPI

## Fase 11 · Vectores (v2) ✅

> DDR-003 Fases A–D completadas.

- [x] Arquitectura basada en protocolos — `GraphStore`, `VectorStore`, `EmbeddingProvider` (DDR-003 Fase A)
- [x] Embeddings locales — `OllamaProvider` con `nomic-embed-text` (DDR-003 Fase B)
- [x] `node_to_text()` — representación textual canónica para embedding
- [x] Factoría de embeddings — `create_provider()` lee `.env`, soporta `ollama` y `none`
- [x] 27 tests de embeddings (mocks + integración en vivo)
- [x] `Neo4jVectorStore` con estrategia de etiqueta secundaria `:Embedded` (DDR-003 Fase C)
- [x] `HybridSearchEngine` — alpha=0.6 vector / 0.4 fulltext + boost por grafo (DDR-003 Fase C)
- [x] Embed-on-write en `EngramaEngine.merge_node()` (DDR-003 Fase C)
- [x] Comando CLI `engrama reindex` (DDR-003 Fase C)
- [x] 18 tests nuevos: vector store, búsqueda híbrida, engine embed, factoría (DDR-003 Fase C)
- [x] Razonamiento temporal — decaimiento de confianza, valid_to, query_at_date, stale_knowledge mejorado (DDR-003 Fase D) ✅

## Fase 12 · Almacenamiento portátil (DDR-004) ✅

> Objetivo: instalación sin dependencias externas. Engrama funciona sobre SQLite + sqlite-vec por defecto; Neo4j pasa a ser un extra opcional. El modelo de datos y la API pública son idénticos en ambos backends. Mergeado el 2026-05-10 vía PR #5.

- [x] **Fase 1** — `Neo4jGraphStore` (síncrono) convierte Records / Nodes / Relationships del driver a dicts Python planos en la frontera. `EngramaEngine` y `recall.py` consumen dicts.
- [x] **Fase 2** — Backend SQLite completo: `engrama/backends/sqlite/{store,async_store,vector,schema.sql}.py`. 36+ métodos del protocolo, FTS5 fulltext, búsqueda vectorial con sqlite-vec.
- [x] **Fase 3** — Factorías `create_stores()` / `create_async_stores()` enlazan CLI, SDK y servidor MCP. Por defecto `GRAPH_BACKEND=sqlite`. Neo4j movido a `[project.optional-dependencies]`.
- [x] **Fase 4** — `OpenAICompatibleProvider` cubre OpenAI, Ollama, LM Studio, vLLM, llama.cpp, Jina con un único cliente. `httpx` promovido a dependencia base.
- [x] **Suite de contratos asíncrona** — `tests/contracts/test_async_graphstore_contract.py` parametrizada sobre ambos backends asíncronos. 421 tests pasando en total (eran 393 antes de esta fase).
- [x] **Bugs detectados y corregidos antes del merge:** deriva del contrato async-store en SQLite (commit `23d5537`), reflect re-fijando Insights aprobados a pending (`e1a0d4e`), búsqueda híbrida perdiendo enriquecimiento en hits puramente vectoriales (`156fbf5`).
- [x] **Guía de decisión pública** — `backends.md` con FAQ y árbol de decisión; `ddr-004.md` con el registro formal.
- [x] **Sanitización de consultas FTS5** — las consultas de usuario que contienen guiones, dos puntos, paréntesis, comillas, etc. se canalizan ahora a través de un sanitizador en `SqliteGraphStore.fulltext_search` (cada token inseguro se envuelve como frase entrecomillada, `"` embebido se duplica según la gramática FTS5). Cierra el fallo de `engrama-mcp-server`; los operadores clave (`AND`/`OR`/`NOT`/`NEAR`) conservan su semántica.
- [x] **Seguimientos** (no bloqueantes): herramienta de migración cross-backend de primera clase `engrama export` / `engrama import` (#30) y matriz de embedders en el README con ejemplos detallados por proveedor (#29).

## Fase 13 · Endurecimiento de seguridad ✅

> DDR-003 Fase E — sanitización de entradas, rastreo de procedencia, recuperación consciente del nivel de confianza.

- [x] Capa de sanitización de entradas (`engrama/core/security.py::Sanitiser`, aplicada en cada frontera de escritura del engine + MCP)
- [x] Campos de procedencia: `source`, `source_agent`, `source_session`, `trust_level` fluyen a través de `merge_node` y están protegidos por el sanitizador contra suplantación
- [x] Ponderación consciente del nivel de confianza en la recuperación de `HybridSearchEngine`
- [x] Aislamiento por ámbito: `MemoryScope` (`org_id` / `user_id` / `agent_id` / `session_id`) aplicado en lecturas y escrituras (publicado en la Fase 14)

## Fase 14 · Memoria multi-ámbito ✅

> DDR-003 Fase F — jerarquía de ámbitos: org_id → user_id → agent_id → session_id.

- [x] Modelo de ámbito: `engrama/core/scope.py::MemoryScope`, configurado por entorno (`ENGRAMA_ORG_ID` / `ENGRAMA_USER_ID` / `ENGRAMA_AGENT_ID` / `ENGRAMA_SESSION_ID`) o por instancia vía kwargs `Engrama(..., user_id=...)`
- [x] Consultas filtradas por ámbito en cada ruta de lectura del store (`fulltext_search`, `get_neighbours`, búsquedas vectoriales)
- [x] Guardia contra inyección de ámbito: el sanitizador elimina claves de ámbito proporcionadas por el llamante, el engine vuelve a aplicar el ámbito activo

## Fase 15 · Benchmarks estándar (en curso)

> DDR-003 Fase G — LOCOMO (objetivo 70–80 %) y LongMemEval (objetivo 75–85 %).

- [x] Scaffold del benchmark + loader de LOCOMO (#46)
- [x] Loader de LongMemEval (#47)
- [x] Runner + scorer recall@k + CLI `engrama bench run` (#48)
- [x] Reporter markdown + CLI `engrama bench report` (#49)
- [x] Pase de endurecimiento del CLI de benchmark (#50)
- [ ] Mediciones de línea base en datasets completos
- [ ] Mejora iterativa

## Fase post-#52 · Contrato dual-vault ✅

> Endurecimiento de la historia de coexistencia multi-MCP para que los agentes dispongan de una señal del servidor que desambigüe el vault de Engrama del vault del usuario en `obsidian-mcp`. Cierra #52.

- [x] Docstrings de herramientas para `engrama_sync_vault` / `engrama_sync_note` / `engrama_ingest` declaran su ámbito de vault (#55, Fase A)
- [x] `Concept:dual-vault-routing-rule` poblado en el grafo y enlazado con `Decision:dual-vault-architecture` (Fase B, 2026-05-16)
- [x] Herramienta MCP `engrama_status` que devuelve ruta del vault, backend, embedder, modo de búsqueda, versión (#56, Fase C)
- [x] Parámetro `dry_run` en `engrama_sync_vault` y `engrama_sync_note` para previsualización antes de escribir (#57, Fase D)
- [x] System prompt v0.5.2 referencia `engrama_status` + `dry_run` en §3

## Fase 16 · Ecosistema de backends (post-DDR-004)

> Ahora que la capa de protocolos está probada con dos backends, se pueden añadir backends adicionales sin tocar el engine, los skills ni el servidor MCP. Cada uno llega tras su propio extra en `[project.optional-dependencies]`.

- [ ] `engrama[arcadedb]` — base de datos multi-modelo (grafo + documento + vector)
- [ ] `engrama[chroma]` — Chroma como vector store dedicado manteniendo SQLite o Neo4j para el grafo
- [ ] `engrama[leann]` — LEANN para índices de embeddings muy grandes
- [ ] `engrama[pgvector]` — Postgres + pgvector para equipos que ya usan Postgres
- [x] Herramienta de migración cross-backend de primera clase `engrama export` / `engrama import` (#30)


## Definición de hecho

1. Código commiteado en el repositorio
2. Tests pasando en ambos backends (la suite de contratos en `tests/contracts/` está parametrizada sobre SQLite y Neo4j; los tests exclusivos de Neo4j se saltan cuando `NEO4J_PASSWORD` no está definido, pero cada comportamiento que verifican también debe cumplirse en SQLite a través de la suite de contratos)
3. Documentado en el archivo de referencia correspondiente (README, ARCHITECTURE, BACKENDS o DDR según el alcance)
4. Mensaje de commit convencional

## Suite de tests

Los recuentos en vivo se quedan obsoletos rápidamente; los dashboards de CI en `main` son la fuente de verdad. Se ejecutan dos jobs en cada PR:

- **Tests (SQLite, sin Docker)** — el contrato que Engrama garantiza a
  cualquiera que ejecute `pip install engrama` sin Neo4j ni `.env`.
  Matriz en Python 3.11 / 3.12 / 3.13.
- **Tests (integración Neo4j)** — todo lo excluido del job de SQLite,
  contra un contenedor de servicio `neo4j:5.26.4-community`.

Los tests están organizados por incumbencia:

- `tests/contracts/` — contratos de protocolo parametrizados sobre ambos
  backends (la línea base *comportamental*; todo lo demás se construye sobre esto).
- `tests/backends/` — comportamiento específico de cada backend (FTS5, sqlite-vec,
  Neo4j async store).
- `tests/test_*.py` — integración de funcionalidades: skills, adaptadores, SDK, CLI,
  herramientas MCP, sanitizador, ámbito, procedencia, temporal, búsqueda híbrida,
  benchmarks, dry-run, etc.

`pytest --collect-only -q | tail -1` da el recuento actual.
