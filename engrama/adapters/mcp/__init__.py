"""
Engrama MCP adapter — exposes the memory graph to Claude Desktop and
any other MCP-compatible client.

Usage (stdio, default)::

    engrama-mcp

Usage (HTTP)::

    engrama-mcp --transport http --host 127.0.0.1 --port 8000

The server reads ``NEO4J_URI``, ``NEO4J_USERNAME``, and ``NEO4J_PASSWORD``
from environment variables (or a ``.env`` file).
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
        "--db-url",
        default=os.getenv("NEO4J_URI", "bolt://localhost:7687"),
        help="Neo4j bolt URI (default: $NEO4J_URI or bolt://localhost:7687)",
    )
    parser.add_argument(
        "--username",
        default=os.getenv("NEO4J_USERNAME", "neo4j"),
        help="Neo4j username (default: $NEO4J_USERNAME or neo4j)",
    )
    parser.add_argument(
        "--password",
        default=os.getenv("NEO4J_PASSWORD"),
        help="Neo4j password (default: $NEO4J_PASSWORD, required)",
    )
    parser.add_argument(
        "--database",
        default=os.getenv("NEO4J_DATABASE", "neo4j"),
        help="Neo4j database name (default: neo4j)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="HTTP host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="HTTP port (default: 8000)")

    args = parser.parse_args()

    if not args.password:
        parser.error(
            "Neo4j password is required. Set NEO4J_PASSWORD in the environment, "
            "create a .env file, or pass --password."
        )

    mcp = create_engrama_mcp(
        db_url=args.db_url,
        username=args.username,
        password=args.password,
        database=args.database,
    )

    if args.transport == "http":
        asyncio.run(mcp.run_http_async(host=args.host, port=args.port))
    else:
        mcp.run()


__all__ = ["main"]
