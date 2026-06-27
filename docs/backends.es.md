# Elegir un backend

> Engrama funciona sobre dos backends de almacenamiento intercambiables.
> Esta guía te indica cuál elegir y por qué.

Engrama 0.9 introduce **almacenamiento portable**: SQLite + la extensión
`sqlite-vec`, todo contenido en un único archivo en
`~/.engrama/engrama.db`. El backend original de Neo4j sigue siendo
completamente compatible — ahora es un extra opcional.

Si no sabes cuál elegir, **empieza con SQLite**. Puedes cambiar más
adelante con una sola variable de entorno.

---

## De un vistazo

| | **SQLite (por defecto)** | **Neo4j (opcional)** |
|---|---|---|
| Instalación | `git clone` + `uv sync` | `git clone` + `uv sync --extra neo4j` + Docker |
| Servicios externos | ninguno | Neo4j 5.26 LTS en Docker |
| Primera ejecución | segundos | ~15s para que arranque la base de datos |
| Huella en disco | un único archivo `.db` | directorio de datos de Neo4j + imagen Docker (~500 MB) |
| Portabilidad | copia el archivo `.db` donde quieras | dump/restore con `neo4j-admin` |
| Concurrencia | un escritor, muchos lectores (WAL) | muchos lectores y escritores |
| Búsqueda vectorial | `sqlite-vec` (fuerza bruta, suficiente hasta ~100k vectores) | índice vectorial de Neo4j (HNSW, escala más) |
| Escritura multiproceso | no recomendado | sí |
| Acceso remoto / cloud | solo archivo local | bolt://host:7687 |
| Lenguaje de consulta Cypher | no disponible | sí |
| Perfil de memoria | mínimo (un solo proceso SQLite) | heap JVM (~1 GB mínimo) |
| Funciona sin Docker | ✅ | ❌ |

El modelo de datos — etiquetas, relaciones, clasificación facetada — es
**idéntico** en ambos backends. Todo lo que almacenes en SQLite puedes
moverlo después a Neo4j (y viceversa) sin reestructurar tu grafo.

---

## Árbol de decisión

```
¿Ejecutas Engrama en un solo portátil / VM / contenedor, para un único usuario?
├─ Sí → SQLite. Listo.
│
└─ No → ¿Varios procesos necesitan escribir a la vez?
        ├─ Sí → Neo4j.
        │
        └─ No → ¿Necesitas >100k embeddings, o prevés alcanzarlos en <12 meses?
                ├─ Sí → Neo4j.
                │
                └─ No → ¿Necesitas analíticas Cypher ad-hoc o un visor de grafos?
                        ├─ Sí → Neo4j.
                        │
                        └─ No → SQLite.
```

En la práctica, la primera rama cubre ~90 % de los usuarios.

---

## Cuándo elegir SQLite

- **Estás empezando.** Cero fricción de instalación, sin Docker, sin JVM.
  `pip install engrama && engrama init` y ya puedes consultar el grafo.
- **Configuraciones de un solo agente.** Un Claude Desktop, un cliente
  MCP, un script de larga ejecución. SQLite lo gestiona perfectamente.
- **Ejecuciones en CI y tests.** Sin servicios externos que levantar —
  `pytest` funciona directamente en un checkout limpio.
- **Distribución embebida.** ¿Distribuyes una herramienta que incluye
  Engrama como librería? Tus usuarios obtienen una capa de memoria
  funcional sin necesidad de Docker.
- **Hosts en el edge / con recursos limitados.** Sin JVM significa que
  Engrama funciona cómodamente en una Raspberry Pi o una VM de 512 MB.
- **Cuadernos de investigación portables.** Envía a un compañero tu
  archivo `.db` y tendrá tu grafo completo — sin migraciones de esquema.

---

## Cuándo elegir Neo4j

- **Entornos de producción multiusuario.** Múltiples agentes (o humanos
  vía Bloom / Browser) escribiendo concurrentemente en el mismo grafo.
- **Búsqueda vectorial a gran escala.** `sqlite-vec` realiza similitud
  por fuerza bruta; suficiente hasta ~100k vectores, pero el índice HNSW
  de Neo4j rendirá mejor a partir de ahí.
- **Ya tenéis pipelines de Cypher.** Si tu equipo escribe Cypher para
  analíticas, lógica de negocio o migraciones, aprovechad esa inversión.
- **Necesitáis Bloom / Neo4j Browser para exploración visual.** SQLite
  no tiene una interfaz nativa equivalente.
- **Clúster / alta disponibilidad** (Neo4j Enterprise). SQLite es una
  base de datos de archivo único — sin replicación.

---

## Cambiar de uno a otro

Basta con cambiar una variable de entorno. El modelo de datos es
idéntico, así que cualquier herramienta o skill que funcione en un
backend funciona en el otro.

### De SQLite a Neo4j

```bash
# 1. Instalar el extra y arrancar Neo4j
uv sync --extra neo4j
docker compose up -d                 # usa docker-compose.yml del repo

# 2. Indicar a Engrama que lo use
echo 'GRAPH_BACKEND=neo4j' >> .env
echo 'NEO4J_PASSWORD=...' >> .env

# 3. (Opcional) recrear el índice vectorial para búsqueda híbrida
uv run engrama init --profile developer
uv run engrama reindex
```

Si `GRAPH_BACKEND=neo4j` está definido pero falta el extra de Python,
tanto la CLI como el servidor MCP fallarán con un mensaje explícito de
instalación en lugar de un error genérico de importación o arranque.

Para migrar datos, la forma más sencilla a día de hoy es: configurar un
script SDK temporal que lea de SQLite y escriba en Neo4j mediante dos
contextos `Engrama`. Una herramienta de exportación de primera clase
está en la hoja de ruta.

### De Neo4j a SQLite

```bash
# 1. Indicar a Engrama que use SQLite
echo 'GRAPH_BACKEND=sqlite' >> .env
echo 'ENGRAMA_DB_PATH=~/.engrama/engrama.db' >> .env  # opcional, es el valor por defecto

# 2. Sincronizar desde tu vault de Obsidian — el vault es portable por diseño (DDR-002)
uv run engrama-mcp     # o usa Claude Desktop
# luego: engrama_sync_vault
```

Dado que las relaciones se persisten en el frontmatter del vault
(DDR-002), el vault de Obsidian es en sí mismo una copia de seguridad
portable del grafo completo. Apuntar una instalación SQLite nueva al
mismo vault y ejecutar `engrama_sync_vault` reconstruye el grafo desde
cero.

---

## ¿Cómo funciona internamente?

Ambos backends implementan los mismos protocolos `GraphStore`,
`VectorStore` y `EmbeddingProvider` (`engrama/core/protocols.py`). Una
factoría única en `engrama/backends/__init__.py` lee `GRAPH_BACKEND` del
entorno y devuelve la implementación correspondiente.

Los skills, el servidor MCP, la CLI y el SDK de Python están escritos
contra los protocolos — no saben qué backend hay debajo. Por eso el
cambio se reduce a una sola variable.

Consulta [architecture.md](architecture.md#capa-de-protocolo-y-backends)
para el diagrama completo de capas, y [DDR-004](ddr-004.md) para la
justificación de diseño del backend portable.

---

## Preguntas frecuentes

**¿Puedo ejecutar ambos backends a la vez?**
Puedes, pero un único proceso de Engrama se vincula a uno solo.
Diferentes procesos pueden apuntar a backends distintos — útil para
pruebas o migraciones.

**¿SQLite soporta todas las características de Neo4j?**
Para la API pública de Engrama (las 14 herramientas MCP, el SDK, la
CLI), sí — son funcionalmente equivalentes y se prueban con la misma
suite de tests de contrato parametrizados. Lo único que SQLite no puede
hacer es ejecutar patrones Cypher en crudo; en su lugar usa consultas
SQL pre-traducidas. Si en el futuro una funcionalidad requiere Cypher
ad-hoc, el backend de Neo4j la recibirá primero.

**¿Y los embeddings?**
Ambos backends soportan la pila completa de búsqueda híbrida (vectorial
+ texto completo + impulso por grafo + temporal). SQLite almacena los
vectores mediante `sqlite-vec` en el mismo archivo `.db`; Neo4j usa su
índice vectorial nativo.

Si el backend del grafo está sano pero el servicio de embeddings está
caído, Engrama degrada a búsqueda de texto completo e informa de ello
explícitamente. Esto permite distinguir entre «Neo4j está mal
configurado» y «Ollama / el servicio de embeddings no está disponible».

**¿El script de migración de esquema (`engrama init`) funciona en SQLite?**
El esquema de SQLite reside en `engrama/backends/sqlite/schema.sql` y se
aplica automáticamente cuando se crea el archivo de base de datos.
`engrama init` en un backend SQLite es un no-op para el esquema (las
restricciones Cypher se ignoran), pero sigue sembrando los nodos de
dominio de tu perfil.

**¿Dónde están mis datos en cada backend?**

- **SQLite:** `~/.engrama/engrama.db` por defecto, o donde apunte
  `ENGRAMA_DB_PATH`. Un solo archivo. Haz copia de seguridad con `cp`.
- **Neo4j:** dentro del volumen Docker `engrama_neo4j_data` (o donde
  hayas montado el directorio de datos de Neo4j). Haz copia de seguridad
  con `neo4j-admin database dump`.
