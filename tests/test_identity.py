"""Tests for standalone single-user identity (Spec 001, FR-7 / R-5)."""

from __future__ import annotations

import uuid

import pytest

from engrama.core.identity import resolve_local_sub


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("ENGRAMA_LOCAL_SUB", raising=False)


def test_env_var_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAMA_LOCAL_SUB", "explicit-sub")
    assert resolve_local_sub(state_dir=tmp_path) == "explicit-sub"
    # The env override must not write a state file.
    assert not (tmp_path / "local_sub").exists()


def test_env_var_whitespace_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("ENGRAMA_LOCAL_SUB", "   ")
    sub = resolve_local_sub(state_dir=tmp_path)
    # Falls back to a generated UUID, persisted.
    uuid.UUID(sub)  # raises if not a valid UUID


def test_generated_uuid_is_persisted_and_stable(tmp_path):
    first = resolve_local_sub(state_dir=tmp_path)
    uuid.UUID(first)  # valid UUID
    assert (tmp_path / "local_sub").read_text(encoding="utf-8").strip() == first
    # Second call reads back the same value rather than regenerating.
    assert resolve_local_sub(state_dir=tmp_path) == first


def test_state_dir_created_if_missing(tmp_path):
    nested = tmp_path / "does" / "not" / "exist"
    sub = resolve_local_sub(state_dir=nested)
    assert (nested / "local_sub").read_text(encoding="utf-8").strip() == sub
