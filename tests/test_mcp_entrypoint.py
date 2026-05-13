"""Regression tests for the ``engrama-mcp`` CLI entry point.

The entry point lives in ``engrama/adapters/mcp/__init__.py::main``. Its only
job before delegating to the FastMCP server is to surface a clean install
hint when the ``[mcp]`` extra is missing — otherwise the user sees a raw
``ImportError`` traceback on a ``pip install engrama`` base install.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Iterator

import pytest


@pytest.fixture
def isolate_mcp_module(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make ``import mcp...`` fail with ImportError(name='mcp')."""
    # Drop anything cached so the next import goes through Python's import
    # machinery instead of returning a cached module.
    for name in list(sys.modules):
        if name == "mcp" or name.startswith(("mcp.", "fastmcp")):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "engrama.adapters.mcp.server", raising=False)
    # Setting these to None makes Python raise ImportError('import of X
    # halted; None in sys.modules', name='X') on any future import attempt.
    monkeypatch.setitem(sys.modules, "mcp", None)
    monkeypatch.setitem(sys.modules, "fastmcp", None)
    yield


def test_main_emits_install_hint_when_mcp_extra_missing(
    isolate_mcp_module: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Re-import the adapter module so it picks up the patched sys.modules.
    import engrama.adapters.mcp as mcp_adapter

    importlib.reload(mcp_adapter)

    with pytest.raises(SystemExit) as excinfo:
        mcp_adapter.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "requires the 'mcp' extra" in err
    assert "uv sync --extra mcp" in err
    # No Python traceback should leak to stderr.
    assert "Traceback" not in err


def test_main_reraises_unrelated_import_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ImportError for something OTHER than the mcp extra must propagate."""
    import engrama.adapters.mcp as mcp_adapter

    def _explode() -> None:
        raise ImportError("No module named 'something_else'", name="something_else")

    # Patch the deferred import target so calling main() triggers our exception.
    monkeypatch.setattr(
        mcp_adapter,
        "_PROJECT_ROOT",
        mcp_adapter._PROJECT_ROOT,  # touched only to ensure module loaded
    )
    monkeypatch.setitem(sys.modules, "engrama.adapters.mcp.server", None)

    with pytest.raises(ImportError) as excinfo:
        mcp_adapter.main()
    assert "engrama.adapters.mcp.server" in str(excinfo.value) or "something_else" in str(
        excinfo.value
    )
