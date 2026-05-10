"""
Engrama — Graph-based long-term memory framework for AI agents.

Quick start::

    from engrama import Engrama

    with Engrama() as eng:
        eng.remember("Technology", "FastAPI", "Async web framework")
        results = eng.recall("FastAPI", hops=2)
"""

__version__ = "0.9.0"

from engrama.adapters.sdk import Engrama  # noqa: F401

__all__ = ["__version__", "Engrama"]
