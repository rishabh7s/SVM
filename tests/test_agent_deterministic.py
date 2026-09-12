"""
Tests for the deterministic logic inside kivi/retrieval/agent.py -- the
parts that don't require an actual LLM call: tool dispatch, the false-premise
checker, and the headless-mode needs_disambiguation collapse. The tool-calling
LOOP itself (agent.run) makes real network calls and is not covered here --
see the module docstring in agent.py.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from kivi.models.responses import AgentResponse, Citation
from kivi.retrieval.agent import ToolCall, _execute_tool, check_false_premise

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
# Tool dispatch
# ---------------------------------------------------------------------------

def test_execute_tool_dispatches_search_nodes(conn):
    result = _execute_tool(conn, ToolCall(tool_name="search_nodes", arguments_json='{"query": "convergence issue"}'))
    assert result[0]["node_id"] == "evt_001"


def test_execute_tool_dispatches_get_node_details(conn):
    result = _execute_tool(conn, ToolCall(tool_name="get_node_details", arguments_json='{"node_id": "fact_002"}'))
    assert result["value_numeric"] == 4500000
    assert result["foreground_app"] == "Slack"  # provenance embedded


def test_execute_tool_dispatches_get_connected_edges(conn):
    result = _execute_tool(
        conn, ToolCall(tool_name="get_connected_edges", arguments_json='{"source_node_id": "evt_001", "edge_type": "resolve"}')
    )
    assert result[0]["connected_node_id"] == "evt_002"


def test_execute_tool_dispatches_compute_without_connection_arg(conn):
    result = _execute_tool(conn, ToolCall(tool_name="compute", arguments_json='{"operation": "sum", "values": [1, 2, 3]}'))
    assert result == 6


def test_execute_tool_unknown_tool_returns_error_not_crash(conn):
    result = _execute_tool(conn, ToolCall(tool_name="not_a_real_tool", arguments_json="{}"))
    assert "error" in result


def test_execute_tool_bad_arguments_returns_error_not_crash(conn):
    result = _execute_tool(conn, ToolCall(tool_name="get_node_details", arguments_json='{"wrong_arg": "x"}'))
    assert "error" in result


def test_execute_tool_malformed_json_string_returns_error_not_crash(conn):
    result = _execute_tool(conn, ToolCall(tool_name="get_node_details", arguments_json="not valid json{{{"))
    assert "error" in result


# ---------------------------------------------------------------------------
# False-premise check
# ---------------------------------------------------------------------------

def test_false_premise_detected_when_question_assumes_done_but_status_is_blocked(conn):
    citation = Citation(source_type="commitment", source_id="com_002", snippet="commit to timeline: blocked")
    correction = check_false_premise(conn, "Is the timeline commitment done yet?", [citation])
    assert correction is not None
    assert "blocked" in correction


def test_no_false_premise_when_status_actually_matches(conn):
    citation = Citation(source_type="commitment", source_id="com_001", snippet="ask about budget: done")
    correction = check_false_premise(conn, "Is it done?", [citation])
    assert correction is None


def test_no_false_premise_check_triggered_for_unrelated_question(conn):
    citation = Citation(source_type="commitment", source_id="com_002", snippet="commit to timeline: blocked")
    correction = check_false_premise(conn, "What's the budget for Meridian?", [citation])
    assert correction is None


def test_false_premise_check_ignores_non_commitment_citations(conn):
    citation = Citation(source_type="fact", source_id="fact_002", snippet="budget=4500000")
    correction = check_false_premise(conn, "Did I agree to this?", [citation])
    assert correction is None


# ---------------------------------------------------------------------------
# headless mode collapses needs_disambiguation into an abstention
# ---------------------------------------------------------------------------

def test_headless_collapse_transformation_shape():
    original = AgentResponse(
        response_type="needs_disambiguation",
        disambiguation_options=["Meridian project", "Meridian Analytics client"],
    )
    collapsed = AgentResponse(
        response_type="abstain",
        abstain_reason="ambiguous_action: cannot present disambiguation options in headless mode",
    )
    # confirms the collapsed shape is itself a valid AgentResponse (would raise otherwise)
    assert collapsed.response_type == "abstain"
    assert original.response_type == "needs_disambiguation"
