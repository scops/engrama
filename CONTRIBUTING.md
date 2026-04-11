# Contributing

Thank you for your interest in Engrama. Contributions are welcome — code, documentation, profiles, and bug reports.

## Development setup

```bash
git clone https://github.com/scops/engrama
cd engrama

# 1. Create your local credentials file
cp .env.example .env
#    Open .env and set NEO4J_PASSWORD to a strong, unique password.
#    Generate one with: python -c "import secrets; print(secrets.token_urlsafe(24))"

# 2. Start Neo4j and install dependencies
docker compose up -d
uv sync --all-extras

# 3. Wait ~15 seconds for Neo4j to boot, then initialise the schema
#    (see README.md for the PowerShell / bash command)
```

> **Important:** Never commit your `.env` file. It is already in `.gitignore`,
> but double-check before pushing. The `.env.example` ships with a sample
> password — always replace it in your local copy.

## Running tests

```bash
uv run python -m pytest tests/ -v
```

Integration tests run against a real Neo4j on `bolt://localhost:7687`.
Credentials are read from your `.env` file — tests will fail with a clear
error if `NEO4J_PASSWORD` is not set. Make sure Docker is running first.

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
fix: prevent duplicate nodes when MERGE fails on datetime
docs: update GRAPH-SCHEMA with vector index notes
chore: bump neo4j driver to 5.28
```

## Submitting changes

1. Fork the repo
2. Create a branch: `git checkout -b feat/your-feature`
3. Write tests for new functionality
4. Make sure all tests pass
5. Open a pull request with a clear description

## Adding a profile

Profiles live in `profiles/`. Copy `profiles/developer.yaml` as a template.
No code changes needed — the engine reads YAML at initialisation.

## MCP adapter

The adapter in `engrama/adapters/mcp/` is based on [scops/mcp-neo4j](https://github.com/scops/mcp-neo4j),
a fork of [neo4j-contrib/mcp-neo4j](https://github.com/neo4j-contrib/mcp-neo4j).
Improvements should be contributed upstream when appropriate.
