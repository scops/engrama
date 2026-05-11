# Glosario — Engrama

Términos clave que aparecen en el README, ordenados por bloques temáticos.

---

## Grafos y bases de datos

**Grafo (de conocimiento)**
Estructura de datos formada por **nodos** (entidades: una persona, un proyecto, una tecnología) y **relaciones** (aristas entre nodos: "USA", "TRATA", "DOCUMENTA"). A diferencia de una tabla, las conexiones son ciudadanos de primera clase: puedes preguntar "¿qué proyectos usan FastMCP?" y se responde recorriendo aristas, no escaneando filas.

**Nodo**
Cada elemento individual del grafo. Tiene una **etiqueta** (`Project`, `Technology`...) y **propiedades** (`name`, `status`, `notes`). En Engrama, una propiedad concreta (`name` o `title`) actúa como clave única para hacer *merge*.

**Relación (o arista / edge)**
La conexión entre dos nodos. Tiene un **tipo** en mayúsculas y suele ser un verbo: `USES`, `TREATS`, `DOCUMENTS`. Es dirigida: va de un nodo origen a un nodo destino.

**Neo4j**
Base de datos de grafos open source (con versión LTS gratuita). Engrama la soporta como backend opcional para producción multi-proceso, índices vectoriales muy grandes o equipos que ya usan Cypher. Se administra con un lenguaje propio llamado **Cypher**. Se instala con `uv sync --extra neo4j` (o `pip install engrama[neo4j]` cuando Engrama llegue a PyPI).

**SQLite**
Base de datos relacional embebida en un único archivo. Es el backend por defecto de Engrama desde la versión 0.9 (DDR-004): cero servicios externos, cero Docker, cero JVM. Cualquier laptop o VM puede ejecutar Engrama con `git clone` + `uv sync` y un comando `uv run engrama init` (Engrama todavía no está publicado en PyPI).

**sqlite-vec**
Extensión de SQLite que añade búsqueda vectorial mediante una "virtual table" llamada `vec0`. Engrama la usa para que los embeddings vivan en el mismo archivo `.db` que el grafo. Búsqueda por fuerza bruta — cómoda hasta ~100k vectores; más allá compensa pasar a Neo4j (ver [BACKENDS.md](BACKENDS.md)).

**FTS5**
Motor de búsqueda fulltext integrado en SQLite (similar a Lucene). Engrama lo usa para la búsqueda por palabras clave cuando el backend es SQLite. En Neo4j el equivalente es el índice fulltext nativo (`memory_search`).

**Cypher**
El lenguaje de consultas de Neo4j. Sintaxis tipo "ASCII art": `(a:Project)-[:USES]->(b:Technology)` significa "un nodo Project conectado a un Technology mediante una relación USES". El backend SQLite no habla Cypher; usa SQL traducido encapsulado tras los métodos del protocolo `GraphStore`, así que los callers no escriben ni Cypher ni SQL a mano.

**Backend (de almacenamiento)**
La implementación concreta del protocolo `GraphStore` + `VectorStore` que Engrama usa para guardar el grafo. Hoy hay dos: `sqlite` (por defecto) y `neo4j` (opt-in). Cambiar de uno a otro es una variable de entorno (`GRAPH_BACKEND=...`).

**Protocolo (`GraphStore` / `VectorStore` / `EmbeddingProvider`)**
Las interfaces abstractas (en `engrama/core/protocols.py`) que cualquier backend o proveedor debe implementar. Skills, MCP server, CLI y SDK hablan solo con los protocolos — no saben qué hay debajo. Esto es lo que permite que añadir un backend nuevo (Chroma, ArcadeDB, pgvector, ...) no toque ni el motor ni las tools.

**Factory (de backends)**
La función `create_stores()` / `create_async_stores()` en `engrama/backends/__init__.py` que lee `GRAPH_BACKEND` y devuelve el backend adecuado. Único punto de wiring entre la config y el resto del código.

**MERGE**
Operación de Cypher que crea un nodo si no existe, o lo actualiza si ya existe. Engrama hace MERGE siempre, evitando duplicados.

**Schema (esquema)**
Definición formal de qué tipos de nodos y relaciones puede haber en el grafo. En Engrama se describe en YAML y se aplica a Neo4j al ejecutar `engrama init`.

---

## Búsqueda

**Embedding**
Representación de un texto como un vector numérico (por defecto 768 dimensiones con `nomic-embed-text` vía Ollama, pero el embedder OpenAI-compatible acepta cualquier modelo y dimensión). Textos con significado parecido producen vectores cercanos. Es la base de la búsqueda semántica.

**Embedder OpenAI-compatible**
Un único cliente HTTP en Engrama que habla el endpoint `/v1/embeddings` definido por OpenAI y reutilizado por Ollama (modo `/v1`), LM Studio, vLLM, llama.cpp, Jina y otros. Con cambiar `OPENAI_BASE_URL` cambias de proveedor sin tocar código (DDR-004).

**Base de datos vectorial**
Almacén optimizado para encontrar embeddings cercanos a uno dado (vecinos más próximos). Engrama no requiere una BD vectorial dedicada: con SQLite se usa la extensión `sqlite-vec` en el mismo archivo, y con Neo4j el índice vectorial nativo de Neo4j 5.

**Búsqueda fulltext**
Búsqueda clásica por palabras clave (tipo Lucene). Encuentra "Neo4j" si tu consulta contiene literalmente "Neo4j". Funciona sin embeddings.

**Búsqueda semántica**
Búsqueda por significado. Encuentra "base de datos de grafos" aunque hayas escrito "graph store", porque sus embeddings son cercanos.

**Búsqueda híbrida**
La que usa Engrama por defecto cuando hay embeddings activos: combina vectorial + fulltext + un *boost* por topología del grafo (nodos muy conectados puntúan más) + un factor temporal (lo reciente puntúa más).

**Reindex**
Volver a generar los embeddings de todos los nodos existentes. Se hace tras activar embeddings por primera vez o cambiar de modelo.

---

## El ecosistema MCP

**MCP (Model Context Protocol)**
Protocolo abierto de Anthropic que estandariza cómo un agente de IA (cliente, p. ej. Claude Desktop) se conecta a herramientas y datos externos (servidores MCP). Sustituye al patrón de "function calling" propietario de cada modelo por un contrato común.

**Servidor MCP**
Proceso que expone herramientas (`engrama_search`, `engrama_remember`...) a un cliente MCP. Engrama incluye un servidor MCP que actúa como capa de abstracción sobre Neo4j: el cliente nunca ve credenciales ni Cypher en crudo.

**Cliente MCP**
La aplicación que consume el servidor (Claude Desktop, Cursor, Claude Code...). Carga la configuración de los servidores y media entre el LLM y las herramientas.

**Tool / herramienta**
Función expuesta por un servidor MCP que el modelo puede invocar. Engrama expone once: `engrama_search`, `engrama_remember`, `engrama_relate`, etc.

---

## Obsidian y vaults

**Obsidian**
Editor de notas en Markdown que trabaja sobre archivos locales. Cada conjunto de notas vive en una carpeta llamada **vault**.

**Vault**
La carpeta raíz que contiene tus notas Markdown de Obsidian. Engrama lee y escribe ahí mediante las herramientas `engrama_sync_*`.

**Frontmatter (YAML frontmatter)**
Bloque de metadatos al principio de una nota, entre `---`, escrito en YAML. Engrama lo usa para etiquetar qué nodos genera cada nota.

---

## Conceptos propios de Engrama

**Insight**
Patrón detectado automáticamente al recorrer el grafo cruzando entidades — por ejemplo "todos tus proyectos que usan PostgreSQL acabaron migrando a PostGIS". Los Insights se generan con `engrama_reflect`, quedan **pendientes** de revisión, y tú decides si los apruebas (se escriben al vault) o los descartas. Es supervisión humana sobre lo que el sistema infiere.

**Reflect (reflexión)**
La operación de mirar el grafo en busca de patrones recurrentes y producir Insights. No inventa hechos: agrega los que ya existen como nodos.

**Ingest**
Leer un documento o conversación y extraer automáticamente nodos y relaciones para el grafo. Atajo frente a poblar a mano con `remember` + `relate`.

**Confidence decay**
Mecanismo opcional que reduce con el tiempo el peso de los nodos no reforzados, simulando el olvido. `engrama decay --rate 0.01` aplica un decaimiento suave; con un mínimo bajo, archiva nodos casi olvidados.

**TTL (Time To Live)**
Tiempo de vida de un nodo. `forget_by_ttl(..., days=365)` elimina (o purga) los nodos no tocados en un año.

**Profile / module**
**Profile**: definición completa del esquema (`developer.yaml`). **Module**: pieza más pequeña que se compone con otras (`hacking.yaml`, `teaching.yaml`). Engrama trae un `base.yaml` universal y deja que combines módulos sobre él.

---

## Otros términos del README

**Ollama**
Runtime para correr LLMs y modelos de embeddings localmente, en tu propia máquina. Engrama lo usa opcionalmente para generar embeddings sin depender de APIs externas.

**FastMCP**
Framework Python para construir servidores MCP con poco código (decoradores `@mcp.tool` sobre funciones). Engrama está construido encima de FastMCP.

**uv**
Gestor moderno de paquetes y entornos virtuales para Python, escrito en Rust. Reemplaza a `pip` + `venv` + `pip-tools`. Mucho más rápido. `uv run X` ejecuta un comando dentro del entorno del proyecto sin necesidad de activarlo.

**Extra (de instalación)**
Grupo opcional de dependencias declarado en `pyproject.toml`. `uv sync` instala solo el núcleo (SQLite); `uv sync --extra neo4j` añade el driver de Neo4j; `uv sync --extra mcp` añade FastMCP. Pueden combinarse: `uv sync --extra neo4j --extra mcp`. Cuando Engrama se publique en PyPI el equivalente será `pip install engrama[neo4j,mcp]`.

**DDR (Design Decision Record)**
Documento corto que registra una decisión arquitectónica importante, su contexto y sus consecuencias. Engrama tiene cuatro: DDR-001 clasificación facetada, DDR-002 sincronización bidireccional vault ↔ grafo, DDR-003 protocolos + embeddings + búsqueda híbrida + razonamiento temporal, DDR-004 almacenamiento portátil (SQLite por defecto).

**YAML**
Formato de serialización legible por humanos, basado en indentación. Se usa para el esquema de Engrama y para el frontmatter de Obsidian.

**PascalCase**
Convención de naming donde cada palabra empieza por mayúscula, sin separadores: `Project`, `CookingTechnique`. Engrama exige PascalCase para etiquetas de nodos.

**Apache 2.0**
Licencia open source permisiva con concesión explícita de patentes. Permite uso comercial sin obligación de liberar código derivado.

**DCO (Developer Certificate of Origin)**
Mecanismo ligero, alternativo al CLA tradicional, donde el contribuidor certifica con `git commit -s` (firma) que tiene derecho a aportar el código bajo la licencia del proyecto. No requiere firmar documentos legales.
