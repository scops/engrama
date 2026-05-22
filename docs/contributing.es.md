# Contribuir

Gracias por vuestro interés en Engrama. Las contribuciones son
bienvenidas — código, documentación, perfiles y reportes de errores.

## Configuración del entorno de desarrollo

La configuración por defecto usa el backend SQLite sin servicios
externos. Si también queréis ejecutar los tests exclusivos de Neo4j en
local, seguid la sección opcional de Neo4j más abajo.

```bash
git clone https://github.com/scops/engrama
cd engrama

# Instalar con dependencias de desarrollo (pytest, ruff, etc.)
uv sync --extra dev

# Ejecutar la suite de tests solo-SQLite (sin Docker, sin .env)
uv run pytest tests/backends/test_sqlite tests/contracts/test_graphstore_contract.py -v
```

Con eso basta para empezar a contribuir si trabajáis en código
agnóstico al backend (skills, engine, embeddings, servidor MCP, SDK,
CLI, el backend SQLite o documentación).

### Opcional: tests completos de integración con Neo4j

```bash
# 1. Instalar el extra neo4j
uv sync --extra dev --extra neo4j

# 2. Crear vuestro archivo de credenciales local
cp .env.example .env
#    Abrid .env, estableced GRAPH_BACKEND=neo4j y NEO4J_PASSWORD a una
#    contraseña robusta y única. Podéis generar una con:
#       python -c "import secrets; print(secrets.token_urlsafe(24))"

# 3. Arrancar Neo4j
docker compose up -d

# 4. Esperad ~15 segundos y ejecutad la suite completa (SQLite + Neo4j parametrizados)
uv run pytest -v
```

> **Importante:** nunca hagáis commit de vuestro `.env`. Ya está en `.gitignore`,
> pero comprobadlo antes de hacer push. El archivo `.env.example` incluye una
> contraseña de ejemplo — reemplazadla siempre en vuestra copia local.

## Ejecución de tests

```bash
# Todos los tests (los de Neo4j se saltan cuando NEO4J_PASSWORD no está definido)
uv run pytest -v

# Agnósticos al backend + solo-SQLite
uv run pytest tests/backends/ tests/contracts/ tests/test_sdk.py

# Solo las suites de contratos — demuestran que ambos backends coinciden
uv run pytest tests/contracts/
```

Los tests de integración se ejecutan contra un archivo SQLite local
(creado en `tmp_path`) o un Neo4j real en `bolt://localhost:7687`. No
hay mocks para la capa de datos — las suites de contratos en
`tests/contracts/` se parametrizan sobre ambos backends, síncrono y
asíncrono, y es precisamente esa red de seguridad la que detectó tres
bugs de deriva durante DDR-004.

Cuando añadáis un nuevo comportamiento que toque almacenamiento:

1. Implementad el nuevo método en el `*GraphStore` /
   `*VectorStore` correspondiente (síncrono y asíncrono, ambos backends).
2. Añadid un test en `tests/contracts/` que se ejecute contra ambos —
   así mantenemos la honestidad de los backends.
3. Las peculiaridades específicas de cada backend (p. ej. particularidades
   de `sqlite-vec`) van en `tests/backends/test_sqlite_*.py` junto a
   la implementación.

## Estilo de código

- Formateador: `ruff format`
- Linter: `ruff check`
- Type hints obligatorios en todas las funciones públicas
- Docstrings en todas las clases y funciones públicas

```bash
uv run ruff format .
uv run ruff check .
```

## Mensajes de commit

Seguid [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add recall skill with 2-hop traversal
fix(sqlite): align async store contract with Neo4jAsyncStore
docs: update GRAPH-SCHEMA with vector index notes
chore: bump neo4j driver to 5.28
test(contracts): add async parameterised suite
```

Cuando el cambio sea específico de un backend, hacedlo explícito en el
scope (`fix(sqlite)`, `feat(neo4j)`) para que los revisores y el
changelog puedan orientarse rápidamente.

## Enviar cambios

1. Haced fork del repositorio
2. Cread una rama: `git checkout -b feat/your-feature`
3. Escribid tests para la nueva funcionalidad (y añadidlos a la suite
   de contratos si el comportamiento es agnóstico al backend)
4. Aseguraos de que los tests pasen en ambos backends en local — como
   mínimo la ruta SQLite; la ruta Neo4j también se espera si lo tenéis
   configurado
5. Abrid un pull request con una descripción clara y una sección
   "Test plan" indicando qué habéis ejecutado

## Añadir un perfil

Los perfiles se encuentran en `profiles/`. Copiad `profiles/developer.yaml`
como plantilla. No se necesitan cambios de código — el engine lee el YAML
al inicializarse y lo aplica al backend que esté activo.

## Añadir un backend

La capa de protocolos (`engrama/core/protocols.py`) es intencionadamente
pequeña. Un nuevo backend implementa `GraphStore` (síncrono) y un
equivalente asíncrono que replica las formas de retorno ricas de
`Neo4jAsyncStore` (para que el servidor MCP siga funcionando sin
cambios). Véase `engrama/backends/sqlite/` como ejemplo práctico.
Conectadlo a la factoría en `engrama/backends/__init__.py`, proteged su
driver con un extra en `[project.optional-dependencies]` y añadidlo a
la lista de parámetros de la suite de contratos.

## Adaptador MCP

El adaptador en `engrama/adapters/mcp/` es un servidor FastMCP nativo
que habla con el async store que seleccione la factoría. Los handlers de
las herramientas MCP no contienen Cypher ni SQL — toda la lógica de
almacenamiento reside en el `*AsyncStore` correspondiente. Mantened esa
separación.
