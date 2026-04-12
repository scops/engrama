"""
engrama/cli.py

Command-line interface for Engrama.

Commands:

    engrama init --profile developer
        Generate schema.py and init-schema.cypher from a profile YAML,
        then apply the schema to Neo4j.

    engrama init --profile base --modules hacking teaching photography
        Compose a base profile with domain modules and apply.

    engrama verify
        Check connectivity to Neo4j.

    engrama reflect
        Run cross-entity pattern detection and print results.

    engrama search <query>
        Fulltext search across the memory graph.

    engrama reindex [--batch-size 50] [--force]
        Batch re-embed all nodes and store vectors.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv

# Locate .env relative to the package root so it works regardless of cwd.
_PACKAGE_ROOT = Path(__file__).resolve().parent
_PROJECT_ROOT = _PACKAGE_ROOT.parent
load_dotenv(_PROJECT_ROOT / ".env")


def _find_project_root() -> Path:
    """Walk up from cwd looking for pyproject.toml or profiles/."""
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "pyproject.toml").exists():
            return candidate
        if (candidate / "profiles").is_dir():
            return candidate
    return cwd


def cmd_init(args: argparse.Namespace) -> int:
    """Generate schema from profile and optionally apply to Neo4j."""
    project_root = _find_project_root()
    profile_path = Path(args.profile)

    # Resolve relative profile paths against project_root/profiles/
    if not profile_path.exists():
        candidate = project_root / "profiles" / f"{args.profile}.yaml"
        if candidate.exists():
            profile_path = candidate
        else:
            print(f"Error: profile not found: {args.profile}", file=sys.stderr)
            print(f"  Looked in: {profile_path} and {candidate}", file=sys.stderr)
            return 1

    generate_script = project_root / "scripts" / "generate_from_profile.py"
    if not generate_script.exists():
        # Also check inside the package
        generate_script = _PACKAGE_ROOT / "scripts" / "generate_from_profile.py"
        if not generate_script.exists():
            print("Error: generate_from_profile.py not found.", file=sys.stderr)
            return 1

    # Step 1: Generate schema files
    modules = getattr(args, "modules", None) or []
    if modules:
        print(f"Generating schema from {profile_path.name} + modules: {', '.join(modules)}...")
    else:
        print(f"Generating schema from {profile_path.name}...")
    cmd = [sys.executable, str(generate_script), str(profile_path)]
    if modules:
        cmd.extend(["--modules"] + modules)
    if args.dry_run:
        cmd.append("--dry-run")
    cmd.extend(["--project-root", str(project_root)])

    result = subprocess.run(cmd, capture_output=not args.dry_run)
    if result.returncode != 0:
        print("Error generating schema:", file=sys.stderr)
        if result.stderr:
            print(result.stderr.decode(), file=sys.stderr)
        return 1

    if args.dry_run:
        return 0

    print("Schema files generated.")

    # Step 2: Optionally apply schema to Neo4j
    if not args.no_apply:
        cypher_path = project_root / "scripts" / "init-schema.cypher"
        if cypher_path.exists():
            print("Applying schema to Neo4j...")
            try:
                from engrama.core.client import EngramaClient
                client = EngramaClient()
                client.verify()

                # Read and execute the cypher script statement by statement.
                # Each chunk between semicolons may contain leading comment
                # lines (// ...) that must be stripped before execution.
                cypher_text = cypher_path.read_text(encoding="utf-8")
                raw_chunks = [s.strip() for s in cypher_text.split(";") if s.strip()]
                statements: list[str] = []
                for chunk in raw_chunks:
                    # Strip comment lines from within the chunk
                    lines = [
                        line for line in chunk.splitlines()
                        if line.strip() and not line.strip().startswith("//")
                    ]
                    cleaned = "\n".join(lines).strip()
                    if cleaned:
                        statements.append(cleaned)

                for stmt in statements:
                    try:
                        client.run(stmt)
                    except Exception as e:
                        # SHOW statements may fail on certain Neo4j editions
                        # — skip non-critical errors.
                        if "SHOW" not in stmt.upper():
                            print(f"  Warning: {e}", file=sys.stderr)
                # Step 3: Seed Domain nodes per module (BUG-004)
                if modules:
                    _seed_domain_nodes(client, modules)

                client.close()
                print("Schema applied successfully.")
            except Exception as e:
                print(f"Warning: could not apply schema to Neo4j: {e}", file=sys.stderr)
                print("  You can apply it manually with:", file=sys.stderr)
                print(f"  cypher-shell < {cypher_path}", file=sys.stderr)
        else:
            print("No init-schema.cypher found — skipping Neo4j apply.")

    return 0


# Seed data for each domain module
_MODULE_SEEDS: dict[str, dict] = {
    "hacking": {
        "domain": "cybersecurity",
        "domain_description": "Ethical hacking, penetration testing, and cybersecurity",
        "concepts": [
            "injection-vulnerability",
            "privilege-escalation",
            "lateral-movement",
            "enumeration",
            "post-exploitation",
        ],
    },
    "teaching": {
        "domain": "teaching",
        "domain_description": "Corporate training, course delivery, and instructional design",
        "concepts": [
            "course-design",
            "assessment",
            "hands-on-lab",
            "learning-objectives",
        ],
    },
    "photography": {
        "domain": "photography",
        "domain_description": "Nature and wildlife photography",
        "concepts": [
            "composition",
            "exposure",
            "wildlife-observation",
        ],
    },
    "ai": {
        "domain": "ai-ml",
        "domain_description": "Artificial intelligence and machine learning",
        "concepts": [
            "neural-network",
            "supervised-learning",
            "prompt-engineering",
            "fine-tuning",
        ],
    },
}


def _seed_domain_nodes(client: "EngramaClient", modules: list[str]) -> None:
    """Seed Domain (and optionally Concept) nodes for each module.

    Uses MERGE so it's safe to run repeatedly.
    """
    from engrama.core.client import EngramaClient  # noqa: F811

    for module_name in modules:
        seed = _MODULE_SEEDS.get(module_name)
        if not seed:
            continue

        # Create Domain node
        try:
            client.run(
                "MERGE (d:Domain {name: $name}) "
                "ON CREATE SET d.description = $desc, "
                "d.created_at = datetime(), d.updated_at = datetime() "
                "ON MATCH SET d.updated_at = datetime()",
                {"name": seed["domain"], "desc": seed["domain_description"]},
            )
            print(f"  Seeded Domain: {seed['domain']}")
        except Exception as e:
            print(f"  Warning: could not seed domain {seed['domain']}: {e}", file=sys.stderr)

        # Create Concept nodes and link to Domain
        for concept_name in seed.get("concepts", []):
            try:
                client.run(
                    "MERGE (c:Concept {name: $name}) "
                    "ON CREATE SET c.created_at = datetime(), c.updated_at = datetime() "
                    "ON MATCH SET c.updated_at = datetime() "
                    "WITH c "
                    "MATCH (d:Domain {name: $domain}) "
                    "MERGE (c)-[:IN_DOMAIN]->(d)",
                    {"name": concept_name, "domain": seed["domain"]},
                )
            except Exception as e:
                print(f"  Warning: could not seed concept {concept_name}: {e}", file=sys.stderr)


def cmd_verify(args: argparse.Namespace) -> int:
    """Check Neo4j connectivity."""
    try:
        from engrama.core.client import EngramaClient
        client = EngramaClient()
        client.verify()
        print(f"Connected to Neo4j at {client._uri}")
        client.close()
        return 0
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        return 1


def cmd_reflect(args: argparse.Namespace) -> int:
    """Run reflect and print insights."""
    try:
        from engrama.adapters.sdk import Engrama
        with Engrama() as eng:
            insights = eng.reflect()
            if not insights:
                print("No patterns detected.")
                return 0
            print(f"Detected {len(insights)} pattern(s):\n")
            for i, insight in enumerate(insights, 1):
                conf = int(insight.confidence * 100)
                print(f"  {i}. [{conf}%] {insight.title}")
                print(f"     {insight.body}\n")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_reindex(args: argparse.Namespace) -> int:
    """Batch re-embed all nodes and store vectors.

    Iterates over every node in the graph, generates embeddings using the
    configured provider, and stores them via the vector store.  Existing
    embeddings are overwritten.
    """
    try:
        from engrama.core.client import EngramaClient
        from engrama.backends import create_embedding_provider
        from engrama.backends.neo4j.vector import Neo4jVectorStore
        from engrama.embeddings.text import node_to_text

        embedder = create_embedding_provider()
        if getattr(embedder, "dimensions", 0) == 0:
            print(
                "Error: EMBEDDING_PROVIDER is 'none'. "
                "Set EMBEDDING_PROVIDER=ollama in .env first.",
                file=sys.stderr,
            )
            return 1

        # Health check the embedder
        if not embedder.health_check():
            print(
                "Error: embedding provider health check failed. "
                "Is Ollama running and the model pulled?",
                file=sys.stderr,
            )
            return 1

        client = EngramaClient()
        client.verify()
        vector_store = Neo4jVectorStore(
            client,
            dimensions=embedder.dimensions,
        )
        vector_store.ensure_index()

        # Fetch all nodes
        print("Fetching all nodes from graph...")
        records = client.run(
            "MATCH (n) WHERE NOT 'Embedded' IN labels(n) OR $force "
            "RETURN elementId(n) AS eid, labels(n) AS labels, "
            "properties(n) AS props",
            {"force": args.force},
        )

        if not records:
            print("No nodes to embed.")
            client.close()
            return 0

        total = len(records)
        batch_size = args.batch_size
        embedded = 0
        errors = 0

        print(f"Embedding {total} nodes (batch_size={batch_size})...")

        for i in range(0, total, batch_size):
            batch = records[i : i + batch_size]
            texts = []
            metas = []

            for r in batch:
                labels = r["labels"]
                props = dict(r["props"])
                # Pick primary label (skip system labels)
                primary = next(
                    (l for l in labels if l != "Embedded"), labels[0]
                )
                text = node_to_text(primary, props)
                texts.append(text)
                metas.append({
                    "eid": r["eid"],
                    "label": primary,
                    "name": props.get("name") or props.get("title", "?"),
                })

            try:
                embeddings = embedder.embed_batch(texts)
                items = list(zip(
                    [m["eid"] for m in metas],
                    embeddings,
                ))
                stored = vector_store.store_vectors(items)
                embedded += stored
            except Exception as e:
                print(f"  Batch {i // batch_size + 1} failed: {e}", file=sys.stderr)
                errors += len(batch)

            done = min(i + batch_size, total)
            pct = done / total * 100
            print(f"  [{done}/{total}] {pct:.0f}%% — {embedded} embedded", end="")

        print(f"\nDone: {embedded} nodes embedded, {errors} errors.")
        count = vector_store.count()
        print(f"Total vectors in index: {count}")
        client.close()
        return 0

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_search(args: argparse.Namespace) -> int:
    """Fulltext search."""
    try:
        from engrama.adapters.sdk import Engrama
        with Engrama() as eng:
            results = eng.search(args.query, limit=args.limit)
            if not results:
                print("No results found.")
                return 0
            for r in results:
                print(f"  [{r['type']}] {r['name']}  (score: {r['score']:.2f})")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def main() -> None:
    """Entry point for ``engrama`` CLI."""
    parser = argparse.ArgumentParser(
        prog="engrama",
        description="Engrama — Memory graph CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # --- init ---
    p_init = sub.add_parser("init", help="Generate schema from profile and apply to Neo4j")
    p_init.add_argument(
        "--profile", "-p", required=True,
        help="Profile name (e.g. 'developer') or path to YAML file",
    )
    p_init.add_argument(
        "--modules", "-m", nargs="*", default=[],
        help="Domain modules to compose (e.g. hacking teaching photography ai)",
    )
    p_init.add_argument(
        "--no-apply", action="store_true",
        help="Generate files only, don't apply to Neo4j",
    )
    p_init.add_argument(
        "--dry-run", action="store_true",
        help="Print generated content to stdout without writing files",
    )

    # --- verify ---
    sub.add_parser("verify", help="Check Neo4j connectivity")

    # --- reflect ---
    sub.add_parser("reflect", help="Run cross-entity pattern detection")

    # --- search ---
    p_search = sub.add_parser("search", help="Fulltext search")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument(
        "--limit", "-l", type=int, default=10,
        help="Max results (default: 10)",
    )

    # --- reindex ---
    p_reindex = sub.add_parser(
        "reindex", help="Batch re-embed all nodes and store vectors",
    )
    p_reindex.add_argument(
        "--batch-size", "-b", type=int, default=50,
        help="Batch size for embedding (default: 50)",
    )
    p_reindex.add_argument(
        "--force", "-f", action="store_true",
        help="Re-embed nodes that already have embeddings",
    )

    args = parser.parse_args()

    handlers = {
        "init": cmd_init,
        "verify": cmd_verify,
        "reflect": cmd_reflect,
        "search": cmd_search,
        "reindex": cmd_reindex,
    }

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(handler(args))


if __name__ == "__main__":
    main()


d