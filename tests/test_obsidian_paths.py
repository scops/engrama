"""Vault path resolution: every note path stays inside the vault root."""

from __future__ import annotations

from pathlib import Path

import pytest

from engrama.adapters.obsidian.adapter import ObsidianAdapter


@pytest.fixture()
def vault(tmp_path: Path) -> ObsidianAdapter:
    root = tmp_path / "vault"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "a.md").write_text("# a\n", encoding="utf-8")
    # A sibling directory whose name extends the vault's own name.
    (tmp_path / "vault-other").mkdir()
    (tmp_path / "vault-other" / "b.md").write_text("# b\n", encoding="utf-8")
    return ObsidianAdapter(root)


@pytest.mark.parametrize("path", ["notes/a.md", "./notes/a.md", "notes/../notes/a.md"])
def test_paths_inside_the_vault_resolve(vault: ObsidianAdapter, path: str) -> None:
    assert vault.read_note(path)["success"] is True


@pytest.mark.parametrize(
    "path",
    ["../vault-other/b.md", "../outside.md", "notes/../../vault-other/b.md", "/etc/hostname"],
)
def test_paths_outside_the_vault_are_rejected(vault: ObsidianAdapter, path: str) -> None:
    with pytest.raises(ValueError):
        vault.read_note(path)
