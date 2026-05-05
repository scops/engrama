#!/usr/bin/env python3
"""
scripts/generate_from_profile.py

Code generator: reads a profile YAML (optionally composed with domain modules)
and produces all derived schema files.

Usage:
    # Standalone profile (backward-compatible):
    python scripts/generate_from_profile.py profiles/developer.yaml

    # Composable: base profile + domain modules:
    python scripts/generate_from_profile.py profiles/base.yaml \\
        --modules hacking teaching photography ai

    # Dry-run (preview without writing):
    python scripts/generate_from_profile.py profiles/base.yaml --modules hacking --dry-run

Generated files:
    engrama/core/schema.py          — NodeType/RelationType enums + dataclasses
    scripts/init-schema.cypher      — constraints + fulltext index + range indexes

The profile YAML is the single source of truth for the graph schema.
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Profile loader
# ---------------------------------------------------------------------------


def load_profile(path: Path) -> dict[str, Any]:
    """Load and validate a profile YAML file.

    Args:
        path: Path to the profile YAML.

    Returns:
        Parsed profile dict.

    Raises:
        SystemExit: If validation fails.
    """
    with open(path, encoding="utf-8") as f:
        profile = yaml.safe_load(f)

    errors: list[str] = []
    if "name" not in profile:
        errors.append("Profile must have a 'name' field.")
    if "nodes" not in profile or not profile["nodes"]:
        errors.append("Profile must define at least one node type.")
    if "relations" not in profile:
        errors.append("Profile must define a 'relations' list (may be empty).")

    for i, node in enumerate(profile.get("nodes", [])):
        if "label" not in node:
            errors.append(f"Node {i} is missing 'label'.")
        if "properties" not in node:
            errors.append(f"Node '{node.get('label', i)}' is missing 'properties'.")
        if "required" not in node:
            errors.append(f"Node '{node.get('label', i)}' is missing 'required'.")

    if errors:
        print("Profile validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    return profile


def load_module(name: str, modules_dir: Path) -> dict[str, Any]:
    """Load a domain module YAML from the modules directory.

    Args:
        name: Module name (without extension), e.g. 'hacking'.
        modules_dir: Path to the modules directory.

    Returns:
        Parsed module dict.

    Raises:
        SystemExit: If the module file is not found or invalid.
    """
    path = modules_dir / f"{name}.yaml"
    if not path.exists():
        print(f"Error: module not found: {path}", file=sys.stderr)
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        module = yaml.safe_load(f)
    if "nodes" not in module:
        module["nodes"] = []
    if "relations" not in module:
        module["relations"] = []
    return module


def merge_profiles(
    base: dict[str, Any],
    modules: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge a base profile with one or more domain modules.

    Nodes with the same label get their properties merged (union, preserving
    order from the first definition).  The longer description wins.
    Relations are unioned and deduplicated.

    Args:
        base: The base profile dict.
        modules: List of module dicts to merge in.

    Returns:
        A new merged profile dict ready for codegen.

    Raises:
        SystemExit: If relation endpoints reference undefined node labels.
    """
    # Index base nodes by label for fast lookup
    nodes_by_label: dict[str, dict[str, Any]] = {}
    for node in base.get("nodes", []):
        nodes_by_label[node["label"]] = dict(node)  # shallow copy

    # Merge each module's nodes
    module_names: list[str] = []
    for module in modules:
        module_names.append(module.get("name", "unknown"))
        for node in module.get("nodes", []):
            label = node["label"]
            if label in nodes_by_label:
                # Merge properties (union, base order first)
                existing = nodes_by_label[label]
                existing_props = list(existing["properties"])
                for prop in node["properties"]:
                    if prop not in existing_props:
                        existing_props.append(prop)
                existing["properties"] = existing_props

                # Merge required (union)
                existing_req = set(existing.get("required", []))
                existing_req.update(node.get("required", []))
                existing["required"] = sorted(existing_req)

                # Keep longer description
                new_desc = node.get("description", "")
                old_desc = existing.get("description", "")
                if len(new_desc) > len(old_desc):
                    existing["description"] = new_desc
            else:
                nodes_by_label[label] = dict(node)

    # Merge relations (deduplicate by (type, from, to) tuple)
    seen_rels: set[tuple[str, str, str]] = set()
    merged_rels: list[dict[str, str]] = []

    for rel in base.get("relations", []):
        key = (rel["type"], rel["from"], rel["to"])
        if key not in seen_rels:
            seen_rels.add(key)
            merged_rels.append(rel)

    for module in modules:
        for rel in module.get("relations", []):
            key = (rel["type"], rel["from"], rel["to"])
            if key not in seen_rels:
                seen_rels.add(key)
                merged_rels.append(rel)

    # Validate: all relation endpoints must exist in the merged node set
    all_labels = set(nodes_by_label.keys())
    errors: list[str] = []
    for rel in merged_rels:
        if rel["from"] not in all_labels:
            errors.append(
                f"Relation {rel['type']}: from-label '{rel['from']}' "
                f"not defined in any profile or module."
            )
        if rel["to"] not in all_labels:
            errors.append(
                f"Relation {rel['type']}: to-label '{rel['to']}' "
                f"not defined in any profile or module."
            )
    if errors:
        print("Merge validation errors:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Build merged profile
    composed_name = base["name"] + "+" + "+".join(module_names)
    return {
        "name": composed_name,
        "description": f"Composed profile: {base.get('name', 'base')} + {', '.join(module_names)}",
        "nodes": list(nodes_by_label.values()),
        "relations": merged_rels,
    }


# ---------------------------------------------------------------------------
# Determine the merge key for a node
# ---------------------------------------------------------------------------


def _merge_key(node_def: dict[str, Any]) -> str:
    """Return the merge key for a node definition.

    If 'title' is in the required list, use 'title'. Otherwise 'name'.
    """
    required = node_def.get("required", [])
    if "title" in required:
        return "title"
    return "name"


# ---------------------------------------------------------------------------
# Generator: schema.py
# ---------------------------------------------------------------------------

_SCHEMA_HEADER = '''\
"""
Engrama — Graph schema as Python dataclasses.

Auto-generated from profile: {profile_name}
Generated at: {timestamp}

Do not edit manually — regenerate with:
    python scripts/generate_from_profile.py profiles/{profile_name}.yaml
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
'''


def _python_type(prop_name: str) -> str:
    """Infer a Python type hint from a property name."""
    if prop_name in ("created_at", "updated_at"):
        return "Optional[datetime.datetime]"
    if prop_name in ("date",):
        return "Optional[datetime.date]"
    if prop_name in ("stack", "tags"):
        return "list[str]"
    if prop_name in ("confidence",):
        return "float"
    return "Optional[str]"


def _default_value(prop_name: str, is_required: bool) -> str:
    """Return the default value expression for a dataclass field."""
    if is_required:
        return ""  # no default
    if prop_name in ("created_at", "updated_at", "date"):
        return " = None"
    if prop_name in ("stack", "tags"):
        return " = field(default_factory=list)"
    if prop_name in ("confidence",):
        return " = 0.8"
    if prop_name in ("status",):
        return ' = None'
    return " = None"


def generate_schema(profile: dict[str, Any]) -> str:
    """Generate the full schema.py content from a profile.

    Args:
        profile: Parsed profile dict.

    Returns:
        Python source code as a string.
    """
    lines: list[str] = []

    # Header
    lines.append(_SCHEMA_HEADER.format(
        profile_name=profile["name"],
        timestamp=datetime.now().isoformat(timespec="seconds"),
    ))

    # NodeType enum
    lines.append("")
    lines.append("# " + "-" * 75)
    lines.append("# Enums")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("")
    lines.append("class NodeType(str, Enum):")
    lines.append(f'    """Labels for every node defined in the {profile["name"]} profile."""')
    lines.append("")

    all_nodes = profile["nodes"]
    # Always include Insight as a system node
    node_labels = [n["label"] for n in all_nodes]
    if "Insight" not in node_labels:
        node_labels.append("Insight")

    for label in node_labels:
        lines.append(f'    {label.upper()} = "{label}"')
    lines.append("")

    # RelationType enum
    lines.append("")
    lines.append("class RelationType(str, Enum):")
    lines.append(f'    """Relationship types defined in the {profile["name"]} profile."""')
    lines.append("")

    rel_types: list[str] = []
    for rel in profile.get("relations", []):
        rt = rel["type"]
        if rt not in rel_types:
            rel_types.append(rt)
    for rt in rel_types:
        lines.append(f'    {rt} = "{rt}"')
    lines.append("")

    # Dataclasses
    lines.append("")
    lines.append("# " + "-" * 75)
    lines.append("# Node dataclasses")
    lines.append("# " + "-" * 75)

    for node_def in all_nodes:
        label = node_def["label"]
        props = node_def["properties"]
        required = set(node_def.get("required", []))
        merge_key = _merge_key(node_def)
        description = node_def.get("description", f"A {label} node in the memory graph.")

        lines.append("")
        lines.append("")
        lines.append("@dataclass")
        lines.append(f"class {label}:")
        lines.append(f'    """{description}"""')
        lines.append("")

        # Required fields first
        for prop in props:
            if prop in required:
                lines.append(f"    {prop}: str")
        # Then optional fields
        for prop in props:
            if prop not in required:
                ptype = _python_type(prop)
                default = _default_value(prop, False)
                lines.append(f"    {prop}: {ptype}{default}")

        # Enrichment fields — rich context for every node (see node-enrichment
        # initiative). Skip any that are already defined on this node to
        # avoid dataclass duplicates (e.g. Dataset already has 'source').
        existing = set(props)
        if "summary" not in existing:
            lines.append("    summary: Optional[str] = None")
        if "details" not in existing:
            lines.append("    details: Optional[str] = None")
        if "tags" not in existing:
            lines.append("    tags: list[str] = field(default_factory=list)")
        if "source" not in existing:
            lines.append("    source: Optional[str] = None")

        # Always add timestamps
        lines.append("    created_at: Optional[datetime.datetime] = None")
        lines.append("    updated_at: Optional[datetime.datetime] = None")

    # Always add Insight dataclass if not user-defined
    if "Insight" not in [n["label"] for n in all_nodes]:
        lines.append("")
        lines.append("")
        lines.append("@dataclass")
        lines.append("class Insight:")
        lines.append('    """A cross-entity pattern detected by the reflect skill."""')
        lines.append("")
        lines.append("    title: str")
        lines.append('    body: str = ""')
        lines.append("    confidence: float = 0.8")
        lines.append('    status: str = "pending"')
        lines.append('    source_query: str = ""')
        # Enrichment fields also on Insight
        lines.append("    summary: Optional[str] = None")
        lines.append("    details: Optional[str] = None")
        lines.append("    tags: list[str] = field(default_factory=list)")
        lines.append("    source: Optional[str] = None")
        lines.append("    created_at: Optional[datetime.datetime] = None")
        lines.append("    updated_at: Optional[datetime.datetime] = None")

    # NODE_DATACLASS_MAP
    lines.append("")
    lines.append("")
    lines.append("# " + "-" * 75)
    lines.append("# Mapping helpers")
    lines.append("# " + "-" * 75)
    lines.append("")
    lines.append("NODE_DATACLASS_MAP: dict[NodeType, type] = {")
    for label in node_labels:
        lines.append(f"    NodeType.{label.upper()}: {label},")
    lines.append("}")
    lines.append('"""Maps each ``NodeType`` enum member to its corresponding dataclass."""')
    lines.append("")

    # Title-keyed labels helper
    title_labels = [n["label"] for n in all_nodes if _merge_key(n) == "title"]
    lines.append("")
    lines.append(f"TITLE_KEYED_LABELS: frozenset[str] = frozenset({set(title_labels)!r})")
    lines.append('"""Node labels that use ``title`` instead of ``name`` as merge key."""')
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generator: init-schema.cypher
# ---------------------------------------------------------------------------


def generate_cypher(profile: dict[str, Any]) -> str:
    """Generate the init-schema.cypher content from a profile.

    Args:
        profile: Parsed profile dict.

    Returns:
        Cypher script as a string.
    """
    lines: list[str] = []
    lines.append(f"// Engrama — schema initialisation script")
    lines.append(f"// Auto-generated from profile: {profile['name']}")
    lines.append(f"// Generated at: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("//")
    lines.append("// Run once after Neo4j starts:")
    lines.append("//   docker exec -i engrama-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "
                  "< scripts/init-schema.cypher")
    lines.append("")
    lines.append("// === CONSTRAINTS ===")
    lines.append("")

    all_nodes = profile["nodes"]
    node_labels = [n["label"] for n in all_nodes]
    if "Insight" not in node_labels:
        node_labels.append("Insight")

    for node_def in all_nodes:
        label = node_def["label"]
        key = _merge_key(node_def)
        constraint_name = f"{label.lower()}_{key}"
        lines.append(f"CREATE CONSTRAINT {constraint_name} IF NOT EXISTS")
        lines.append(f"  FOR (n:{label}) REQUIRE n.{key} IS UNIQUE;")
        lines.append("")

    # Insight constraint (always present)
    if "Insight" not in [n["label"] for n in all_nodes]:
        lines.append("CREATE CONSTRAINT insight_title IF NOT EXISTS")
        lines.append("  FOR (n:Insight) REQUIRE n.title IS UNIQUE;")
        lines.append("")

    # Fulltext index
    lines.append("// === FULLTEXT INDEX ===")
    lines.append("")

    label_list = "|".join(node_labels)

    # Collect all text properties across all nodes.
    # NOTE: 'tags' IS indexable (Neo4j fulltext supports string arrays) — it is
    # a first-class enrichment field and we want it searchable.
    text_props: list[str] = []
    for node_def in all_nodes:
        for prop in node_def["properties"]:
            if prop not in text_props and prop not in ("date", "confidence", "stack"):
                text_props.append(prop)
    # Always include body for Insight
    if "body" not in text_props:
        text_props.append("body")
    # Always include enrichment fields (summary, details, tags) so every node
    # can be retrieved by rich context regardless of profile-specific props.
    for enrich_prop in ("summary", "details", "tags"):
        if enrich_prop not in text_props:
            text_props.append(enrich_prop)

    prop_list = ", ".join(f"n.{p}" for p in text_props)

    # DROP + CREATE so changes to the index definition take effect when
    # `engrama init` re-applies the schema. DROP IF EXISTS is idempotent.
    lines.append("DROP INDEX memory_search IF EXISTS;")
    lines.append("")
    lines.append("CREATE FULLTEXT INDEX memory_search")
    lines.append(f"FOR (n:{label_list})")
    lines.append(f"ON EACH [{prop_list}];")
    lines.append("")

    # Vector index for embeddings (DDR-003 Phase C)
    # Uses secondary :Embedded label so one index covers all primary labels
    lines.append("// === VECTOR INDEX (DDR-003) ===")
    lines.append("")
    lines.append("CREATE VECTOR INDEX memory_vectors IF NOT EXISTS")
    lines.append("FOR (n:Embedded) ON (n.embedding)")
    lines.append("OPTIONS {indexConfig: {")
    lines.append("  `vector.dimensions`: 768,")
    lines.append("  `vector.similarity_function`: 'cosine'")
    lines.append("}};")
    lines.append("")

    # Range indexes for status fields
    lines.append("// === RANGE INDEXES ===")
    lines.append("")
    for node_def in all_nodes:
        if "status" in node_def["properties"]:
            label = node_def["label"]
            lines.append(f"CREATE INDEX {label.lower()}_status IF NOT EXISTS")
            lines.append(f"  FOR (n:{label}) ON (n.status);")
            lines.append("")

    # Verify
    lines.append("// === VERIFY ===")
    lines.append("")
    lines.append("SHOW CONSTRAINTS;")
    lines.append('SHOW INDEXES YIELD name, type, state WHERE state = "ONLINE";')
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Generator: profile summary (human-readable)
# ---------------------------------------------------------------------------


def generate_summary(profile: dict[str, Any]) -> str:
    """Generate a human-readable summary of the profile for review.

    Args:
        profile: Parsed profile dict.

    Returns:
        Markdown summary.
    """
    lines: list[str] = []
    lines.append(f"# Profile: {profile['name']}")
    if "description" in profile:
        lines.append(f"\n> {profile['description']}")
    lines.append("")

    lines.append("## Node types")
    lines.append("")
    for node_def in profile["nodes"]:
        label = node_def["label"]
        key = _merge_key(node_def)
        props = ", ".join(node_def["properties"])
        desc = node_def.get("description", "")
        lines.append(f"- **{label}** (key: `{key}`) — {desc}")
        lines.append(f"  Properties: {props}")
    lines.append("")

    lines.append("## Relationships")
    lines.append("")
    for rel in profile.get("relations", []):
        lines.append(f"- `{rel['from']}` --[{rel['type']}]--> `{rel['to']}`")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for the codegen script."""
    parser = argparse.ArgumentParser(
        description="Generate Engrama schema files from a profile YAML."
    )
    parser.add_argument("profile", type=Path, help="Path to the profile YAML file.")
    parser.add_argument(
        "--modules", "-m",
        nargs="+",
        default=[],
        help=(
            "Domain modules to compose with the base profile. "
            "Pass module names (e.g. 'hacking teaching ai') — "
            "resolved from profiles/modules/ directory."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print generated files to stdout without writing.",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root directory (default: auto-detect from script location).",
    )
    args = parser.parse_args()

    # Resolve project root
    if args.project_root:
        root = args.project_root.resolve()
    else:
        root = Path(__file__).resolve().parent.parent

    profile = load_profile(args.profile)

    # If modules are specified, load and merge them
    if args.modules:
        modules_dir = root / "profiles" / "modules"
        if not modules_dir.is_dir():
            print(f"Error: modules directory not found: {modules_dir}", file=sys.stderr)
            sys.exit(1)
        loaded_modules = [load_module(m, modules_dir) for m in args.modules]
        profile = merge_profiles(profile, loaded_modules)

    schema_content = generate_schema(profile)
    cypher_content = generate_cypher(profile)
    summary_content = generate_summary(profile)

    if args.dry_run:
        print("=" * 60)
        print("PROFILE SUMMARY")
        print("=" * 60)
        print(summary_content)
        print("=" * 60)
        print("engrama/core/schema.py")
        print("=" * 60)
        print(schema_content)
        print("=" * 60)
        print("scripts/init-schema.cypher")
        print("=" * 60)
        print(cypher_content)
    else:
        schema_path = root / "engrama" / "core" / "schema.py"
        cypher_path = root / "scripts" / "init-schema.cypher"

        schema_path.write_text(schema_content, encoding="utf-8")
        print(f"Written: {schema_path}")
        print(f"Written: {schema_path}")

        cypher_path.write_text(cypher_content, encoding="utf-8")
        print(f"Written: {cypher_path}")

        print(f"\nProfile '{profile['name']}' applied successfully.")
        print("Next steps:")
        print("  1. Review the generated files")
        print("  2. Drop and recreate the fulltext index in Neo4j:")
        print("     docker exec -i engrama-neo4j cypher-shell -u neo4j -p $NEO4J_PASSWORD "
              "< scripts/init-schema.cypher")
        print("  3. Run tests: uv run pytest tests/ -v")


if __name__ == "__main__":
    main()
