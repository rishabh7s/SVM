"""
Unit tests for kivi/retrieval/tools.py's save_memory -- the agent's
proactive-write tool. Pure Python/SQL, no LLM, no network: save_memory
itself never calls a model (see its docstring), so these test the actual
write path directly, the same way test_retrieval_tools.py tests the
read-only tools.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from kivi.retrieval.tools import get_node_details, get_node_history, save_memory, search_nodes

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "kivi.db"


@pytest.fixture
def conn(tmp_path):
    temp_db = tmp_path / "kivi_test_save_memory.db"
    shutil.copy(DB_PATH, temp_db)
    connection = sqlite3.connect(temp_db)
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def test_save_memory_creates_new_entity_and_fact(conn):
    msg = save_memory(conn, entity="Alex", attribute="role", value="PM")
    assert "Saved" in msg
    assert "Alex" in msg and "PM" in msg

    results = search_nodes(conn, "Alex PM")
    node_types = {r["node_type"] for r in results}
    assert "fact" in node_types
    assert "entity" in node_types


def test_save_memory_is_immediately_searchable(conn):
    save_memory(conn, entity="Nova", attribute="deadline", value="Dec 1")
    results = search_nodes(conn, "Nova deadline")
    assert any(r["node_type"] == "fact" for r in results)


def test_save_memory_supersedes_existing_fact_not_duplicates(conn):
    save_memory(conn, entity="David", attribute="role", value="engineer")
    msg2 = save_memory(conn, entity="David", attribute="role", value="tech lead")
    assert "Updated" in msg2 or "Saved" in msg2

    # exactly one ACTIVE fact for (entity, attribute) -- never two
    results = search_nodes(conn, "David role")
    fact_nodes = [r for r in results if r["node_type"] == "fact"]
    active_facts = [f for f in fact_nodes if get_node_details(conn, f["node_id"])["is_active"] == 1]
    assert len(active_facts) == 1
    assert "tech lead" in get_node_details(conn, active_facts[0]["node_id"])["value_text"]

    # old value preserved in history, not erased
    history = get_node_history(conn, active_facts[0]["node_id"])
    values = [h["value_text"] for h in history["history"]]
    assert "engineer" in values
    assert "tech lead" in values


def test_save_memory_resolves_to_same_entity_on_repeated_mention(conn):
    save_memory(conn, entity="Priya", attribute="team", value="Platform")
    save_memory(conn, entity="Priya", attribute="location", value="Bengaluru")

    results = search_nodes(conn, "Priya")
    entity_ids = {r["node_id"] for r in results if r["node_type"] == "entity"}
    assert len(entity_ids) == 1  # same entity reused, not a duplicate "Priya" created


def test_save_memory_noop_when_value_unchanged(conn):
    save_memory(conn, entity="Sam", attribute="status", value="active")
    msg = save_memory(conn, entity="Sam", attribute="status", value="active")
    assert "No change needed" in msg


def test_save_memory_rejects_empty_fields(conn):
    assert "failed" in save_memory(conn, entity="", attribute="role", value="PM")
    assert "failed" in save_memory(conn, entity="Alex", attribute="", value="PM")
    assert "failed" in save_memory(conn, entity="Alex", attribute="role", value="")


def test_save_memory_records_context_as_provenance(conn):
    save_memory(conn, entity="Rohan", attribute="role", value="lead", context="Rohan just told me he's now the lead.")
    results = search_nodes(conn, "Rohan lead")
    fact_id = next(r["node_id"] for r in results if r["node_type"] == "fact")
    details = get_node_details(conn, fact_id)
    assert details["formatted_text"] == "Rohan just told me he's now the lead."
    assert details["foreground_app"] == "Hey Kivi"
