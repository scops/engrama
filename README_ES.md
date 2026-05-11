# Engrama

> Framework de memoria a largo plazo basado en grafos para agentes de IA.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Backend](https://img.shields.io/badge/backend-SQLite_%7C_Neo4j-green.svg)](BACKENDS.md)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
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

El modelo de datos es idéntico en ambos. Mira **[BACKENDS.md](BACKENDS.md)**
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

**A) Desde Claude Desktop** — ver [Integración MCP](#integración-mcp-claude-desktop) más abajo.

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

Si has leído [BACKENDS.md](BACKENDS.md) y decides que necesitas Neo4j —
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

### Opción A: Ollama (local, lo más simple)

```bash
# 1. Instala Ollama desde https://ollama.com y arráncalo
# 2. Descarga el modelo
ollama pull nomic-embed-text

# 3. Añade a .env
echo 'EMBEDDING_PROVIDER=ollama'         >> .env
echo 'EMBEDDING_MODEL=nomic-embed-text'  >> .env
echo 'EMBEDDING_DIMENSIONS=768'          >> .env
echo 'OLLAMA_URL=http://localhost:11434' >> .env
```

Los embeddings se generan localmente — ningún dato sale de tu máquina.
El modelo `nomic-embed-text` ocupa ~274 MB y soporta un contexto de
8192 tokens.

### Opción B: Servicio compatible con OpenAI (Ollama, OpenAI, LM Studio, vLLM, llama.cpp, Jina, ...)

Las mismas variables de entorno sirven para cualquier servicio que
exponga el endpoint OpenAI `/v1/embeddings`:

```dotenv
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
EMBEDDING_DIMENSIONS=1536
OPENAI_BASE_URL=https://api.openai.com/v1   # OpenAI propio
OPENAI_API_KEY=sk-...
```

Para servidores locales apunta `OPENAI_BASE_URL` al endpoint correcto:

| Proveedor | `OPENAI_BASE_URL` |
|---|---|
| Ollama (modo OpenAI-compat) | `http://localhost:11434/v1` |
| LM Studio | `http://localhost:1234/v1` |
| vLLM | `http://localhost:8000/v1` |
| llama.cpp server | `http://localhost:8080/v1` |
| Jina | `https://api.jina.ai/v1` |

Tras activar embeddings sobre un grafo existente, ejecuta
`uv run engrama reindex` para generar los embeddings de nodos antiguos.
Los nodos nuevos se embeben automáticamente al crearse.

---

## Integración MCP (Claude Desktop)

Engrama actúa como capa de abstracción entre el agente de IA y el
backend de almacenamiento. Claude Desktop se conecta al servidor MCP de
Engrama — nunca ve credenciales, cadenas de conexión ni consultas en
crudo.

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

Ahora deberías ver las once herramientas:

| Herramienta | Descripción |
|------|-------------|
| `engrama_search` | Búsqueda híbrida (vector + fulltext + boost de grafo + temporal) |
| `engrama_remember` | Crear o actualizar un nodo (siempre MERGE) |
| `engrama_relate` | Crear una relación entre dos nodos |
| `engrama_context` | Recuperar el vecindario de un nodo |
| `engrama_sync_note` | Sincronizar una sola nota de Obsidian con el grafo |
| `engrama_sync_vault` | Escaneo completo del vault, reconciliar todas las notas |
| `engrama_ingest` | Leer contenido + extraer conocimiento automáticamente |
| `engrama_reflect` | Detección adaptativa de patrones entre entidades → Insights |
| `engrama_surface_insights` | Leer Insights pendientes para revisión |
| `engrama_approve_insight` | Aprobar o descartar un Insight |
| `engrama_write_insight_to_vault` | Escribir un Insight aprobado en Obsidian |

Consulta [`examples/claude_desktop/system-prompt.md`](examples/claude_desktop/system-prompt.md)
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
```

Para sobrescribir el backend en un comando puntual:

```bash
GRAPH_BACKEND=neo4j uv run engrama verify
```

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

Consulta [`profiles/developer.yaml`](profiles/developer.yaml) para un
perfil independiente completo, y
[`engrama/skills/onboard/references/example-profiles.md`](engrama/skills/onboard/references/example-profiles.md)
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

- [Vision](VISION.md) — por qué existe esto
- [Backends](BACKENDS.md) — guía SQLite vs Neo4j
- [Architecture](ARCHITECTURE.md) — diseño técnico y estructura de directorios
- [Graph Schema](GRAPH-SCHEMA.md) — nodos, relaciones, referencia de consultas
- [Roadmap](ROADMAP.md) — fases de desarrollo y estado
- [Changelog](CHANGELOG.md) — notas de versión
- [Contributing](CONTRIBUTING.md) — cómo contribuir
- [DDR-001](DDR-001.md) — clasificación facetada
- [DDR-002](DDR-002.md) — sincronización bidireccional vault ↔ grafo
- [DDR-003](DDR-003.md) — capa de protocolos, embeddings, búsqueda híbrida, razonamiento temporal
- [DDR-004](DDR-004.md) — almacenamiento portátil (SQLite por defecto)
- [Glosario](GLOSARIO_ES.md) — términos clave

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

Para consultas de licencias comerciales, escribe a
sinensiaitsolutions@gmail.com.

---

## Relacionado

- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — Servidor MCP genérico para Neo4j (Engrama usa su propio adaptador nativo que habla SQLite y Neo4j).
- [sqlite-vec](https://github.com/asg017/sqlite-vec) — Extensión de búsqueda vectorial para SQLite que da vida al backend Engrama por defecto.
