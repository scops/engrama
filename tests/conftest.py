"""
Engrama test suite — conftest.py

Tests that need a live Neo4j instance go through the ``neo4j_driver``
fixture, which skips gracefully when ``NEO4J_PASSWORD`` is not set.
This means the SQLite-only test suite (``tests/backends/test_sqlite*``)
runs without any external dependency — matching the spec's goal of
``pip install engrama && pytest`` working out of the box.
"""

import os

import pytest
from dotenv import load_dotenv

load_dotenv()

NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "")
NEO4J_AVAILABLE = bool(NEO4J_PASS)


@pytest.fixture(scope="session")
def neo4j_driver():
    if not NEO4J_AVAILABLE:
        pytest.skip("Neo4j not configured (set NEO4J_PASSWORD to run)")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    driver.verify_connectivity()
    yield driver
    driver.close()


@pytest.fixture(scope="function")
def neo4j_session(neo4j_driver):
    with neo4j_driver.session() as session:
        yield session
        # Clean up test nodes after each test.
        session.run("MATCH (n) WHERE n.test = true DETACH DELETE n")
