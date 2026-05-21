# Engrama

> Framework de memoria a largo plazo basado en grafos para agentes de IA.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Backend](https://img.shields.io/badge/backend-SQLite_%7C_Neo4j-green.svg)](backends.md)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](../LICENSE)
[![Estado](https://img.shields.io/badge/estado-alpha%20%C2%B7%20instalaci%C3%B3n%20desde%20fuente-orange.svg)](#inicio-r%C3%A1pido-sqlite-cero-dependencias)

Engrama dota a cualquier agente de IA de una memoria persistente y
estructurada respaldada por un **grafo de conocimiento**. En lugar de
almacenes clave-valor planos o bases de datos vectoriales opacas,
Engrama almacena **entidades**, **observaciones** y **relaciones** — y
permite a los agentes recorrer ese grafo para razonar sobre el
conocimiento acumulado.

Hay dos backends de primera clase:

- **SQLite + `sqlite-vec`** (por defecto desde la 0.9) — un único
  archivo, sin servicios externos, `git clone` + `uv sync` y a
  correr (Engrama aún no está en PyPI; instalación desde fuente).
- **Neo4j 5.26 LTS** (opcional) — para producción multiproceso, índices
  vectoriales muy grandes o equipos que ya usan Cypher.

El modelo de datos es idéntico en ambos. Mira **[backends.md](backends.md)**
para la guía completa de elección; el resto de este README asume el
SQLite por defecto.

Inspirado en el concepto de "segundo cerebro" de Karpathy, pero pensado
para agentes en lugar de humanos — y con grafos en vez de wikis.

---

## ¿Por qué grafos?

| | JSON plano / KV | Base vectorial | **Engrama (Grafo)** |
|---|---|---|---|
| Consultas por relaciones | ❌ | ❌ | ✅ nativo |
| Escala a 10k+ memorias | ❌ lento | ✅ | ✅ |
| Funciona sin embeddings | ✅ | ❌ | ✅ (opcional) |
| Local-first / privado | ✅ | depende | ✅ |
| Cero servicios externos | ✅ | ❌ | ✅ (SQLite) |
| "¿Qué proyectos usan FastMCP?" | escaneo | aproximado | recorrido a 1 salto |

---

## Requisitos previos

Necesitas dos cosas para arrancar con el backend SQLite por defecto.
**Docker no hace falta** salvo que decidas usar Neo4j.

| Requisito | Versión | Cómo comprobar | Guía de instalación |
|---|---|---|---|
| **Python** | 3.11 o superior | `python --version` | [python.org/downloads](https://www.python.org/downloads/) |
| **uv** (gestor de paquetes Python) | cualquier versión reciente | `uv --version` | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

> **Usuarios de Windows:** después de instalar Python, asegúrate de marcar
> "Add Python to PATH". Tras instalar uv, puede que necesites reiniciar
> el terminal.

**Opcionales:**

- [Obsidian](https://obsidian.md/) — solo necesario para sincronización con vault.
- Un servidor de embeddings local para búsqueda semántica — Ollama, LM
  Studio, vLLM, llama.cpp o cualquier servicio que hable la API
  OpenAI-compatible. Ver [Configuración de embeddings](#configuración-de-embeddings-opcional).
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) —
  solo si optas por el backend Neo4j.

---

## Inicio rápido (SQLite, cero dependencias)

### Paso 1: Clonar e instalar

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync
```

Esto crea un entorno virtual en `.venv/` e instala las dependencias
base (`sqlite-vec`, `httpx`, `pydantic`, `python-dotenv`, `pyyaml`). El
driver de Neo4j **no** se instala por defecto.

### Paso 2: Inicializar el esquema

```bash
uv run engrama init --profile developer
```

El archivo SQLite se crea automáticamente en `~/.engrama/engrama.db` la
primera vez. El esquema se aplica solo — sin restricciones que
ejecutar, sin servicio que esperar. Se cargan los nodos semilla del
perfil que elijas.

### Paso 3: Verificar

```bash
uv run engrama verify
```

Salida esperada: `backend=sqlite, ok=true, ...`

### Paso 4: Usarlo

Tres formas:

**A) Desde Claude Desktop o Codex** — ver [Integración MCP](#integración-mcp) más abajo.

**B) Desde Python:**

```python
from engrama import Engrama

with Engrama() as eng:
    eng.remember("Technology", "FastAPI", "High-performance async framework")
    eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")
    results = eng.search("microservices")
```

**C) Desde la línea de comandos:**

```bash
uv run engrama search "FastAPI"
uv run engrama reflect
```

> **Nota:** todos los comandos `engrama` de la CLI necesitan el prefijo
> `uv run` salvo que actives primero el entorno virtual con
> `.venv\Scripts\Activate.ps1` (Windows) o `source .venv/bin/activate`
> (Linux/macOS).

---

## Inicio rápido (Neo4j, opt-in)

Si has leído [backends.md](backends.md) y decides que necesitas Neo4j —
escrituras multi-proceso, índices vectoriales muy grandes, una cadena
de herramientas Cypher existente — sigue esta ruta en lugar de la
anterior.

### Paso 1: Instalar con el extra Neo4j

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync --extra neo4j
```

### Paso 2: Configurar credenciales

```bash
# Linux / macOS / Git Bash
cp .env.example .env
# PowerShell (Windows)
Copy-Item .env.example .env
```

Abre `.env` y configura:

1. `GRAPH_BACKEND=neo4j`
2. `NEO4J_PASSWORD` — elige una contraseña fuerte
3. `VAULT_PATH` (opcional) — ruta absoluta a tu vault de Obsidian si
   quieres usar las herramientas de sincronización

### Paso 3: Arrancar Neo4j

```bash
docker compose up -d
```

Espera unos 15 segundos. Verifica con `docker ps` — `engrama-neo4j`
debe estar `healthy`.

### Paso 4: Inicializar el esquema

```bash
uv run engrama init --profile developer
```

Esto genera y aplica las restricciones Cypher + los índices fulltext y
vectorial.

### Paso 5: Verificar

```bash
uv run engrama verify
```

Salida esperada: `Connected to Neo4j at bolt://localhost:7687`.

El resto del flujo (SDK Python, CLI, integración MCP) es idéntico al
camino SQLite.

---

## Configuración de embeddings (opcional)

Engrama funciona de fábrica solo con búsqueda fulltext. Para **búsqueda
por similitud semántica** — encontrar nodos conceptualmente relacionados,
no solo coincidencias por palabra clave — activa los embeddings vía
cualquier servicio compatible con OpenAI.

Configura cuatro variables de entorno (`EMBEDDING_PROVIDER`,
`EMBEDDING_MODEL`, `EMBEDDING_DIMENSIONS`, `OPENAI_BASE_URL`) más
`OPENAI_API_KEY` cuando el servidor lo requiera. Luego ejecuta
`uv run engrama verify` — imprime `Embeddings: ok (provider=…, model=…)`
al éxito y `Embeddings: degraded …` si el endpoint o el modelo no
están disponibles.

Tras activar embeddings sobre un grafo existente, ejecuta
`uv run engrama reindex` para embeberse los nodos antiguos. Los nodos
nuevos se embeben automáticamente al crearse. Si el endpoint deja de
estar disponible, la búsqueda degrada a `fulltext_only` y expone el
motivo en `search_mode` — Engrama nunca devuelve resultados vacíos en
silencio.

### Matriz de proveedores (ejemplos trabajados)

Todos los proveedores hablan el formato OpenAI `/v1/embeddings`, así
que `EMBEDDING_PROVIDER=openai` es la opción recomendada para todos
ellos (incluido Ollama local). La ruta nativa de Ollama `/api/embed`
sigue soportada con `EMBEDDING_PROVIDER=ollama` — se mantiene por
retrocompatibilidad, pero elige un estilo y mantente en él.

#### Ollama (local, recomendado para empezar)

Local, gratis, sin API key, ~274 MB de descarga. La mejor ruta si
solo quieres probar búsqueda semántica.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:11434/v1
```

```bash
# Instalar: https://ollama.com
ollama pull nomic-embed-text
uv run engrama verify   # → "Embeddings: ok (provider=openai, model=nomic-embed-text)"
```

Otros modelos locales sólidos: `mxbai-embed-large` (1024 dims, inglés),
`bge-m3` (1024 dims, multilingüe). Empareja `EMBEDDING_DIMENSIONS` con
el modelo — discrepancias hacen que la búsqueda híbrida caiga a fulltext.

#### OpenAI

API cloud, de pago, latencia <10 ms. La implementación de referencia
del contrato `/v1/embeddings`.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
```

`text-embedding-3-large` (3072 dims) también funciona — pon
`EMBEDDING_DIMENSIONS=3072`. Los modelos `text-embedding-3-*` aceptan
un valor de dimensiones menor para truncar (p. ej.
`EMBEDDING_DIMENSIONS=512` con el modelo small y OpenAI devuelve
vectores de 512 dims).

#### LM Studio

Servidor local gestionado por GUI, útil cuando quieres selector de
modelo y gestor de descargas. Arranca un modelo de embeddings desde
la pestaña "Local Server" de LM Studio primero.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-ai/nomic-embed-text-v1.5-GGUF
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:1234/v1
OPENAI_API_KEY=lm-studio
```

LM Studio ignora el valor de la API key pero su cliente HTTP espera
que la cabecera esté presente — cualquier string no vacío sirve.

#### vLLM

Servidor de inferencia de alto throughput, buena opción cuando
embebes en bulk y quieres batching contra una GPU.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=intfloat/e5-mistral-7b-instruct
EMBEDDING_DIMENSIONS=4096
OPENAI_BASE_URL=http://localhost:8000/v1
OPENAI_API_KEY=any
```

Arranca vLLM con un modelo capaz de embeddings:
`vllm serve intfloat/e5-mistral-7b-instruct --task embed`. Empareja
`EMBEDDING_DIMENSIONS` con el hidden size del modelo.

#### llama.cpp server

Servidor binario único CPU/GPU, mínimas piezas móviles. Útil para
modelos de embedding pequeños en hosts con recursos limitados.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=nomic-embed-text-v1.5.Q4_K_M
EMBEDDING_DIMENSIONS=768
OPENAI_BASE_URL=http://localhost:8080/v1
OPENAI_API_KEY=any
```

Arranca con `--embedding` y la ruta al GGUF:
`./llama-server -m nomic-embed-text-v1.5.Q4_K_M.gguf --embedding --port 8080`.

#### Jina (cloud)

Embeddings multilingües hosteados con contexto largo. Pago por token,
sin self-hosting.

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=jina-embeddings-v3
EMBEDDING_DIMENSIONS=1024
OPENAI_BASE_URL=https://api.jina.ai/v1
OPENAI_API_KEY=jina_...
```

`jina-embeddings-v3` es multilingüe (89 idiomas) con 8192 tokens de
contexto. Para entradas más cortas y menos latencia, usa
`jina-embeddings-v2-base-en` con `EMBEDDING_DIMENSIONS=768`.

---

## Integración MCP

Engrama actúa como capa de abstracción entre el agente de IA y el
backend de almacenamiento. Los clientes MCP se conectan al servidor de
Engrama — nunca ven credenciales, cadenas de conexión ni consultas en
crudo.

### Claude Desktop

**1. Localiza tu archivo de configuración de Claude Desktop:**

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**2. Añade el servidor de Engrama.**

La configuración de abajo usa SQLite por defecto. El flag `--backend`
es opcional (por defecto `sqlite`), pero hacerlo explícito ayuda a
leer la config de un vistazo:

```json
{
  "mcpServers": {
    "engrama": {
      "command": "uv",
      "args": [
        "run", "--directory", "C:\\Proyectos\\engrama",
        "--extra", "mcp",
        "engrama-mcp", "--backend", "sqlite"
      ]
    }
  }
}
```

Para el backend Neo4j cambia `--backend sqlite` por `--backend neo4j`
(o quita el flag y pon `GRAPH_BACKEND=neo4j` en `.env`). Asegúrate de
añadir también el extra: `"--extra", "mcp", "--extra", "neo4j"`.

**Importante:** cambia `C:\\Proyectos\\engrama` por la ruta real donde
clonaste el repositorio. En macOS/Linux usa barras normales (p. ej.
`/home/tu_usuario/engrama`). Aquí no hacen falta credenciales — el
servidor las lee desde `.env` cuando funciona contra Neo4j.

**3. Reinicia Claude Desktop** completamente (sal y vuelve a abrir).

### Codex

Codex soporta servidores MCP locales por `stdio`, así que puedes
registrar Engrama directamente desde el CLI:

```bash
codex mcp add engrama -- uv run --directory C:\Proyectos\engrama --extra mcp engrama-mcp --backend sqlite
```

Para Neo4j, cambia `--backend sqlite` por `--backend neo4j` y añade
también el extra:

```bash
codex mcp add engrama -- uv run --directory C:\Proyectos\engrama --extra mcp --extra neo4j engrama-mcp --backend neo4j
```

Después comprueba que quedó registrado:

```bash
codex mcp list
```

Igual que en Claude Desktop, cambia `C:\Proyectos\engrama` por la ruta
real donde clonaste el repositorio.

### ChatGPT Desktop

ChatGPT **no** usa directamente esta configuración local por `stdio`.
La documentación actual de OpenAI describe los conectores MCP
personalizados de ChatGPT como servidores MCP **remotos** importados
desde `Settings -> Connectors`, sobre HTTP/SSE y no como un comando
local.

Eso significa que `engrama-mcp` encaja bien con Claude Desktop y
Codex, pero **todavía no** como integración directa de ChatGPT Desktop.
Para usar Engrama desde ChatGPT habría que exponer un endpoint MCP
remoto y empaquetarlo como conector personalizado de ChatGPT.

Ahora deberías ver las doce herramientas:

| Herramienta | Descripción |
|------|-------------|
| `engrama_status` | Introspección de solo lectura (vault path, backend, embedder, modo de búsqueda, versión). Llámala al inicio de sesión cuando hay otros MCPs de Obsidian conectados. |
| `engrama_search` | Búsqueda híbrida (vector + fulltext + boost de grafo + temporal) |
| `engrama_remember` | Crear o actualizar un nodo (siempre MERGE) |
| `engrama_relate` | Crear una relación entre dos nodos |
| `engrama_context` | Recuperar el vecindario de un nodo |
| `engrama_sync_note` | Sincronizar una sola nota de Obsidian con el grafo. Acepta `dry_run=true` para previsualizar el impacto. |
| `engrama_sync_vault` | Escaneo completo del vault, reconciliar todas las notas. Acepta `dry_run=true` para previsualizar create/update counts + ficheros que recibirían `engrama_id`. |
| `engrama_ingest` | Leer contenido + extraer conocimiento automáticamente |
| `engrama_reflect` | Detección adaptativa de patrones entre entidades → Insights |
| `engrama_surface_insights` | Leer Insights pendientes para revisión |
| `engrama_approve_insight` | Aprobar o descartar un Insight |
| `engrama_write_insight_to_vault` | Escribir un Insight aprobado en Obsidian |

Consulta [`examples/claude_desktop/system-prompt.md`](../examples/claude_desktop/system-prompt.md)
para un system prompt listo para pegar que enseña a Claude a usar el
grafo de memoria.

---

## SDK de Python

Usa Engrama directamente desde cualquier script de Python — sin MCP:

```python
from engrama import Engrama

# Por defecto SQLite en ~/.engrama/engrama.db
with Engrama() as eng:
    # Escribir
    eng.remember("Technology", "FastAPI", "High-performance async framework")
    eng.associate("MyProject", "Project", "USES", "FastAPI", "Technology")

    # Leer
    results = eng.recall("FastAPI", hops=2)
    hits = eng.search("microservices", limit=5)

    # Reflexionar
    insights = eng.reflect()
    pending = eng.surface_insights()
    eng.approve_insight(pending[0].title)

    # Olvidar
    eng.forget("Technology", "OldLib")
    eng.forget_by_ttl("Technology", days=365, purge=True)
```

Para apuntar explícitamente a Neo4j:

```python
with Engrama(backend="neo4j") as eng:
    ...
```

O pon `GRAPH_BACKEND=neo4j` en `.env` y llama a `Engrama()` sin
argumentos. Todos los métodos están documentados con docstrings — usa
`help(Engrama)` o el autocompletado de tu IDE para explorarlos.

---

## Referencia de la CLI

Todos los comandos requieren el prefijo `uv run` (o un entorno virtual activado):

```bash
uv run engrama init --profile developer                         # SQLite (por defecto)
uv run engrama init --profile base --modules hacking teaching   # Composable
uv run engrama init --profile developer --dry-run               # Vista previa
uv run engrama verify                                           # Comprobación de salud
uv run engrama search "microservices"                           # Búsqueda híbrida
uv run engrama reflect                                          # Detección de patrones
uv run engrama reindex                                          # Re-embedding por lotes
uv run engrama decay --dry-run                                  # Vista previa del decay
uv run engrama decay --rate 0.01                                # Aplicar decay suave
uv run engrama decay --rate 0.1 --min-confidence 0.05           # Agresivo + archivar
uv run engrama export dump.ndjson                               # Volcado backend-agnostic
uv run engrama import dump.ndjson --purge                       # Restaurar (limpiando destino)
```

Para sobrescribir el backend en un comando puntual:

```bash
GRAPH_BACKEND=neo4j uv run engrama verify
```

`engrama export` vuelca el grafo + vectores del backend activo a un
fichero NDJSON. `engrama import` lo restaura. Funciona cross-backend
— para migrar SQLite a Neo4j:

```bash
GRAPH_BACKEND=sqlite uv run engrama export dump.ndjson
GRAPH_BACKEND=neo4j  uv run engrama import dump.ndjson --purge
```

Los vectores se restauran sólo cuando origen y destino tienen las
mismas dimensiones de embedding; si no coinciden se saltan y
`engrama reindex` los regenera con el embedder activo.

---

## Modos de búsqueda

Tres modos, controlados por `EMBEDDING_PROVIDER`:

**Solo fulltext** (`EMBEDDING_PROVIDER=none`, por defecto) —
coincidencia por palabras clave. SQLite usa FTS5; Neo4j usa su índice
fulltext nativo. Funciona sin dependencias extra.

**Híbrida** (`EMBEDDING_PROVIDER=ollama` o `openai`) — combina
similitud semántica (búsqueda vectorial) con coincidencia por palabras
clave, más un boost por topología del grafo y un factor temporal.
Encuentra nodos conceptualmente relacionados incluso sin coincidencia
exacta de palabras clave.

**Cómo activar la búsqueda híbrida:**
1. Establece `EMBEDDING_PROVIDER` en `.env` (ver
   [Configuración de embeddings](#configuración-de-embeddings-opcional)).
2. Ejecuta `uv run engrama reindex` para generar embeddings de nodos
   existentes.
3. Los nodos nuevos reciben embeddings automáticamente al crearse.

La fórmula de puntuación es:

    final = α × vector + (1-α) × fulltext + β × graph_boost + γ × temporal

con α=0.6, β=0.15, γ=0.1 por defecto. Configurables vía `HYBRID_ALPHA`
y `HYBRID_GRAPH_BETA` en `.env`.

---

## Personalizar tu grafo (onboarding)

Engrama viene con un perfil `developer`, pero el esquema debería
encajar con **tu** mundo, no con una plantilla genérica. El grafo de
una enfermera no se parece en nada al de un desarrollador — y esa es
la idea.

### Opción A: Usar el perfil `developer` integrado

```bash
uv run engrama init --profile developer
```

Crea nodos para Projects, Technologies, Decisions, Problems, Courses,
Concepts y Clients.

### Opción B: Que Claude construya tus módulos (recomendado)

Abre Claude Desktop con Engrama conectado y dile:

> "Quiero configurar Engrama para mi trabajo. Soy enfermera con un
> máster en biología, doy clases a estudiantes de grado y los fines de
> semana me encanta cocinar."

Claude te entrevistará durante unos 5 minutos — qué cosas registras día
a día, cómo se conectan en tu cabeza — y luego generará módulos de
dominio personalizados: `nursing.yaml`, `biology.yaml`,
`teaching.yaml`, `cooking.yaml`. Los compone con el `base.yaml`
universal y aplica el esquema, todo en una misma conversación. No hace
falta saber YAML.

### Opción C: Componer a partir de módulos existentes

```bash
uv run engrama init --profile base --modules hacking teaching photography ai
```

Esto fusiona `profiles/base.yaml` (Project, Concept, Decision, Problem,
Technology, Person) con módulos específicos de dominio de
`profiles/modules/`.

**Módulos de ejemplo incluidos:**

| Módulo | Añade |
|---|---|
| `hacking` | Target, Vulnerability, Technique, Tool, CTF |
| `teaching` | Course, Client, Exercise, Material |
| `photography` | Photo, Location, Species, Gear |
| `ai` | Model, Dataset, Experiment, Pipeline |

Estos cuatro son **ejemplos, no una lista cerrada** — cualquiera puede
crear un módulo para cualquier dominio.

### Opción D: Escribir tu propio módulo

Un módulo es solo un pequeño archivo YAML en `profiles/modules/`.
Ejemplo de cocina:

```yaml
name: cooking
description: Recipes, techniques, and ingredients

nodes:
  - label: Recipe
    properties: [name, cuisine, difficulty, time, notes]
    required: [name]
    description: "A dish or preparation."
  - label: Ingredient
    properties: [name, category, season, notes]
    required: [name]
    description: "A food ingredient — vegetable, spice, protein."
  - label: CookingTechnique
    properties: [name, type, notes]
    required: [name]
    description: "A culinary method — sous vide, fermentation, braising."

relations:
  - {type: USES,      from: Recipe,     to: Ingredient}
  - {type: APPLIES,   from: Recipe,     to: CookingTechnique}
  - {type: RELATED,   from: Ingredient, to: Concept}        # 'Concept' viene de base.yaml
  - {type: DOCUMENTS, from: Recipe,     to: Project}        # 'Project' viene de base.yaml
```

Guárdalo como `profiles/modules/cooking.yaml`, y luego:

```bash
uv run engrama init --profile base --modules cooking teaching
```

**Reglas para módulos:**

- Los nodos usan etiquetas en PascalCase y `name` o `title` como clave de merge.
- Las relaciones pueden referenciar cualquier etiqueta de `base.yaml` sin redefinirla.
- Si dos módulos definen la misma etiqueta, las propiedades se fusionan automáticamente.
- Los tipos de relación deben ser verbos (USES, TREATS, COVERS), no sustantivos.

Consulta [`profiles/developer.yaml`](../profiles/developer.yaml) para un
perfil independiente completo, y
[`engrama/skills/onboard/references/example-profiles.md`](../engrama/skills/onboard/references/example-profiles.md)
para perfiles trabajados en enfermería, abogacía, PM, creativos
freelance.

### Consejos para buenos perfiles

- **3 a 5 tipos de nodo por módulo** es el punto óptimo. La base ya te
  da 6. Un usuario multi-rol típico acaba con 12–18 en total.
- Usa `title` como clave de merge para cosas con forma de frase
  (decisiones, problemas, protocolos). Usa `name` para todo lo demás.
- Incluye siempre `status` en nodos con ciclo de vida — reflect lo usa
  para distinguir elementos abiertos vs resueltos.
- Ante la duda, deja que Claude genere el módulo por ti (Opción B).

---

## Referencia de configuración

| Variable | Por defecto | Propósito |
|---|---|---|
| `GRAPH_BACKEND` | `sqlite` | `sqlite`, `neo4j` o `null` (testing) |
| `VECTOR_BACKEND` | acompaña al grafo | Auto (`sqlite-vec` para SQLite) |
| `ENGRAMA_DB_PATH` | `~/.engrama/engrama.db` | Archivo SQLite |
| `NEO4J_URI` | `bolt://localhost:7687` | URI de conexión a Neo4j |
| `NEO4J_USERNAME` | `neo4j` | Usuario Neo4j |
| `NEO4J_PASSWORD` | — | Contraseña Neo4j (requerida con `GRAPH_BACKEND=neo4j`) |
| `NEO4J_DATABASE` | `neo4j` | Nombre de base de datos Neo4j |
| `ENGRAMA_PROFILE` | `developer` | Perfil para generar el esquema |
| `VAULT_PATH` | `~/Documents/vault` | Raíz del vault de Obsidian |
| `EMBEDDING_PROVIDER` | `none` | `none`, `ollama` u `openai` |
| `EMBEDDING_MODEL` | `nomic-embed-text` | Nombre del modelo |
| `EMBEDDING_DIMENSIONS` | `768` | Tamaño del vector |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Endpoint OpenAI-compat |
| `OPENAI_API_KEY` | — | API key (cuando hace falta) |
| `OLLAMA_URL` | `http://localhost:11434` | Endpoint API de Ollama |
| `HYBRID_ALPHA` | `0.6` | Peso vector vs fulltext |
| `HYBRID_GRAPH_BETA` | `0.15` | Peso del boost por topología |

---

## Documentación

- [Vision](vision.md) — por qué existe esto
- [Backends](backends.md) — guía SQLite vs Neo4j
- [Architecture](architecture.md) — diseño técnico y estructura de directorios
- [Graph Schema](graph-schema.md) — nodos, relaciones, referencia de consultas
- [Roadmap](roadmap.md) — fases de desarrollo y estado
- [Changelog](changelog.md) — notas de versión
- [Contributing](contributing.md) — cómo contribuir
- [DDR-001](ddr-001.md) — clasificación facetada
- [DDR-002](ddr-002.md) — sincronización bidireccional vault ↔ grafo
- [DDR-003](ddr-003.md) — capa de protocolos, embeddings, búsqueda híbrida, razonamiento temporal
- [DDR-004](ddr-004.md) — almacenamiento portátil (SQLite por defecto)
- [Glosario](glosario_es.md) — términos clave

---

## Licencia

Engrama está licenciado bajo Apache License 2.0.
Copyright 2026 Sinensia IT Solutions.

Eres libre de usar, modificar y distribuir Engrama tanto en proyectos
personales como comerciales. La licencia Apache 2.0 incluye una
concesión explícita de patentes, dándote tranquilidad para adoptar
Engrama en entornos empresariales sin preocupaciones de propiedad
intelectual.

### Contribuciones

Al enviar un pull request aceptas que tu contribución se licencia bajo
los mismos términos de Apache 2.0. Usamos un Developer Certificate of
Origin (DCO) — firma tus commits con `git commit -s`.

### Extensiones comerciales

Determinadas funcionalidades premium (como hosting gestionado,
colaboración multi-tenant y analítica avanzada) podrán ofrecerse bajo
una licencia comercial separada. El motor principal, las herramientas
MCP y toda la funcionalidad de cara a la comunidad permanecen
totalmente open source bajo Apache 2.0.

Para consultas de licencias comerciales, por favor visita el formulario de contacto en [Sinensia](https://sinensia.com).

---

## Relacionado

- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — Servidor MCP genérico para Neo4j (Engrama usa su propio adaptador nativo que habla SQLite y Neo4j).
- [sqlite-vec](https://github.com/asg017/sqlite-vec) — Extensión de búsqueda vectorial para SQLite que da vida al backend Engrama por defecto.
