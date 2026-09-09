"""
Tests for kivi/api/memory_ops.py -- the deterministic memory management
layer shared by the REST API and the agent's conversational tools. No LLM,
no network. Runs against a disposable temp copy of the seeded database.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from kivi.retrieval.memory_ops import delete_memory, get_memory, update_memory
from kivi.retrieval.tools import get_connected_edges, get_node_history, search_nodes

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "kivi.db"


@pytest.fixture
def conn(tmp_path):
    temp_db = tmp_path / "kivi_test.db"
    shutil.copy(DB_PATH, temp_db)
    connection = sqlite3.connect(temp_db)
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


# ---------------------------------------------------------------------------
# get_memory
# ---------------------------------------------------------------------------

def test_get_memory_passthrough(conn):
    result = get_memory(conn, "fact_002")
    assert result["value_numeric"] == 4500000


def test_get_memory_nonexistent_returns_error(conn):
    result = get_memory(conn, "fact_does_not_exist")
    assert "error" in result


# ---------------------------------------------------------------------------
# update_memory -- facts
# ---------------------------------------------------------------------------

def test_update_fact_creates_new_version_not_mutation(conn):
    result = update_memory(conn, "fact_002", {"value_numeric": 4200000}, note="corrected number")
    assert "error" not in result
    new_id = result["fact_id"]
    assert new_id != "fact_002"
    assert result["value_numeric"] == 4200000
    assert result["is_active"] == 1
    assert result["foreground_app"] == "Kivi API"  # honest provenance -- not mislabeled as a dictation

    # old row still exists, superseded, not deleted
    old = conn.execute("SELECT is_active, superseded_by_id, deleted_at FROM declarative_facts WHERE fact_id='fact_002'").fetchone()
    assert old["is_active"] == 0
    assert old["superseded_by_id"] == new_id
    assert old["deleted_at"] is None


def test_update_fact_full_history_preserved_across_manual_edit(conn):
    update_memory(conn, "fact_002", {"value_numeric": 4200000})
    history = get_node_history(conn, "fact_002")
    assert len(history["history"]) == 3  # cap_001 dictation -> cap_002 dictation -> manual edit
    assert history["history"][-1]["value_numeric"] == 4200000


def test_update_fact_rejects_updating_a_superseded_row(conn):
    result = update_memory(conn, "fact_001", {"value_numeric": 1})  # fact_001 is already superseded
    assert "error" in result
    assert "not the current version" in result["error"]


def test_update_fact_rejects_unknown_field(conn):
    result = update_memory(conn, "fact_002", {"attribute": "sneaky_rename"})
    assert "error" in result


def test_update_deleted_fact_is_rejected(conn):
    delete_memory(conn, "fact_002")
    result = update_memory(conn, "fact_002", {"value_numeric": 1})
    assert "error" in result
    assert "deleted" in result["error"]


# ---------------------------------------------------------------------------
# update_memory -- commitments
# ---------------------------------------------------------------------------

def test_update_commitment_status_creates_new_version(conn):
    result = update_memory(
        conn, "com_002", {"status": "done", "status_confirmed_by_user": True}, note="finance lead confirmed"
    )
    assert "error" not in result
    assert result["status"] == "done"

    old_status = conn.execute(
        "SELECT is_active, superseded_by_id FROM commitment_status_events WHERE status_event_id = 'cse_003'"
    ).fetchone()
    assert old_status["is_active"] == 0
    assert old_status["superseded_by_id"] is not None


def test_update_commitment_rejects_done_without_confirmation(conn):
    result = update_memory(conn, "com_002", {"status": "done", "status_confirmed_by_user": False})
    assert "error" in result
    assert "status_confirmed_by_user" in result["error"]


def test_update_commitment_rejects_blocked_without_reason(conn):
    result = update_memory(conn, "com_001", {"status": "blocked", "blocking_reason": None})
    assert "error" in result


# ---------------------------------------------------------------------------
# update_memory -- events (correction path)
# ---------------------------------------------------------------------------

def test_update_event_creates_new_event_and_corrects_edge(conn):
    result = update_memory(conn, "evt_002", {"description": "corrected description"}, note="fixed a typo")
    assert "error" not in result
    new_id = result["event_id"]
    assert result["description"] == "corrected description"

    old = conn.execute("SELECT deleted_at, description FROM episodic_events WHERE event_id='evt_002'").fetchone()
    assert old["deleted_at"] is not None
    assert old["description"] == "Forced the fixed-step solver at 0.001s to match the controller sample time."  # unchanged

    edges = get_connected_edges(conn, new_id)
    assert len(edges) == 1
    assert edges[0]["relationship_type"] == "corrects"
    assert edges[0]["connected_node_id"] == "evt_002"


def test_update_event_requires_nonempty_description(conn):
    result = update_memory(conn, "evt_001", {"description": ""})
    assert "error" in result


# ---------------------------------------------------------------------------
# update_memory -- entities and unrecognized ids
# ---------------------------------------------------------------------------

def test_update_entity_is_rejected(conn):
    result = update_memory(conn, "ent_meridian", {"canonical_name": "New Name"})
    assert "error" in result


def test_update_unrecognized_id_returns_error(conn):
    result = update_memory(conn, "xyz_unknown", {})
    assert "error" in result


# ---------------------------------------------------------------------------
# delete_memory
# ---------------------------------------------------------------------------

def test_delete_fact_soft_deletes(conn):
    result = delete_memory(conn, "fact_002", reason="test deletion")
    assert result["deleted"] is True
    assert result["deleted_at"] is not None

    row = conn.execute("SELECT deleted_at FROM declarative_facts WHERE fact_id='fact_002'").fetchone()
    assert row["deleted_at"] is not None  # row still exists, just flagged


def test_delete_excludes_from_search(conn):
    delete_memory(conn, "evt_001")
    results = search_nodes(conn, "convergence issue")
    assert results == []


def test_delete_still_visible_via_direct_get(conn):
    delete_memory(conn, "evt_001")
    details = get_memory(conn, "evt_001")
    assert "error" not in details
    assert details["deleted_at"] is not None


def test_delete_still_visible_via_history_for_facts(conn):
    delete_memory(conn, "fact_002")
    history = get_node_history(conn, "fact_002")
    assert history["history"][-1]["deleted_at"] is not None


def test_double_delete_rejected(conn):
    delete_memory(conn, "evt_001")
    result = delete_memory(conn, "evt_001")
    assert "error" in result
    assert "already deleted" in result["error"]


def test_delete_entity_rejected(conn):
    result = delete_memory(conn, "ent_meridian")
    assert "error" in result


def test_delete_unrecognized_id_returns_error(conn):
    result = delete_memory(conn, "xyz_unknown")
    assert "error" in result


def test_delete_nonexistent_id_returns_error(conn):
    result = delete_memory(conn, "fact_totally_made_up")
    assert "error" in result
