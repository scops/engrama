"""
engrama/cli.py

Command-line interface for Engrama.

The active backend is selected by the ``GRAPH_BACKEND`` env var
(``sqlite`` by default, opt in to ``neo4j``). All commands work
against whichever backend is configured.

Commands:

    engrama init --profile developer
        Generate schema.py from a profile YAML, then seed Domain /
        Concept nodes for any requested modules. With Neo4j, also
        applies init-schema.cypher.

    engrama init --profile base --modules hacking teaching photography
        Compose a base profile with domain modules and apply.

    engrama verify
        Check the configured backend is reachable.

    engrama reflect
        Run cross-entity pattern detection and print results.

    engrama search <query>
        Fulltext search across the memory graph.

    engrama reindex [--batch-size 50] [--force]
        Batch re-embed all nodes and store vectors.

    engrama export <file> [--no-vectors]
        Dump graph + vectors to an NDJSON file (backend-agnostic).

    engrama import <file> [--purge]
        Restore an NDJSON dump into the active backend.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import UTC
from pathlib import Path
from typing import Any

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
    """Generate schema from profile and apply it to the configured backend.

    SQLite picks up its schema automatically at connection time, so for
    that backend ``init`` only seeds Domain/Concept nodes for the
    requested modules. Neo4j gets the full ``init-schema.cypher`` apply.
    """
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

    if args.no_apply:
        return 0

    # Step 2: apply schema + seed via the configured backend.
    backend = os.getenv("GRAPH_BACKEND", "sqlite")
    try:
        from engrama.backends import create_stores

        store, _ = create_stores()
    except Exception as e:
        print(f"Warning: could not open {backend} backend: {e}", file=sys.stderr)
        return 0

    try:
        if backend == "neo4j":
            cypher_path = project_root / "scripts" / "init-schema.cypher"
            if cypher_path.exists():
                print("Applying schema to Neo4j...")
                cypher_text = cypher_path.read_text(encoding="utf-8")
                raw_chunks = [s.strip() for s in cypher_text.split(";") if s.strip()]
                statements: list[str] = []
                for chunk in raw_chunks:
                    lines = [
                        line
                        for line in chunk.splitlines()
                        if line.strip() and not line.strip().startswith("//")
                    ]
                    cleaned = "\n".join(lines).strip()
                    if cleaned:
                        statements.append(cleaned)
                failures = store.apply_schema_statements(statements)
                for stmt, exc in failures:
                    if "SHOW" not in stmt.upper():
                        print(f"  Warning: {exc}", file=sys.stderr)
            else:
                print("No init-schema.cypher found — skipping Neo4j apply.")
        else:
            print(f"Backend {backend!r}: schema auto-applied at connection time.")

        if modules:
            _seed_domain_nodes(store, modules)
        print("Init complete.")
    except Exception as e:
        print(f"Warning: init step failed: {e}", file=sys.stderr)
    finally:
        if hasattr(store, "close"):
            store.close()

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


def _seed_domain_nodes(store: Any, modules: list[str]) -> None:
    """Seed Domain (and optionally Concept) nodes for each module.

    Backend-agnostic: works against any GraphStore that exposes the
    ``seed_domain`` / ``seed_concept_in_domain`` helpers.
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
    """Check the configured backend is reachable."""
    try:
        from engrama.backends import create_embedding_provider, create_stores

        store, _ = create_stores()
        health = store.health_check()
        backend = os.getenv("GRAPH_BACKEND", "sqlite")
        print(f"Connected to {backend}: {health}")
        embedder = create_embedding_provider()
        if getattr(embedder, "dimensions", 0) > 0:
            if embedder.health_check():
                print(
                    "Embeddings: ok "
                    f"(provider={os.getenv('EMBEDDING_PROVIDER', 'none')}, "
                    f"model={getattr(embedder, 'model', 'n/a')})"
                )
            else:
                print(
                    "Embeddings: degraded "
                    f"(provider={os.getenv('EMBEDDING_PROVIDER', 'none')}, "
                    "endpoint unreachable or model unavailable)",
                    file=sys.stderr,
                )
        if hasattr(store, "close"):
            store.close()
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

    Iterates over every node in the graph, generates embeddings using
    the configured provider, and writes them via the configured vector
    store. Existing embeddings are overwritten.
    """
    try:
        from engrama.backends import create_embedding_provider, create_stores
        from engrama.embeddings.text import node_to_text

        embedder = create_embedding_provider()
        if getattr(embedder, "dimensions", 0) == 0:
            print(
                "Error: EMBEDDING_PROVIDER is 'none'. "
                "Configure an embedder (e.g. EMBEDDING_PROVIDER=ollama) first.",
                file=sys.stderr,
            )
            return 1

        # Health check the embedder
        if not embedder.health_check():
            print(
                "Error: embedding provider health check failed. "
                "Is the configured endpoint reachable?",
                file=sys.stderr,
            )
            return 1

        # Push embedder dims through the factory so the vector store
        # sizes itself correctly (sqlite-vec needs dims at create time).
        config = {"EMBEDDING_DIMENSIONS": str(embedder.dimensions)}
        store, vector_store = create_stores(config)
        # Fetch all nodes
        print("Fetching all nodes from graph...")
        records = store.list_nodes_for_embedding(force=args.force)

        if not records:
            print("No nodes to embed.")
            if hasattr(store, "close"):
                store.close()
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
                primary = next((lbl for lbl in labels if lbl != "Embedded"), labels[0])
                text = node_to_text(primary, props)
                texts.append(text)
                metas.append(
                    {
                        "eid": r["eid"],
                        "label": primary,
                        "name": props.get("name") or props.get("title", "?"),
                    }
                )

            try:
                embeddings = embedder.embed_batch(texts)
                items = list(
                    zip(
                        [m["eid"] for m in metas],
                        embeddings,
                    )
                )
                stored = vector_store.store_vectors(items)
                embedded += stored
            except Exception as e:
                print(f"  Batch {i // batch_size + 1} failed: {e}", file=sys.stderr)
                errors += len(batch)

            done = min(i + batch_size, total)
            pct = done / total * 100
            print(f"  [{done}/{total}] {pct:.0f}% — {embedded} embedded", end="")

        print(f"\nDone: {embedded} nodes embedded, {errors} errors.")
        count = vector_store.count()
        print(f"Total vectors in index: {count}")
        if hasattr(store, "close"):
            store.close()
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
                print(
                    f"  rate={rate}, min_confidence={min_conf}, "
                    f"max_age_days={max_age}, label={label or 'all'}\n"
                )
                # Show a preview of what would be decayed
                try:
                    preview = eng._store.query_at_date(
                        "2099-12-31",
                        label=label,
                        limit=20,
                    )
                except Exception:
                    preview = []
                if not preview:
                    print("  No nodes with confidence data found.")
                    return 0
                import math
                from datetime import datetime

                print(f"  {'Name':<30} {'Label':<12} {'Conf':>6} {'→ New':>6} {'Days':>5}")
                print(f"  {'─' * 30} {'─' * 12} {'─' * 6} {'─' * 6} {'─' * 5}")
                now = datetime.now(UTC)
                for r in preview:
                    old_c = r.get("confidence") or 1.0
                    vf = r.get("valid_from")
                    if vf and hasattr(vf, "to_native"):
                        vf = vf.to_native()
                    days = 0.0
                    if vf and hasattr(vf, "timestamp"):
                        days = max(0.0, (now - vf.replace(tzinfo=UTC)).total_seconds() / 86400)
                    new_c = old_c * math.exp(-rate * days)
                    name = (r.get("name") or "?")[:30]
                    label = r.get("label", "?")
                    print(f"  {name:<30} {label:<12} {old_c:>6.3f} {new_c:>6.3f} {days:>5.0f}")
                return 0

            result = eng.decay_scores(
                rate=rate,
                min_confidence=min_conf,
                max_age_days=max_age,
                label=label,
            )
            print(
                f"Decay applied: {result['decayed']} nodes updated, "
                f"{result['archived']} nodes archived."
            )
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def cmd_export(args: argparse.Namespace) -> int:
    """Export the active backend's graph + vectors to an NDJSON file."""
    try:
        from engrama.backends import create_stores
        from engrama.migrate import export_graph

        graph_store, vector_store = create_stores()
        try:
            counts = export_graph(
                graph_store,
                vector_store,
                Path(args.output),
                with_vectors=not args.no_vectors,
            )
        finally:
            close = getattr(graph_store, "close", None)
            if callable(close):
                close()
        print(
            f"Exported {counts['nodes']} nodes, {counts['relations']} relations, "
            f"{counts['vectors']} vectors → {args.output}"
        )
        return 0
    except Exception as e:
        print(f"Export failed: {e}", file=sys.stderr)
        return 1


def cmd_import(args: argparse.Namespace) -> int:
    """Import an NDJSON dump into the active backend."""
    try:
        from engrama.backends import create_stores
        from engrama.migrate import import_graph

        input_path = Path(args.input)
        if not input_path.exists():
            print(f"Error: input file not found: {args.input}", file=sys.stderr)
            return 1

        graph_store, vector_store = create_stores()
        try:
            counts = import_graph(
                graph_store,
                vector_store,
                input_path,
                purge=args.purge,
            )
        finally:
            close = getattr(graph_store, "close", None)
            if callable(close):
                close()
        msg = (
            f"Imported {counts['nodes']} nodes, {counts['relations']} relations, "
            f"{counts['vectors']} vectors from {args.input}"
        )
        if counts["skipped_vectors"]:
            msg += (
                f" (skipped {counts['skipped_vectors']} vectors — dimensions mismatch; "
                f"run `engrama reindex` to rebuild under the active embedder)"
            )
        print(msg)
        return 0
    except Exception as e:
        print(f"Import failed: {e}", file=sys.stderr)
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
    p_init = sub.add_parser(
        "init",
        help="Generate schema from profile and apply it to the configured backend",
    )
    p_init.add_argument(
        "--profile",
        "-p",
        required=True,
        help="Profile name (e.g. 'developer') or path to YAML file",
    )
    p_init.add_argument(
        "--modules",
        "-m",
        nargs="*",
        default=[],
        help="Domain modules to compose (e.g. hacking teaching photography ai)",
    )
    p_init.add_argument(
        "--no-apply",
        action="store_true",
        help="Generate files only, don't apply to the backend",
    )
    p_init.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated content to stdout without writing files",
    )

    # --- verify ---
    sub.add_parser("verify", help="Check the configured backend is reachable")

    # --- reflect ---
    sub.add_parser("reflect", help="Run cross-entity pattern detection")

    # --- search ---
    p_search = sub.add_parser("search", help="Fulltext search")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument(
        "--limit",
        "-l",
        type=int,
        default=10,
        help="Max results (default: 10)",
    )

    # --- reindex ---
    p_reindex = sub.add_parser(
        "reindex",
        help="Batch re-embed all nodes and store vectors",
    )
    p_reindex.add_argument(
        "--batch-size",
        "-b",
        type=int,
        default=50,
        help="Batch size for embedding (default: 50)",
    )
    p_reindex.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="Re-embed nodes that already have embeddings",
    )

    # --- decay ---
    p_decay = sub.add_parser(
        "decay",
        help="Apply confidence decay to nodes (DDR-003 Phase D)",
    )
    p_decay.add_argument(
        "--rate",
        "-r",
        type=float,
        default=0.01,
        help="Exponential decay rate (default: 0.01)",
    )
    p_decay.add_argument(
        "--min-confidence",
        "-c",
        type=float,
        default=0.0,
        help="Archive nodes below this confidence after decay (default: 0, no archive)",
    )
    p_decay.add_argument(
        "--max-age",
        "-a",
        type=int,
        default=0,
        help="Archive nodes older than N days (default: 0, no age limit)",
    )
    p_decay.add_argument(
        "--label",
        type=str,
        default=None,
        help="Restrict to a specific label (default: all labels)",
    )
    p_decay.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without making changes",
    )

    # --- export ---
    p_export = sub.add_parser(
        "export",
        help="Dump the active backend's graph + vectors to NDJSON",
    )
    p_export.add_argument(
        "output",
        help="Path to the NDJSON file to write (parent dirs are created)",
    )
    p_export.add_argument(
        "--no-vectors",
        action="store_true",
        help=(
            "Skip embeddings (graph + relations only). Default: include "
            "vectors when an embedder is configured."
        ),
    )

    # --- import ---
    p_import = sub.add_parser(
        "import",
        help="Restore an NDJSON dump into the active backend",
    )
    p_import.add_argument(
        "input",
        help="Path to the NDJSON file to read",
    )
    p_import.add_argument(
        "--purge",
        action="store_true",
        help="Wipe the destination graph + vectors before importing",
    )

    args = parser.parse_args()

    handlers = {
        "init": cmd_init,
        "verify": cmd_verify,
        "reflect": cmd_reflect,
        "search": cmd_search,
        "reindex": cmd_reindex,
        "decay": cmd_decay,
        "export": cmd_export,
        "import": cmd_import,
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
