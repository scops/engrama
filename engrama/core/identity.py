"""Standalone single-user identity (Spec 001, FR-7 / research R-5).

Bare OSS is single-user by design: it carries ``(org_id, user_id)`` only as
the namespacing primitive for SaaS-gateway compatibility. When no gateway is
in front (no inbound identity headers), Engrama resolves a single stable local
identity — ``sub_local`` — and uses it as ``user_id == org_id == sub_local``.

Derivation order (R-5):

1. ``ENGRAMA_LOCAL_SUB`` environment variable, if set and non-empty
   (explicit, reproducible override).
2. Otherwise a UUID generated once on first run and persisted to
   ``<state_dir>/local_sub`` (zero-config, stable per install).

Multi-user behaviour is out of scope for bare OSS; it exists only behind the
engrama-saas gateway, which injects ``X-Engrama-*`` headers instead.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

_ENV_VAR = "ENGRAMA_LOCAL_SUB"
_STATE_FILENAME = "local_sub"


def _default_state_dir() -> Path:
    """State directory for the persisted local identity (``~/.engrama``).

    Mirrors the default SQLite location so the local identity lives next to
    the rest of a standalone install's state.
    """
    return Path(os.path.expanduser("~/.engrama"))


def resolve_local_sub(state_dir: str | os.PathLike[str] | None = None) -> str:
    """Resolve the stable single-user local identity.

    ``ENGRAMA_LOCAL_SUB`` wins when set. Otherwise a UUID is read from — or
    generated and written to — ``<state_dir>/local_sub``. ``state_dir``
    defaults to ``~/.engrama`` and is accepted explicitly for tests.

    The persisted value is read back rather than regenerated on every call,
    so a standalone install keeps one identity across restarts.
    """
    env_value = os.getenv(_ENV_VAR)
    if env_value and env_value.strip():
        return env_value.strip()

    base = Path(state_dir) if state_dir is not None else _default_state_dir()
    state_file = base / _STATE_FILENAME

    if state_file.exists():
        existing = state_file.read_text(encoding="utf-8").strip()
        if existing:
            return existing

    generated = str(uuid.uuid4())
    base.mkdir(parents=True, exist_ok=True)
    state_file.write_text(generated, encoding="utf-8")
    return generated


__all__ = ["resolve_local_sub"]
