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
Base de datos de grafos open source (con versión LTS gratuita). Engrama la usa como motor de almacenamiento. Se administra con un lenguaje propio llamado **Cypher**.

**Cypher**
El lenguaje de consultas de Neo4j. Sintaxis tipo "ASCII art": `(a:Project)-[:USES]->(b:Technology)` significa "un nodo Project conectado a un Technology mediante una relación USES".

**MERGE**
Operación de Cypher que crea un nodo si no existe, o lo actualiza si ya existe. Engrama hace MERGE siempre, evitando duplicados.

**Schema (esquema)**
Definición formal de qué tipos de nodos y relaciones puede haber en el grafo. En Engrama se describe en YAML y se aplica a Neo4j al ejecutar `engrama init`.

---

## Búsqueda

**Embedding**
Representación de un texto como un vector numérico (en Engrama, 768 dimensiones con `nomic-embed-text`). Textos con significado parecido producen vectores cercanos en el espacio. Es la base de la búsqueda semántica.

**Base de datos vectorial**
Almacén optimizado para encontrar embeddings cercanos a uno dado (vecinos más próximos). Engrama no usa una BD vectorial separada — Neo4j 5 incluye índice vectorial nativo.

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

**YAML**
Formato de serialización legible por humanos, basado en indentación. Se usa para el esquema de Engrama y para el frontmatter de Obsidian.

**PascalCase**
Convención de naming donde cada palabra empieza por mayúscula, sin separadores: `Project`, `CookingTechnique`. Engrama exige PascalCase para etiquetas de nodos.

**Apache 2.0**
Licencia open source permisiva con concesión explícita de patentes. Permite uso comercial sin obligación de liberar código derivado.

**DCO (Developer Certificate of Origin)**
Mecanismo ligero, alternativo al CLA tradicional, donde el contribuidor certifica con `git commit -s` (firma) que tiene derecho a aportar el código bajo la licencia del proyecto. No requiere firmar documentos legales.
