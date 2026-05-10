"""
Tests for the composable profile system.

Tests module loading, merging, validation, and CLI integration.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# Import codegen functions directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_from_profile import (
    generate_cypher,
    generate_schema,
    generate_summary,
    load_module,
    load_profile,
    merge_profiles,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def base_profile() -> dict[str, Any]:
    """Minimal base profile for testing."""
    return {
        "name": "base",
        "description": "Test base profile",
        "nodes": [
            {
                "label": "Project",
                "properties": ["name", "status", "description"],
                "required": ["name"],
                "description": "A project.",
            },
            {
                "label": "Concept",
                "properties": ["name", "domain", "notes"],
                "required": ["name"],
                "description": "A concept.",
            },
        ],
        "relations": [
            {"type": "APPLIES", "from": "Project", "to": "Concept"},
        ],
    }


@pytest.fixture
def module_a() -> dict[str, Any]:
    """A domain module that adds new node types."""
    return {
        "name": "alpha",
        "description": "Test module A",
        "nodes": [
            {
                "label": "Target",
                "properties": ["name", "ip", "status"],
                "required": ["name"],
                "description": "A target machine.",
            },
        ],
        "relations": [
            {"type": "SCANS", "from": "Project", "to": "Target"},
            {"type": "APPLIES", "from": "Target", "to": "Concept"},
        ],
    }


@pytest.fixture
def module_b() -> dict[str, Any]:
    """A domain module that extends an existing node and adds its own."""
    return {
        "name": "beta",
        "description": "Test module B",
        "nodes": [
            {
                "label": "Project",  # same as base — should merge properties
                "properties": ["name", "budget", "deadline"],
                "required": ["name"],
                "description": "A project with budget tracking.",
            },
            {
                "label": "Course",
                "properties": ["name", "level", "status"],
                "required": ["name"],
                "description": "A training course.",
            },
        ],
        "relations": [
            {"type": "TEACHES", "from": "Course", "to": "Concept"},
        ],
    }


@pytest.fixture
def module_bad_ref() -> dict[str, Any]:
    """A module that references a label not defined anywhere."""
    return {
        "name": "broken",
        "description": "Broken module",
        "nodes": [],
        "relations": [
            {"type": "NEEDS", "from": "Project", "to": "Unicorn"},
        ],
    }


# ---------------------------------------------------------------------------
# Merge logic tests
# ---------------------------------------------------------------------------


class TestMergeProfiles:
    """Tests for merge_profiles()."""

    def test_merge_adds_new_nodes(self, base_profile: dict, module_a: dict) -> None:
        """Modules add new node types to the merged result."""
        merged = merge_profiles(base_profile, [module_a])
        labels = [n["label"] for n in merged["nodes"]]
        assert "Project" in labels
        assert "Concept" in labels
        assert "Target" in labels  # from module_a

    def test_merge_preserves_base_relations(self, base_profile: dict, module_a: dict) -> None:
        """Base relations are preserved in the merge."""
        merged = merge_profiles(base_profile, [module_a])
        rel_types = [r["type"] for r in merged["relations"]]
        assert "APPLIES" in rel_types
        assert "SCANS" in rel_types

    def test_merge_deduplicates_relations(self, base_profile: dict, module_a: dict) -> None:
        """Identical (type, from, to) tuples appear only once."""
        merged = merge_profiles(base_profile, [module_a])
        applies_rels = [
            r
            for r in merged["relations"]
            if r["type"] == "APPLIES" and r["from"] == "Project" and r["to"] == "Concept"
        ]
        assert len(applies_rels) == 1

    def test_merge_properties_union(self, base_profile: dict, module_b: dict) -> None:
        """When two sources define the same node, properties are merged."""
        merged = merge_profiles(base_profile, [module_b])
        project = next(n for n in merged["nodes"] if n["label"] == "Project")
        props = project["properties"]
        # Base had: name, status, description
        # Module B had: name, budget, deadline
        assert "name" in props
        assert "status" in props
        assert "description" in props
        assert "budget" in props
        assert "deadline" in props

    def test_merge_keeps_longer_description(self, base_profile: dict, module_b: dict) -> None:
        """The longer description wins on merge."""
        merged = merge_profiles(base_profile, [module_b])
        project = next(n for n in merged["nodes"] if n["label"] == "Project")
        assert project["description"] == "A project with budget tracking."

    def test_merge_multiple_modules(
        self, base_profile: dict, module_a: dict, module_b: dict
    ) -> None:
        """Multiple modules compose correctly."""
        merged = merge_profiles(base_profile, [module_a, module_b])
        labels = [n["label"] for n in merged["nodes"]]
        assert "Project" in labels
        assert "Concept" in labels
        assert "Target" in labels
        assert "Course" in labels
        assert len(labels) == 4

    def test_merge_composed_name(self, base_profile: dict, module_a: dict, module_b: dict) -> None:
        """Composed profile name includes all component names."""
        merged = merge_profiles(base_profile, [module_a, module_b])
        assert merged["name"] == "base+alpha+beta"

    def test_merge_rejects_bad_relation_refs(
        self, base_profile: dict, module_bad_ref: dict
    ) -> None:
        """Merge fails if a relation references a label not in any source."""
        with pytest.raises(SystemExit):
            merge_profiles(base_profile, [module_bad_ref])

    def test_merge_empty_modules_list(self, base_profile: dict) -> None:
        """Merging with no modules returns a valid profile."""
        merged = merge_profiles(base_profile, [])
        assert len(merged["nodes"]) == 2
        assert merged["name"] == "base+"


class TestMergeCodegen:
    """Tests that merged profiles produce valid codegen output."""

    def test_merged_schema_has_all_enums(self, base_profile: dict, module_a: dict) -> None:
        """Generated schema.py includes enum members for all merged nodes."""
        merged = merge_profiles(base_profile, [module_a])
        schema = generate_schema(merged)
        assert "PROJECT" in schema
        assert "CONCEPT" in schema
        assert "TARGET" in schema
        assert "INSIGHT" in schema  # always auto-added
        assert "SCANS" in schema

    def test_merged_cypher_has_constraints(self, base_profile: dict, module_a: dict) -> None:
        """Generated cypher includes constraints for all merged nodes."""
        merged = merge_profiles(base_profile, [module_a])
        cypher = generate_cypher(merged)
        assert "project_name" in cypher
        assert "concept_name" in cypher
        assert "target_name" in cypher
        assert "insight_title" in cypher

    def test_merged_summary_lists_all(
        self, base_profile: dict, module_a: dict, module_b: dict
    ) -> None:
        """Summary includes nodes and relations from all sources."""
        merged = merge_profiles(base_profile, [module_a, module_b])
        summary = generate_summary(merged)
        assert "Project" in summary
        assert "Target" in summary
        assert "Course" in summary
        assert "SCANS" in summary
        assert "TEACHES" in summary


# ---------------------------------------------------------------------------
# File-based tests (use real YAML files)
# ---------------------------------------------------------------------------


class TestRealProfiles:
    """Tests against the actual profile files in the repo."""

    @pytest.fixture
    def project_root(self) -> Path:
        return Path(__file__).resolve().parent.parent

    def test_base_yaml_loads(self, project_root: Path) -> None:
        """base.yaml is a valid profile."""
        profile = load_profile(project_root / "profiles" / "base.yaml")
        assert profile["name"] == "base"
        assert len(profile["nodes"]) >= 5

    def test_developer_yaml_loads(self, project_root: Path) -> None:
        """developer.yaml is still a valid standalone profile."""
        profile = load_profile(project_root / "profiles" / "developer.yaml")
        assert profile["name"] == "developer"

    def test_hacking_module_loads(self, project_root: Path) -> None:
        """hacking.yaml module loads correctly."""
        modules_dir = project_root / "profiles" / "modules"
        module = load_module("hacking", modules_dir)
        assert module["name"] == "hacking"
        labels = [n["label"] for n in module["nodes"]]
        assert "Target" in labels
        assert "Vulnerability" in labels

    def test_base_plus_all_modules(self, project_root: Path) -> None:
        """base + all four modules merge without validation errors."""
        base = load_profile(project_root / "profiles" / "base.yaml")
        modules_dir = project_root / "profiles" / "modules"
        modules = [
            load_module(m, modules_dir) for m in ["hacking", "teaching", "photography", "ai"]
        ]
        merged = merge_profiles(base, modules)
        labels = [n["label"] for n in merged["nodes"]]
        # Should have base nodes + all module-specific nodes
        assert "Project" in labels
        assert "Target" in labels  # hacking
        assert "Course" in labels  # teaching
        assert "Photo" in labels  # photography
        assert "Model" in labels  # ai
        # No duplicates
        assert len(labels) == len(set(labels))

    def test_base_plus_modules_dry_run(self, project_root: Path) -> None:
        """Codegen dry-run works with base + modules via generate_schema."""
        base = load_profile(project_root / "profiles" / "base.yaml")
        modules_dir = project_root / "profiles" / "modules"
        modules = [load_module("hacking", modules_dir)]
        merged = merge_profiles(base, modules)
        schema = generate_schema(merged)
        assert "class Target:" in schema
        assert "class Project:" in schema


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run `python -m engrama.cli <args>` and capture output."""
    return subprocess.run(
        [sys.executable, "-m", "engrama.cli", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCliComposable:
    """Tests for CLI with --modules flag."""

    def test_cli_init_base_plus_modules_dry_run(self) -> None:
        """CLI dry-run with base + modules produces valid output."""
        result = run_cli(
            "init",
            "--profile",
            "base",
            "--modules",
            "hacking",
            "teaching",
            "--dry-run",
        )
        assert result.returncode == 0
        assert "PROFILE SUMMARY" in result.stdout
        assert "Target" in result.stdout
        assert "Course" in result.stdout

    def test_cli_init_missing_module(self) -> None:
        """CLI fails cleanly when a module doesn't exist."""
        result = run_cli(
            "init",
            "--profile",
            "base",
            "--modules",
            "nonexistent_module_xyz",
            "--dry-run",
        )
        assert result.returncode != 0

    def test_cli_init_standalone_still_works(self) -> None:
        """Standalone profile (no modules) still works."""
        result = run_cli("init", "--profile", "developer", "--dry-run")
        assert result.returncode == 0
        assert "PROFILE SUMMARY" in result.stdout

    def test_cli_init_help_shows_modules(self) -> None:
        """Init --help mentions --modules flag."""
        result = run_cli("init", "--help")
        assert "--modules" in result.stdout
