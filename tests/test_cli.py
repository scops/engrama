"""
Tests for Engrama CLI (engrama/cli.py).

Tests the CLI commands via subprocess to simulate real usage.
"""

from __future__ import annotations

import subprocess
import sys


def run_cli(*args: str) -> subprocess.CompletedProcess:
    """Run `python -m engrama.cli <args>` and capture output."""
    return subprocess.run(
        [sys.executable, "-m", "engrama.cli", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestCliInit:
    """Tests for `engrama init`."""

    def test_init_dry_run(self) -> None:
        """Init with --dry-run prints generated output without writing."""
        result = run_cli("init", "--profile", "developer", "--dry-run")
        assert result.returncode == 0
        assert "PROFILE SUMMARY" in result.stdout
        assert "schema.py" in result.stdout
        assert "init-schema.cypher" in result.stdout

    def test_init_missing_profile(self) -> None:
        """Init with nonexistent profile fails cleanly."""
        result = run_cli("init", "--profile", "nonexistent_xyz_profile")
        assert result.returncode == 1
        assert "not found" in result.stderr


class TestCliVerify:
    """Tests for `engrama verify`."""

    def test_verify_connection(self) -> None:
        """Verify succeeds against running Neo4j."""
        result = run_cli("verify")
        assert result.returncode == 0
        assert "Connected" in result.stdout


class TestCliSearch:
    """Tests for `engrama search`."""

    def test_search_no_results(self) -> None:
        """Search for gibberish returns no results (not an error)."""
        result = run_cli("search", "CLI_Nonexistent_ZZZ_99999")
        assert result.returncode == 0
        assert "No results" in result.stdout


class TestCliReflect:
    """Tests for `engrama reflect`."""

    def test_reflect_runs(self) -> None:
        """Reflect runs without error."""
        result = run_cli("reflect")
        assert result.returncode == 0


class TestCliHelp:
    """Tests for --help output."""

    def test_help(self) -> None:
        """Top-level help works."""
        result = run_cli("--help")
        assert result.returncode == 0
        assert "engrama" in result.stdout.lower()

    def test_init_help(self) -> None:
        """Init subcommand help works."""
        result = run_cli("init", "--help")
        assert result.returncode == 0
        assert "--profile" in result.stdout
