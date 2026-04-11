# Contributing

Thank you for your interest in Engrama. Contributions are welcome — code, documentation, profiles, and bug reports.

## Development setup

```bash
git clone https://github.com/scops/engrama
cd engrama
uv sync --all-extras
docker compose up -d   # Neo4j required for integration tests
```

## Running tests

```bash
uv run pytest tests/ -v
```

Integration tests run against a real Neo4j on `bolt://localhost:7687`. Make sure Docker is running first.

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
