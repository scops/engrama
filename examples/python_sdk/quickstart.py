"""Engrama Python SDK — quickstart.

Runs on the zero-dependency SQLite backend. No external services needed.

    pip install engrama        # or: uv add engrama
    python examples/python_sdk/quickstart.py

The script writes to a throwaway database under the system temp directory so
it never touches your real ``~/.engrama`` graph.
"""

from __future__ import annotations

import os
import tempfile

from engrama import Engrama


def main() -> None:
    # Point the SQLite backend at a temp file so this demo is self-contained.
    db_path = os.path.join(tempfile.gettempdir(), "engrama_quickstart.db")
    os.environ.setdefault("GRAPH_BACKEND", "sqlite")
    os.environ["ENGRAMA_DB_PATH"] = db_path

    with Engrama() as eng:
        # 1) Remember some entities (create-or-update; always idempotent).
        eng.remember(
            "Technology",
            "FastAPI",
            "High-performance async web framework for building APIs in Python.",
            tags=["python", "async", "web"],
        )
        eng.remember(
            "Project",
            "Engrama",
            "Graph-based long-term memory framework for AI agents.",
            tags=["memory", "knowledge-graph"],
        )

        # 2) Wire them into the graph with a typed relationship.
        eng.associate("Engrama", "Project", "USES", "FastAPI", "Technology")

        # 3) Search — flat fulltext, no expansion.
        print("search('async'):")
        for hit in eng.search("async"):
            print(f"  - {hit['type']}: {hit['name']}  (score={hit['score']:.3f})")

        # 4) Recall — search + expand each hit with its neighbourhood.
        print("\nrecall('FastAPI'):")
        for result in eng.recall("FastAPI", hops=1):
            neighbours = ", ".join(n.get("name", "?") for n in result.neighbours) or "(none)"
            print(f"  - {result.name}  -> neighbours: {neighbours}")

    print(f"\nDone. Throwaway graph at: {db_path}")


if __name__ == "__main__":
    main()
