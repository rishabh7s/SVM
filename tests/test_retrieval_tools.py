"""
Unit tests for kivi/retrieval/tools.py's atomic graph tools, run directly
against a temp copy of the seeded database -- no agent, no LLM, no network.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from kivi.retrieval.tools import (
    compute,
    get_connected_edges,
    get_events_in_window,
    get_node_details,
    get_node_history,
    search_nodes,
)

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
# search_nodes
# ---------------------------------------------------------------------------

def test_search_nodes_finds_event_via_stemming(conn):
    results = search_nodes(conn, "convergence issue")
    assert len(results) == 1
    assert results[0]["node_id"] == "evt_001"
    assert results[0]["node_type"] == "event"


def test_search_nodes_finds_entity_by_alias(conn):
    results = search_nodes(conn, "meridian")
    node_ids = [r["node_id"] for r in results]
    assert "ent_meridian" in node_ids


def test_search_nodes_finds_across_multiple_node_types(conn):
    results = search_nodes(conn, "meridian")
    node_types = {r["node_type"] for r in results}
    assert "entity" in node_types
    assert "fact" in node_types
    assert "commitment" in node_types


def test_search_nodes_handles_hyphenated_query_without_error(conn):
    results = search_nodes(conn, "fixed-step solver")
    assert len(results) == 1
    assert results[0]["node_id"] == "evt_002"


def test_search_nodes_dedupes_entity_matched_via_multiple_aliases(conn):
    results = search_nodes(conn, "meridian project rollout")
    entity_hits = [r for r in results if r["node_type"] == "entity"]
    entity_ids = [r["node_id"] for r in entity_hits]
    assert entity_ids.count("ent_meridian") == 1


def test_search_nodes_unrelated_query_returns_empty(conn):
    assert search_nodes(conn, "coffee machine broken") == []


def test_search_nodes_empty_query_returns_empty(conn):
    assert search_nodes(conn, "") == []
    assert search_nodes(conn, "   ") == []


# ---------------------------------------------------------------------------
# get_node_details
# ---------------------------------------------------------------------------

def test_get_node_details_fact_includes_provenance_and_supersession_pointers(conn):
    details = get_node_details(conn, "fact_001")
    assert details["node_type"] == "fact"
    assert details["is_active"] == 0
    assert details["superseded_by_id"] == "fact_002"
    assert details["value_numeric"] == 5000000
    assert details["capture_id"] == "cap_001"
    assert details["foreground_app"] == "Slack"


def test_get_node_details_does_not_auto_resolve_superseded_fact(conn):
    details = get_node_details(conn, "fact_001")
    assert details["value_numeric"] == 5000000  # NOT 4500000 (the current value)


def test_get_node_details_commitment_includes_current_status(conn):
    details = get_node_details(conn, "com_002")
    assert details["node_type"] == "commitment"
    assert details["status"] == "blocked"
    assert details["blocking_reason"] is not None


def test_get_node_details_entity_includes_all_aliases(conn):
    details = get_node_details(conn, "ent_meridian")
    assert details["node_type"] == "entity"
    aliases = [a["alias"] for a in details["aliases"]]
    assert "meridian" in aliases
    assert "meridian project" in aliases


def test_get_node_details_event_includes_provenance(conn):
    details = get_node_details(conn, "evt_002")
    assert details["node_type"] == "event"
    assert details["foreground_app"] == "MATLAB"


def test_get_node_details_nonexistent_id_returns_error_not_exception(conn):
    result = get_node_details(conn, "fact_does_not_exist")
    assert "error" in result


def test_get_node_details_unrecognized_prefix_returns_error(conn):
    result = get_node_details(conn, "xyz_totally_unknown")
    assert "error" in result


# ---------------------------------------------------------------------------
# get_connected_edges
# ---------------------------------------------------------------------------

def test_get_connected_edges_finds_resolution_outgoing(conn):
    edges = get_connected_edges(conn, "evt_001", edge_type="resolve")
    assert len(edges) == 1
    assert edges[0]["direction"] == "outgoing"
    assert edges[0]["connected_node_id"] == "evt_002"
    assert "fixed-step solver" in edges[0]["connected_snippet"]


def test_get_connected_edges_finds_problem_incoming(conn):
    edges = get_connected_edges(conn, "evt_002", edge_type="resolve")
    assert len(edges) == 1
    assert edges[0]["direction"] == "incoming"
    assert edges[0]["connected_node_id"] == "evt_001"


def test_get_connected_edges_filters_by_edge_type(conn):
    no_match = get_connected_edges(conn, "evt_001", edge_type="must_precede")
    assert no_match == []


def test_get_connected_edges_without_filter_returns_all(conn):
    edges = get_connected_edges(conn, "com_001")
    assert len(edges) == 1
    assert edges[0]["relationship_type"] == "must_precede"


def test_get_connected_edges_entity_returns_empty_not_error(conn):
    assert get_connected_edges(conn, "ent_meridian") == []


def test_get_connected_edges_unrecognized_id_returns_empty(conn):
    assert get_connected_edges(conn, "xyz_unknown") == []


def test_get_connected_edges_node_with_no_matching_edge_type_returns_empty(conn):
    assert get_connected_edges(conn, "evt_001", edge_type="nonexistent_type") == []


# ---------------------------------------------------------------------------
# get_node_history
# ---------------------------------------------------------------------------

def test_get_node_history_fact_full_chain_regardless_of_which_id_passed(conn):
    history = get_node_history(conn, "fact_001")
    assert history["node_type"] == "fact"
    assert len(history["history"]) == 2
    assert history["history"][0]["fact_id"] == "fact_001"
    assert history["history"][1]["fact_id"] == "fact_002"


def test_get_node_history_fact_chain_same_from_current_id_too(conn):
    history = get_node_history(conn, "fact_002")
    assert len(history["history"]) == 2


def test_get_node_history_commitment_status_chain(conn):
    history = get_node_history(conn, "com_001")
    assert history["node_type"] == "commitment"
    statuses = [h["status"] for h in history["history"]]
    assert statuses == ["open", "done"]


def test_get_node_history_event_returns_empty_with_note(conn):
    history = get_node_history(conn, "evt_001")
    assert history["history"] == []
    assert "note" in history


def test_get_node_history_entity_returns_empty_with_note(conn):
    history = get_node_history(conn, "ent_meridian")
    assert history["history"] == []
    assert "note" in history


def test_get_node_history_nonexistent_id_returns_error(conn):
    result = get_node_history(conn, "fact_does_not_exist")
    assert "error" in result


# ---------------------------------------------------------------------------
# Orthogonal tools (compute, get_events_in_window)
# ---------------------------------------------------------------------------

def test_compute_sum():
    assert compute("sum", [100, 200, 300]) == 600


def test_compute_rejects_empty_values():
    with pytest.raises(ValueError):
        compute("sum", [])


def test_get_events_in_window_finds_simulink_events(conn):
    events = get_events_in_window(conn, "2026-08-01T00:00:00", "2026-08-31T23:59:59", entity_id="ent_simulink")
    assert len(events) == 2


def test_get_events_in_window_outside_range_returns_empty(conn):
    assert get_events_in_window(conn, "2020-01-01T00:00:00", "2020-01-31T00:00:00") == []
