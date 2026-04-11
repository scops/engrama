"""
engrama/cli.py

Command-line interface for Engrama.

Commands:

    engrama init --profile developer
        Generate schema.py and init-schema.cypher from a profile YAML,
        then apply the schema to Neo4j.

    engrama verify
        Check connectivity to Neo4j.

    engrama reflect
        Run cross-entity pattern detection and print results.

    engrama search <query>
        Fulltext search across the memory graph.

    engrama schema --dry-run
        Preview what the codegen would produce without writing files.
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
    print(f"Generating schema from {profile_path.name}...")
    cmd = [sys.executable, str(generate_script), str(profile_path)]
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

                # Read and execute the cypher script statement by statement
                cypher_text = cypher_path.read_text(encoding="utf-8")
                statements = [
                    s.strip() for s in cypher_text.split(";")
                    if s.strip() and not s.strip().startswith("//")
                ]
                for stmt in statements:
                    if stmt:
                        try:
                            client.run(stmt)
                        except Exception as e:
                            # Some statements (SHOW) may fail on certain Neo4j
                            # editions — skip non-critical errors.
                            if "SHOW" not in stmt.upper():
                                print(f"  Warning: {e}", file=sys.stderr)
                client.close()
                print("Schema applied successfully.")
            except Exception as e:
                print(f"Warning: could not apply schema to Neo4j: {e}", file=sys.stderr)
                print("  You can apply it manually with:", file=sys.stderr)
                print(f"  cypher-shell < {cypher_path}", file=sys.stderr)
        else:
            print("No init-schema.cypher found — skipping Neo4j apply.")

    return 0


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


def main() -> int:
    """Main CLI entry point."""
    parser = argparse.ArgumentParser(
        prog="engrama",
        description="Engrama — graph-based long-term memory for AI agents",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- init ---
    init_p = subparsers.add_parser(
        "init",
        help="Generate schema from a profile and apply to Neo4j",
    )
    init_p.add_argument(
        "--profile", "-p",
        required=True,
        help="Profile name (e.g. 'developer') or path to YAML file",
    )
    init_p.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview generated output without writing files",
    )
    init_p.add_argument(
        "--no-apply",
        action="store_true",
        help="Generate files but don't apply schema to Neo4j",
    )

    # --- verify ---
    subparsers.add_parser("verify", help="Check Neo4j connectivity")

    # --- reflect ---
    subparsers.add_parser("reflect", help="Run cross-entity pattern detection")

    # --- search ---
    search_p = subparsers.add_parser("search", help="Fulltext search the memory graph")
    search_p.add_argument("query", help="Search string")
    search_p.add_argument("--limit", "-n", type=int, default=10, help="Max results")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    handlers = {
        "init": cmd_init,
        "verify": cmd_verify,
        "reflect": cmd_reflect,
        "search": cmd_search,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
