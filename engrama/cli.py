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
                from engrama.backends.neo4j.backend import Neo4jGraphStore
                from engrama.core.client import EngramaClient
                client = EngramaClient()
                client.verify()
                store = Neo4jGraphStore(client)

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

                failures = store.apply_schema_statements(statements)
                for stmt, exc in failures:
                    # SHOW statements may fail on certain Neo4j editions
                    # — skip non-critical errors.
                    if "SHOW" not in stmt.upper():
                        print(f"  Warning: {exc}", file=sys.stderr)
                # Step 3: Seed Domain nodes per module (BUG-004)
                if modules:
                    _seed_domain_nodes(store, modules)

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


def _seed_domain_nodes(store: "Neo4jGraphStore", modules: list[str]) -> None:
    """Seed Domain (and optionally Concept) nodes for each module.

    Uses MERGE so it's safe to run repeatedly.
    """
    for module_name in modules:
        seed = _MODULE_SEEDS.get(module_name)
        if not seed:
            continue

        # Create Domain node
        try:
            store.seed_domain(seed["domain"], seed["domain_description"])
            print(f"  Seeded Domain: {seed['domain']}")
        except Exception as e:
            print(f"  Warning: could not seed domain {seed['domain']}: {e}", file=sys.stderr)

        # Create Concept nodes and link to Domain
        for concept_name in seed.get("concepts", []):
            try:
                store.seed_concept_in_domain(concept_name, seed["domain"])
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
        from engrama.backends import create_embedding_provider
        from engrama.backends.neo4j.backend import Neo4jGraphStore
        from engrama.backends.neo4j.vector import Neo4jVectorStore
        from engrama.core.client import EngramaClient
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
        store = Neo4jGraphStore(client)
        vector_store = Neo4jVectorStore(
            client,
            dimensions=embedder.dimensions,
        )
        vector_store.ensure_index()

        # Fetch all nodes
        print("Fetching all nodes from graph...")
        records = store.list_nodes_for_embedding(force=args.force)

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


def cmd_decay(args: argparse.Namespace) -> int:
    """Apply confidence decay to all nodes (DDR-003 Phase D).

    Uses the sync backend's ``decay_scores`` for writes and provides
    a detailed sample table when ``--dry-run`` is set.
    """
    try:
        from engrama.adapters.sdk import Engrama
        with Engrama() as eng:
            rate = args.rate
            min_conf = args.min_confidence
            max_age = args.max_age
            label = args.label

            if args.dry_run:
                print("[DRY RUN] No changes will be written.\n")
                print(f"  rate={rate}, min_confidence={min_conf}, "
                      f"max_age_days={max_age}, label={label or 'all'}\n")
                # Show a preview of what would be decayed
                try:
                    preview = eng._store.query_at_date(
                        "2099-12-31", label=label, limit=20,
                    )
                except Exception:
                    preview = []
                if not preview:
                    print("  No nodes with confidence data found.")
                    return 0
                import math
                from datetime import datetime, timezone
                print(f"  {'Name':<30} {'Label':<12} {'Conf':>6} {'→ New':>6} {'Days':>5}")
                print(f"  {'─' * 30} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 5}")
                now = datetime.now(timezone.utc)
                for r in preview:
                    old_c = r.get("confidence") or 1.0
                    vf = r.get("valid_from")
                    if vf and hasattr(vf, "to_native"):
                        vf = vf.to_native()
                    days = 0.0
                    if vf and hasattr(vf, "timestamp"):
                        days = max(0.0, (now - vf.replace(tzinfo=timezone.utc)).total_seconds() / 86400)
                    new_c = old_c * math.exp(-rate * days)
                    name = (r.get("name") or "?")[:30]
                    print(f"  {name:<30} {r.get('label', '?'):<12} {old_c:>6.3f} {new_c:>6.3f} {days:>5.0f}")
                return 0

            result = eng.decay_scores(
                rate=rate,
                min_confidence=min_conf,
                max_age_days=max_age,
                label=label,
            )
            print(f"Decay applied: {result['decayed']} nodes updated, "
                  f"{result['archived']} nodes archived.")
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

    # --- decay ---
    p_decay = sub.add_parser(
        "decay", help="Apply confidence decay to nodes (DDR-003 Phase D)",
    )
    p_decay.add_argument(
        "--rate", "-r", type=float, default=0.01,
        help="Exponential decay rate (default: 0.01)",
    )
    p_decay.add_argument(
        "--min-confidence", "-c", type=float, default=0.0,
        help="Archive nodes below this confidence after decay (default: 0, no archive)",
    )
    p_decay.add_argument(
        "--max-age", "-a", type=int, default=0,
        help="Archive nodes older than N days (default: 0, no age limit)",
    )
    p_decay.add_argument(
        "--label", type=str, default=None,
        help="Restrict to a specific label (default: all labels)",
    )
    p_decay.add_argument(
        "--dry-run", action="store_true",
        help="Show what would happen without making changes",
    )

    args = parser.parse_args()

    handlers = {
        "init": cmd_init,
        "verify": cmd_verify,
        "reflect": cmd_reflect,
        "search": cmd_search,
        "reindex": cmd_reindex,
        "decay": cmd_decay,
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
