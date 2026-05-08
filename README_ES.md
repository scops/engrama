# Engrama

> Framework de memoria a largo plazo basado en grafos para agentes de IA.

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://python.org)
[![Neo4j](https://img.shields.io/badge/neo4j-5.26_LTS-green.svg)](https://neo4j.com)
[![License](https://img.shields.io/badge/license-Apache_2.0-blue.svg)](LICENSE)
[![PyPI](https://img.shields.io/badge/pypi-engrama-orange.svg)](https://pypi.org/project/engrama)

Engrama dota a cualquier agente de IA de una memoria persistente y estructurada respaldada por un grafo de conocimiento Neo4j. En lugar de almacenes clave-valor planos o bases de datos vectoriales opacas, Engrama almacena **entidades**, **observaciones** y **relaciones** — y permite a los agentes recorrer ese grafo para razonar sobre el conocimiento acumulado.

Inspirado en el concepto de "segundo cerebro" de Karpathy, pero pensado para agentes en lugar de humanos — y con grafos en vez de wikis.

---

## ¿Por qué grafos?

| | JSON plano / KV | Base vectorial | **Engrama (Grafo)** |
|---|---|---|---|
| Consultas por relaciones | ❌ | ❌ | ✅ nativo |
| Escala a 10k+ memorias | ❌ lento | ✅ | ✅ |
| Funciona sin embeddings | ✅ | ❌ | ✅ (Ollama opcional) |
| Local-first / privado | ✅ | depende | ✅ |
| "¿Qué proyectos usan FastMCP?" | escaneo completo | aproximado | recorrido a 1 salto |

---

## Requisitos previos

Necesitas tres cosas instaladas antes de empezar. Si ya las tienes, salta a **Inicio rápido**.

| Requisito | Versión | Cómo comprobar | Guía de instalación |
|---|---|---|---|
| **Python** | 3.11 o superior | `python --version` | [python.org/downloads](https://www.python.org/downloads/) |
| **Docker Desktop** | cualquier versión reciente | `docker --version` | [docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/) |
| **uv** (gestor de paquetes Python) | cualquier versión reciente | `uv --version` | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

> **Usuarios de Windows:** después de instalar Python, asegúrate de marcar
> "Add Python to PATH". Tras instalar uv, puede que necesites reiniciar el terminal.

**Opcionales:**

- [Obsidian](https://obsidian.md/) — solo necesario para las funciones de sincronización con vault. Recomendable como base documental de apoyo.
- [Ollama](https://ollama.com/) — solo necesario para embeddings locales (búsqueda semántica). Ver [Configuración de embeddings](#configuración-de-embeddings-opcional) más abajo. Permite al agente encontrar similitudes más allá del grafo.

---

## Inicio rápido

### Paso 1: Clonar el repositorio

```bash
git clone https://github.com/scops/engrama
cd engrama
```

### Paso 2: Configurar credenciales

Copia el archivo de entorno de ejemplo y establece una contraseña:

```bash
# Linux / macOS / Git Bash
cp .env.example .env

# PowerShell (Windows)
Copy-Item .env.example .env
```

Ahora abre `.env` en cualquier editor de texto y configura **dos valores**:

1. `NEO4J_PASSWORD` — cambia `CHANGE_ME_BEFORE_FIRST_RUN` por una contraseña que elijas
2. `VAULT_PATH` — la **ruta absoluta** a la carpeta de tu vault de Obsidian
   (p. ej. `VAULT_PATH=C:\Users\tu_usuario\Documents\obsidian_vault\vault`)

`VAULT_PATH` es necesario para las herramientas de sincronización con Obsidian (`engrama_sync_note`,
`engrama_sync_vault`, `engrama_write_insight_to_vault`). Si no usas
Obsidian, puedes dejarlo vacío — las herramientas de grafo seguirán funcionando.

### Paso 3: Arrancar Neo4j

```bash
docker compose up -d
```

Espera unos 15 segundos a que arranque la base de datos. Puedes comprobar que está sana con:

```bash
docker ps
```

Deberías ver `engrama-neo4j` con estado `Up ... (healthy)`.

### Paso 4: Instalar dependencias

```bash
uv sync
```

Esto crea un entorno virtual en `.venv/` e instala todas las dependencias.

### Paso 5: Inicializar el esquema

Esto genera el esquema del grafo a partir del perfil de desarrollador y lo aplica a Neo4j:

```bash
uv run engrama init --profile developer
```

Deberías ver:

```
Generating schema from developer.yaml...
Schema files generated.
Applying schema to Neo4j...
Schema applied successfully.
```

### Paso 6: Verificar que todo funciona

```bash
uv run engrama verify
```

Salida esperada: `Connected to Neo4j at bolt://localhost:7687`

Opcionalmente, ejecuta la suite de tests:

```bash
uv run pytest tests/ -v
```

### Paso 7: Usarlo

Tienes tres formas de usar Engrama:

**A) Desde Claude Desktop** (recomendado) — ver la sección MCP más abajo.

**B) Desde Python:**

```python
from engrama import Engrama

with Engrama() as eng:
    eng.remember("Technology", "Neo4j", "Graph database for knowledge graphs")
    results = eng.search("Neo4j")
```

**C) Desde la línea de comandos:**

```bash
uv run engrama search "Neo4j"
uv run engrama reflect
```

> **Nota:** todos los comandos `engrama` de la CLI deben ir precedidos de `uv run`
> a menos que actives primero el entorno virtual con
> `.venv\Scripts\Activate.ps1` (Windows) o `source .venv/bin/activate`
> (Linux/macOS).

### Configuración de embeddings (opcional)

Engrama funciona de fábrica solo con búsqueda fulltext. Si quieres
**búsqueda por similitud semántica** (encontrar nodos conceptualmente relacionados, no solo
coincidencias por palabra clave), puedes activar embeddings locales mediante Ollama.

**1. Instala Ollama** — descárgalo desde [ollama.com](https://ollama.com/) y
asegúrate de que está corriendo (`ollama serve` o lanza la app de escritorio).

**2. Descarga el modelo de embeddings:**

```bash
ollama pull nomic-embed-text
```

**3. Activa los embeddings en `.env`:**

```dotenv
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_DIMENSIONS=768
OLLAMA_URL=http://localhost:11434
```

**4. Verifica que el modelo está disponible:**

```bash
ollama list
```

Deberías ver `nomic-embed-text:latest` en la salida.

> **Nota:** los embeddings se generan localmente — ningún dato sale de tu máquina.
> El modelo `nomic-embed-text` ocupa ~274 MB y soporta un contexto de 8192 tokens.

### ¿Y ahora qué?

El Inicio rápido te deja configurado con el perfil **developer** por defecto. Si no
eres desarrollador, o quieres un grafo que encaje con tu flujo de trabajo concreto, mira
la sección [Personalizar tu grafo](#personalizar-tu-grafo-onboarding) más abajo.

Si ya tienes notas de Obsidian y quieres poblar el grafo a partir de ellas,
conéctate desde Claude Desktop (siguiente sección) y pídele a Claude que ejecute `engrama_sync_vault`.

---

## Integración MCP (Claude Desktop)

Engrama actúa como una capa de abstracción entre el agente de IA y la base de datos.
Claude Desktop se conecta al servidor MCP de Engrama — nunca ve credenciales
de la base de datos, cadenas de conexión ni consultas en crudo.

**1. Localiza tu archivo de configuración de Claude Desktop:**

- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**2. Añade el servidor de Engrama.** Abre el archivo y añade (o fusiona en)
la sección `mcpServers`:

```json
{
  "mcpServers": {
    "engrama": {
      "command": "uv",
      "args": [
        "run", "--directory", "C:\\Proyectos\\engrama",
        "--extra", "mcp", "engrama-mcp"
      ]
    }
  }
}
```

**Importante:** cambia `C:\\Proyectos\\engrama` por la ruta real donde
clonaste el repositorio. En macOS/Linux usa barras normales (p. ej. `/home/tu_usuario/engrama`).
Aquí no hacen falta credenciales de base de datos — el servidor las lee desde `.env`.

**3. Reinicia Claude Desktop** completamente (sal y vuelve a abrir, no solo cierres la ventana).

Ahora deberías ver las herramientas de Engrama disponibles. Hay once:

| Herramienta | Descripción |
|------|-------------|
| `engrama_search` | Búsqueda híbrida (vector + fulltext + boost de grafo) |
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
para un system prompt listo para pegar que enseña a Claude a usar el grafo de memoria.

---

## SDK de Python

Usa Engrama directamente desde cualquier script de Python — sin necesidad de MCP:

```python
from engrama import Engrama

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

Todos los métodos están documentados con docstrings — usa `help(Engrama)` o el
autocompletado de tu IDE para explorarlos.

---

## Referencia de la CLI

Todos los comandos requieren el prefijo `uv run` (o un entorno virtual activado):

```bash
uv run engrama init --profile developer                        # Perfil independiente
uv run engrama init --profile base --modules hacking teaching  # Composable
uv run engrama init --profile developer --dry-run              # Vista previa sin escribir
uv run engrama verify                                          # Comprobar conectividad con Neo4j
uv run engrama search "microservices"                          # Búsqueda fulltext
uv run engrama reflect                                         # Ejecutar detección de patrones
uv run engrama reindex                                         # Re-embedding por lotes de todos los nodos
uv run engrama decay --dry-run                                 # Vista previa del decay de confianza
uv run engrama decay --rate 0.01                               # Aplicar decay suave
uv run engrama decay --rate 0.1 --min-confidence 0.05          # Agresivo + archivar
```

---

## Modos de búsqueda

Engrama soporta tres modos de búsqueda según tu configuración:

**Solo fulltext** (`EMBEDDING_PROVIDER=none`, por defecto) — coincidencia por palabras clave mediante el índice fulltext integrado de Neo4j. Funciona de fábrica, sin dependencias adicionales.

**Híbrida** (`EMBEDDING_PROVIDER=ollama`) — combina similitud semántica (búsqueda vectorial) con coincidencia por palabras clave, más un boost por topología del grafo y puntuación temporal. Encuentra nodos conceptualmente relacionados incluso sin coincidencia exacta de palabras clave. Requiere Ollama corriendo localmente con el modelo `nomic-embed-text`.

**Cómo activar la búsqueda híbrida:**
1. Establece `EMBEDDING_PROVIDER=ollama` en `.env` (ver [Configuración de embeddings](#configuración-de-embeddings-opcional))
2. Ejecuta `uv run engrama reindex` para generar embeddings de los nodos existentes
3. Los nodos nuevos reciben embeddings automáticamente al crearse

La fórmula de puntuación es: `final = α × vector + (1-α) × fulltext + β × graph_boost + γ × temporal`, donde por defecto α=0.6, β=0.15, γ=0.1. Son configurables vía las variables `HYBRID_ALPHA` y `HYBRID_GRAPH_BETA` de `.env`.

---

## Personalizar tu grafo (onboarding)

Engrama viene con un perfil `developer`, pero el esquema del grafo debería encajar con
**tu** mundo, no con una plantilla genérica. El grafo de un enfermero no se parece en nada al
de un desarrollador — y esa es precisamente la idea.

### Opción A: Usar el perfil developer integrado

Si eres desarrollador o instructor técnico, el perfil por defecto ya funciona:

```bash
uv run engrama init --profile developer
```

Esto crea nodos para Projects, Technologies, Decisions, Problems, Courses,
Concepts y Clients.

### Opción B: Que Claude construya tus módulos (recomendado)

Es el camino más fácil, y funciona para **cualquier** rol o combinación de
roles. Abre Claude Desktop con Engrama conectado y dile:

> "Quiero configurar Engrama para mi trabajo. Soy enfermera con un máster en
> biología, doy clases a estudiantes de grado y los fines de semana me encanta cocinar."

Claude te entrevistará durante unos 5 minutos — qué cosas registras día a día,
cómo se conectan en tu cabeza — y luego generará módulos de dominio personalizados
adaptados a ti: `nursing.yaml`, `biology.yaml`, `teaching.yaml`,
`cooking.yaml`. Los compone con el `base.yaml` universal y aplica
el esquema, todo en una misma conversación. No hace falta saber YAML.

### Opción C: Componer a partir de módulos existentes

Engrama trae unos cuantos módulos de ejemplo para empezar. Combina cualquiera de
ellos con el perfil **base** universal:

```bash
uv run engrama init --profile base --modules hacking teaching photography ai
```

Esto fusiona `profiles/base.yaml` (Project, Concept, Decision, Problem,
Technology, Person) con nodos y relaciones específicos de dominio de
`profiles/modules/`.

**Módulos de ejemplo incluidos:**

| Módulo | Añade |
|---|---|
| `hacking` | Target, Vulnerability, Technique, Tool, CTF |
| `teaching` | Course, Client, Exercise, Material |
| `photography` | Photo, Location, Species, Gear |
| `ai` | Model, Dataset, Experiment, Pipeline |

Estos cuatro son **ejemplos, no una lista cerrada**. La verdadera potencia es que cualquiera
puede crear un módulo para cualquier dominio — ver Opción D más abajo.

### Opción D: Escribir tu propio módulo

Un módulo es solo un pequeño archivo YAML en `profiles/modules/`. Aquí tienes un ejemplo
completo para alguien que registra recetas de cocina:

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
  - {type: USES,      from: Recipe,           to: Ingredient}
  - {type: APPLIES,   from: Recipe,           to: CookingTechnique}
  - {type: RELATED,   from: Ingredient,       to: Concept}        # 'Concept' viene de base.yaml
  - {type: DOCUMENTS, from: Recipe,           to: Project}        # 'Project' viene de base.yaml
```

Guárdalo como `profiles/modules/cooking.yaml`, y luego compón:

```bash
uv run engrama init --profile base --modules cooking teaching
```

**Reglas para los módulos:**

- Los nodos usan etiquetas en PascalCase y `name` o `title` como clave de merge
- Las relaciones pueden referenciar cualquier etiqueta de `base.yaml` (Project, Concept, Decision, Problem, Technology, Person) sin necesidad de redefinirlas
- Si dos módulos definen la misma etiqueta, las propiedades se fusionan automáticamente
- Los tipos de relación deberían ser verbos (USES, TREATS, COVERS), no sustantivos

Consulta [`profiles/developer.yaml`](profiles/developer.yaml) para ver un perfil
independiente completo, y
[`engrama/skills/onboard/references/example-profiles.md`](engrama/skills/onboard/references/example-profiles.md)
para perfiles trabajados en dominios muy distintos (enfermería, abogacía, PM,
creativos freelance).

### Consejos para buenos perfiles

- **3 a 5 tipos de nodo por módulo** es el punto óptimo. La base ya te da
  6. Un usuario multi-rol típico acaba con 12–18 en total, lo cual está bien.
- Usa `title` como clave de merge para cosas con forma de frase (decisiones, problemas, protocolos). Usa `name` para todo lo demás.
- Incluye siempre `status` en nodos con ciclo de vida — el skill de reflect lo usa para distinguir elementos abiertos vs resueltos.
- Ante la duda, deja que tu agente (Claude, Codex, Gemini, etc.) genere el módulo por ti (Opción B).

---

## Documentación

- [Vision](VISION.md) — por qué existe esto
- [Architecture](ARCHITECTURE.md) — diseño técnico y estructura de directorios
- [Graph Schema](GRAPH-SCHEMA.md) — nodos, relaciones, referencia de Cypher
- [Roadmap](ROADMAP.md) — fases de desarrollo y estado
- [Contributing](CONTRIBUTING.md) — cómo contribuir

---

## Licencia

Engrama está licenciado bajo Apache License 2.0.
Copyright 2026 Sinensia IT Solutions

Licenciado bajo Apache License, Version 2.0 (la "Licencia");
no puedes usar este archivo salvo en cumplimiento de la Licencia.
Puedes obtener una copia de la Licencia en:

    http://www.apache.org/licenses/LICENSE-2.0

Eres libre de usar, modificar y distribuir Engrama tanto en proyectos personales como comerciales. La licencia Apache 2.0 incluye una concesión explícita de patentes, dándote tranquilidad para adoptar Engrama en entornos empresariales sin preocupaciones de propiedad intelectual.

### Contribuciones

Al enviar un pull request o contribución, aceptas que tu contribución se licencia bajo los mismos términos de Apache 2.0. Usamos un Developer Certificate of Origin (DCO) — firma tus commits con `git commit -s` para certificar que tienes derecho a enviar el código bajo esta licencia.

### Extensiones comerciales

Determinadas funcionalidades premium (como hosting gestionado, colaboración multi-tenant y analítica avanzada) podrán ofrecerse bajo una licencia comercial separada. El motor principal, las herramientas MCP y toda la funcionalidad de cara a la comunidad permanecen totalmente open source bajo Apache 2.0.

Para consultas de licencias comerciales, escribe a sinensiaitsolutions@gmail.com.

---

## Relacionado

- [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j) — Servidor MCP para Neo4j (Engrama usa su propio adaptador nativo en su lugar)
