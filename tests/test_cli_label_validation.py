"""
Engrama — unit tests for CLI label-argument validation.

CLI ``--label`` / ``--labels`` values are interpolated into Cypher by the
backend (``decay_scores``, ``query_at_date``, ``migrate keys``). They must be
checked against the schema whitelist before they reach the store, mirroring the
validation the MCP/engine paths already do.

Pure-function tests — no Neo4j or DB connection required.
"""

from __future__ import annotations

from engrama.cli import _validate_label_args
from engrama.core.schema import NodeType


def test_none_and_absent_labels_pass() -> None:
    assert _validate_label_args() is None
    assert _validate_label_args(None) is None
    assert _validate_label_args(None, None) is None


def test_valid_schema_label_passes() -> None:
    a_valid = next(iter(NodeType)).value
    assert _validate_label_args(a_valid) is None


def test_multiple_valid_labels_pass() -> None:
    values = [m.value for m in NodeType][:3]
    assert _validate_label_args(*values) is None


def test_unknown_label_is_rejected() -> None:
    err = _validate_label_args("Definitely-Not-A-Label")
    assert err is not None
    assert "invalid label" in err


def test_injection_payload_is_rejected() -> None:
    err = _validate_label_args("Project} MATCH (x) DETACH DELETE x //")
    assert err is not None


def test_first_invalid_among_valid_is_caught() -> None:
    valid = next(iter(NodeType)).value
    assert _validate_label_args(valid, "Bogus") is not None
