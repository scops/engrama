# Contributing

Thank you for your interest in Engrama. Contributions are welcome —
code, documentation, profiles, and bug reports.

## Development setup

The default setup runs on the SQLite backend with zero external
services. If you also want to run the Neo4j-only tests locally, follow
the optional Neo4j section below.

```bash
git clone https://github.com/scops/engrama
cd engrama

# Install with dev dependencies (pytest, ruff, etc.)
uv sync --extra dev

# Run the SQLite-only test suite (no Docker, no .env required)
uv run pytest tests/backends/test_sqlite tests/contracts/test_graphstore_contract.py -v
```

That's enough to start contributing if you're working on backend-
agnostic code (skills, engine, embeddings, MCP server, SDK, CLI, the
SQLite backend, or docs).

### Optional: full Neo4j integration tests

```bash
# 1. Install the neo4j extra
uv sync --extra dev --extra neo4j

# 2. Create your local credentials file
cp .env.example .env
#    Open .env, set GRAPH_BACKEND=neo4j and NEO4J_PASSWORD to a strong,
#    unique password. Generate one with:
#       python -c "import secrets; print(secrets.token_urlsafe(24))"

# 3. Start Neo4j
docker compose up -d

# 4. Wait ~15 seconds, then run the full suite (SQLite + Neo4j parametrised)
uv run pytest -v
```

> **Important:** never commit your `.env`. It is already in `.gitignore`,
> but double-check before pushing. The `.env.example` ships with a sample
> password — always replace it in your local copy.

## Running tests

```bash
# All tests (Neo4j tests skip when NEO4J_PASSWORD is unset)
uv run pytest -v

# Backend-agnostic + SQLite-only
uv run pytest tests/backends/ tests/contracts/ tests/test_sdk.py

# Just the contract suites — proves both backends agree
uv run pytest tests/contracts/
```

Integration tests run against either a local SQLite file (created in
`tmp_path`) or a real Neo4j on `bolt://localhost:7687`. There are no
mocks for the data layer — the contract suites in
`tests/contracts/` parameterise over both backends, sync and async,
which is the safety net that caught three drift bugs during DDR-004.

When you add a new behaviour that touches storage:

1. Implement the new method on the relevant `*GraphStore` /
   `*VectorStore` (sync and async, both backends).
2. Add a test in `tests/contracts/` that runs against both — that's
   how we keep the backends honest.
3. Backend-specific quirks (e.g. `sqlite-vec` peculiarities) live in
   `tests/backends/test_sqlite_*.py` next to the implementation.

## Code style

- Formatter: `ruff format`
- Linter: `ruff check`
- Type hints required on all public functions
- Docstrings on all public classes and functions

```bash
uv run ruff format .
uv run ruff check .
```

## Commit messages

Follow [Conventional Commits](https://www.conventionalcommits.org/):

```
feat: add recall skill with 2-hop traversal
fix(sqlite): align async store contract with Neo4jAsyncStore
docs: update GRAPH-SCHEMA with vector index notes
chore: bump neo4j driver to 5.28
test(contracts): add async parameterised suite
```

When the change is backend-specific, make it explicit in the scope
(`fix(sqlite)`, `feat(neo4j)`) so reviewers and the changelog can route
quickly.

## Submitting changes

1. Fork the repo
2. Create a branch: `git checkout -b feat/your-feature`
3. Write tests for new functionality (and add them to the contract
   suite if the behaviour is backend-agnostic)
4. Make sure tests pass on both backends locally — at minimum the
   SQLite path; the Neo4j path is also expected if you have it set up
5. Open a pull request with a clear description and a "Test plan"
   section listing what you ran

## Adding a profile

Profiles live in `profiles/`. Copy `profiles/developer.yaml` as a
template. No code changes needed — the engine reads YAML at
initialisation and applies it to whichever backend is active.

## Adding a backend

The protocol layer (`engrama/core/protocols.py`) is intentionally small.
A new backend implements `GraphStore` (sync) and an async equivalent
that mirrors `Neo4jAsyncStore`'s rich return shapes (so the MCP server
keeps working unchanged). See `engrama/backends/sqlite/` for a worked
example. Plug it into the factory in `engrama/backends/__init__.py`,
guard its driver with a `[project.optional-dependencies]` extra, and
add it to the contract suite parameter list.

## MCP adapter

The adapter in `engrama/adapters/mcp/` is a native FastMCP server that
talks to whichever async store the factory selects. The MCP tool
handlers contain zero Cypher / SQL — all storage logic is in the matching
`*AsyncStore`. Keep it that way.
