"""
Engrama test suite — conftest.py

Provides a real Neo4j driver fixture for integration tests.
Set NEO4J_TEST_URI, NEO4J_TEST_USER, NEO4J_TEST_PASSWORD in .env or environment
to point at a running instance (docker compose up -d).
"""

import os
import pytest
from neo4j import GraphDatabase


from dotenv import load_dotenv

load_dotenv()

NEO4J_URI  = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASS = os.getenv("NEO4J_PASSWORD", "")

if not NEO4J_PASS:
    raise RuntimeError(
        "NEO4J_PASSWORD is not set. Copy .env.example to .env and fill in your password."
    )


@pytest.fixture(scope="session")
def neo4j_driver():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    driver.verify_connectivity()
    yield driver
    driver.close()


@pytest.fixture(scope="function")
def neo4j_session(neo4j_driver):
    with neo4j_driver.session() as session:
        yield session
        # clean up test nodes after each test
        session.run("MATCH (n) WHERE n.test = true DETACH DELETE n")
