"""
Engrama test suite — conftest.py

Tests that need a live Neo4j carry the ``neo4j`` marker (module-level
``pytestmark`` or a marked parameter). They are skipped at collection
time when ``NEO4J_PASSWORD`` is not set, before any fixture tries to
connect, so a plain ``pytest`` on a base install (``pip install engrama``
or ``uv sync``) runs everything else and never waits on a missing
server. CI runs ``-m "not neo4j"`` and ``-m neo4j`` as separate jobs.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "")
NEO4J_AVAILABLE = bool(NEO4J_PASS)


try:
    import neo4j  # noqa: F401

    _NEO4J_DRIVER_INSTALLED = True
except ImportError:
    _NEO4J_DRIVER_INSTALLED = False

_NEO4J_MODULE_MARK = "pytestmark = pytest.mark.neo4j"


def pytest_ignore_collect(collection_path, config):
    """Without the ``neo4j`` extra, don't import whole-module Neo4j suites.

    They import the driver at module level, so collecting them would fail
    before the marker could skip them.
    """
    if _NEO4J_DRIVER_INSTALLED or collection_path.suffix != ".py":
        return None
    if collection_path.name.startswith("test_") and _NEO4J_MODULE_MARK in (
        collection_path.read_text(encoding="utf-8")
    ):
        return True
    return None


def pytest_collection_modifyitems(config, items):
    if NEO4J_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="Neo4j not configured (set NEO4J_PASSWORD to run)")
    for item in items:
        if "neo4j" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def neo4j_driver():
    if not NEO4J_AVAILABLE:
        pytest.skip("Neo4j not configured (set NEO4J_PASSWORD to run)")
    from neo4j import GraphDatabase

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    driver.verify_connectivity()
    yield driver
    driver.close()


class _BufferedResult:
    """The records of a fully consumed query, with ``Result.single()`` semantics."""

    def __init__(self, records: list) -> None:
        self._records = records

    def single(self):
        return self._records[0] if self._records else None


class _CommittingSession:
    """A session whose ``run`` has committed by the time it returns.

    A bare ``session.run`` is a lazy auto-commit transaction: it only commits
    once its result is consumed. Tests seed data here and then read it back
    through the engine's own connection, so an unconsumed seeding ``MERGE``
    was intermittently invisible to the code under test (flaky associate
    tests). Consuming eagerly makes every seed durable before the next step.
    """

    def __init__(self, session) -> None:
        self._session = session

    def run(self, query, parameters=None, **kwargs):
        return _BufferedResult(list(self._session.run(query, parameters, **kwargs)))

    def __getattr__(self, name):
        return getattr(self._session, name)


@pytest.fixture(scope="function")
def neo4j_session(neo4j_driver):
    with neo4j_driver.session() as raw_session:
        session = _CommittingSession(raw_session)
        yield session
        # Clean up test nodes after each test.
        session.run("MATCH (n) WHERE n.test = true DETACH DELETE n")
