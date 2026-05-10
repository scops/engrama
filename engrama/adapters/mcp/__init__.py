"""
Engrama MCP adapter — exposes the memory graph to Claude Desktop and
any other MCP-compatible client.

Usage (stdio, default)::

    engrama-mcp                              # SQLite backend (default)
    engrama-mcp --backend neo4j              # opt in to Neo4j
    engrama-mcp --transport http --port 8000

The server reads ``GRAPH_BACKEND`` (default ``sqlite``) plus
backend-specific env vars: ``ENGRAMA_DB_PATH`` for SQLite,
``NEO4J_URI`` / ``NEO4J_USERNAME`` / ``NEO4J_PASSWORD`` for Neo4j.
``VAULT_PATH`` enables the Obsidian sync tools.
"""

import argparse
import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv

# Locate .env relative to the package root (engrama/adapters/mcp/__init__.py → ../../..)
# so it works regardless of the working directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(_PROJECT_ROOT / ".env")


def main() -> None:
    """CLI entry point registered as ``engrama-mcp`` in pyproject.toml."""
    from .server import create_engrama_mcp  # deferred to keep import lightweight

    parser = argparse.ArgumentParser(description="Engrama MCP Server")
    parser.add_argument(
        "--backend",
        default=os.getenv("GRAPH_BACKEND", "sqlite"),
        choices=["sqlite", "neo4j"],
        help="Storage backend (default: sqlite, override with $GRAPH_BACKEND)",
    )
    # SQLite-specific
    parser.add_argument(
        "--db-path",
        default=os.getenv("ENGRAMA_DB_PATH"),
        help="SQLite database path (default: ~/.engrama/engrama.db)",
    )
    # Neo4j-specific (only used when --backend=neo4j)
    parser.add_argument(
        "--db-url",
        default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j bolt URI when --backend=neo4j",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("NEO4J_USERNAME", "neo4j"),
        help="Neo4j username when --backend=neo4j",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("NEO4J_PASSWORD"),
        help="Neo4j password when --backend=neo4j (env: $NEO4J_PASSWORD)",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("NEO4J_DATABASE", "neo4j"),
        help="Neo4j database name when --backend=neo4j",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument(
        "--vault-path",
        default=os.getenv("VAULT_PATH"),
        help="Absolute path to the Obsidian vault root (default: $VAULT_PATH)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")

    args = parser.parse_args()

    # Build a config dict honoring the backend choice.
    config: dict[str, str] = {"GRAPH_BACKEND": args.backend}
    if args.backend == "neo4j":
        if not args.password:
            parser.error("--password (or $NEO4J_PASSWORD) is required when --backend=neo4j.")
        config.update(
            {
                "NEO4J_URI": args.db_url,
                "NEO4J_USERNAME": args.username,
                "NEO4J_PASSWORD": args.password,
                "NEO4J_DATABASE": args.database,
            }
        )
    elif args.backend == "sqlite" and args.db_path:
        config["ENGRAMA_DB_PATH"] = args.db_path

    mcp = create_engrama_mcp(
        backend=args.backend,
        config=config,
        vault_path=args.vault_path,
    )

    if args.transport == "http":
        asyncio.run(mcp.run_http_async(host=args.host, port=args.port))
    else:
        mcp.run()


__all__ = ["main"]
