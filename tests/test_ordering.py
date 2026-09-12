"""
Tests for get_open_commitments -- the "what should I work on first?"
capability. The ordering is deterministic Python, so all of it is testable
with no model call.
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from kivi.retrieval.agent import TOOL_REGISTRY
from kivi.retrieval.tools import get_open_commitments


def _iso(days_from_today: int) -> str:
    return (datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days_from_today)).date().isoformat()


def _commitment(
    conn: sqlite3.Connection,
    mention: str,
    *,
    status: str = "open",
    due: str | None = None,
    blocking_reason: str | None = None,
    entity_id: str | None = "ent_meridian",
    created_at: str = "2026-01-01T00:00:00",
) -> str:
    """Inserts one commitment plus its active status row."""
    capture_id = conn.execute("SELECT capture_id FROM captures LIMIT 1").fetchone()[0]
    commitment_id = f"com_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO commitments (commitment_id, commitment_mention, description, entity_id, "
        " source_capture_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (commitment_id, mention, f"description of {mention}", entity_id, capture_id, created_at),
    )
    conn.execute(
        "INSERT INTO commitment_status_events (status_event_id, commitment_id, status, "
        " status_confirmed_by_user, blocking_reason, due_date_resolved, source_capture_id, is_active) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (
            f"cse_{uuid.uuid4().hex[:12]}", commitment_id, status,
            1 if status == "done" else 0, blocking_reason, due, capture_id,
        ),
    )
    return commitment_id


def _precedes(conn: sqlite3.Connection, first: str, then: str) -> None:
    capture_id = conn.execute("SELECT capture_id FROM captures LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO relationships (relationship_id, source_type, source_id, target_type, target_id, "
        " relationship_type, reason, source_capture_id) "
        "VALUES (?, 'commitment', ?, 'commitment', ?, 'must_precede', 'test', ?)",
        (f"rel_{uuid.uuid4().hex[:12]}", first, then, capture_id),
    )


def _clear(conn: sqlite3.Connection) -> None:
    """Empties the seed's commitments so each test controls the whole set."""
    conn.execute("DELETE FROM relationships WHERE source_type = 'commitment'")
    conn.execute("DELETE FROM commitment_status_events")
    conn.execute("DELETE FROM commitments")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_tool_is_registered():
    assert TOOL_REGISTRY["get_open_commitments"] is get_open_commitments


def test_prompt_points_at_the_tool_for_ordering_questions():
    from kivi.retrieval.agent import SYSTEM_PROMPT

    assert "get_open_commitments" in SYSTEM_PROMPT
    assert "do not assemble that answer yourself" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Dependencies win -- the vision's "ask the question before the meeting"
# ---------------------------------------------------------------------------

def test_dependency_beats_urgency(seeded_conn):
    """A prerequisite comes first even when the thing it unblocks is more
    urgent -- doing the dependent work first isn't suboptimal, it's blocked."""
    _clear(seeded_conn)
    ask = _commitment(seeded_conn, "ask the finance lead about the budget", due=_iso(30))
    commit = _commitment(seeded_conn, "commit to a rollout timeline", due=_iso(1))
    _precedes(seeded_conn, ask, commit)

    result = get_open_commitments(seeded_conn)
    order = [x["commitment_id"] for x in result["ordered"]]
    assert order.index(ask) < order.index(commit), "prerequisite must be listed first"

    ask_entry = next(x for x in result["ordered"] if x["commitment_id"] == ask)
    commit_entry = next(x for x in result["ordered"] if x["commitment_id"] == commit)
    assert "commit to a rollout timeline" in ask_entry["unblocks"]
    assert "ask the finance lead about the budget" in commit_entry["must_follow"]
    assert "must happen after" in commit_entry["why_here"]


def test_dependency_chain_is_fully_ordered(seeded_conn):
    _clear(seeded_conn)
    a = _commitment(seeded_conn, "step A", due=_iso(9))
    b = _commitment(seeded_conn, "step B", due=_iso(5))
    c = _commitment(seeded_conn, "step C", due=_iso(1))
    _precedes(seeded_conn, a, b)
    _precedes(seeded_conn, b, c)

    order = [x["commitment_id"] for x in get_open_commitments(seeded_conn)["ordered"]]
    assert order.index(a) < order.index(b) < order.index(c)


def test_circular_dependencies_degrade_to_urgency_with_a_warning(seeded_conn):
    """Contradictory recorded edges must not drop work or hang."""
    _clear(seeded_conn)
    x = _commitment(seeded_conn, "task X", due=_iso(5))
    y = _commitment(seeded_conn, "task Y", due=_iso(2))
    _precedes(seeded_conn, x, y)
    _precedes(seeded_conn, y, x)

    result = get_open_commitments(seeded_conn)
    assert len(result["ordered"]) == 2, "both commitments must still be listed"
    assert result.get("warnings"), "a cycle must be reported, not silently resolved"
    assert "circular" in result["warnings"][0]


# ---------------------------------------------------------------------------
# Urgency ordering
# ---------------------------------------------------------------------------

def test_overdue_comes_first_and_says_so(seeded_conn):
    _clear(seeded_conn)
    _commitment(seeded_conn, "future work", due=_iso(20))
    _commitment(seeded_conn, "overdue work", due=_iso(-5))

    ordered = get_open_commitments(seeded_conn)["ordered"]
    assert ordered[0]["commitment"] == "overdue work"
    assert "overdue" in ordered[0]["why_here"]


def test_dated_work_beats_undated_work(seeded_conn):
    _clear(seeded_conn)
    _commitment(seeded_conn, "someday", due=None)
    _commitment(seeded_conn, "next week", due=_iso(7))

    ordered = get_open_commitments(seeded_conn)["ordered"]
    assert [x["commitment"] for x in ordered] == ["next week", "someday"]


def test_in_progress_beats_not_started_at_equal_urgency(seeded_conn):
    """Finishing started work beats starting new work."""
    _clear(seeded_conn)
    _commitment(seeded_conn, "not started", status="open", due=None, created_at="2026-01-01T00:00:00")
    _commitment(seeded_conn, "already going", status="in_progress", due=None, created_at="2026-01-02T00:00:00")

    ordered = get_open_commitments(seeded_conn)["ordered"]
    assert [x["commitment"] for x in ordered] == ["already going", "not started"]


def test_ordering_is_stable_across_identical_calls(seeded_conn):
    _clear(seeded_conn)
    for i in range(6):
        _commitment(seeded_conn, f"task {i}", due=None)

    first = [x["commitment_id"] for x in get_open_commitments(seeded_conn)["ordered"]]
    second = [x["commitment_id"] for x in get_open_commitments(seeded_conn)["ordered"]]
    assert first == second


# ---------------------------------------------------------------------------
# Blocked work is separated, never ranked as today's work
# ---------------------------------------------------------------------------

def test_blocked_work_is_separated_with_its_reason(seeded_conn):
    _clear(seeded_conn)
    _commitment(seeded_conn, "can do this", due=_iso(3))
    _commitment(
        seeded_conn, "cannot do this", status="blocked",
        blocking_reason="waiting on vendor access from Meridian Logistics",
    )

    result = get_open_commitments(seeded_conn)
    assert [x["commitment"] for x in result["ordered"]] == ["can do this"]
    assert len(result["blocked"]) == 1
    blocked = result["blocked"][0]
    assert blocked["commitment"] == "cannot do this"
    # the user's own words are the answer to "what am I waiting on"
    assert "Meridian Logistics" in blocked["blocking_reason"]
    assert "Meridian Logistics" in blocked["why_here"]
    assert "rank" not in blocked, "blocked work must not be ranked as actionable"


def test_blocked_work_is_excluded_even_when_overdue(seeded_conn):
    """An overdue blocked item is still not something the user can act on."""
    _clear(seeded_conn)
    _commitment(seeded_conn, "actionable", due=_iso(30))
    _commitment(seeded_conn, "blocked and overdue", status="blocked",
                blocking_reason="waiting on legal", due=_iso(-10))

    result = get_open_commitments(seeded_conn)
    assert [x["commitment"] for x in result["ordered"]] == ["actionable"]
    assert [x["commitment"] for x in result["blocked"]] == ["blocked and overdue"]


# ---------------------------------------------------------------------------
# Scope, exclusions, empty states
# ---------------------------------------------------------------------------

def test_done_commitments_never_appear(seeded_conn):
    """A memory system that reminds you about finished work stops being trusted."""
    _clear(seeded_conn)
    _commitment(seeded_conn, "finished", status="done")
    _commitment(seeded_conn, "outstanding", due=_iso(2))

    result = get_open_commitments(seeded_conn)
    everything = [x["commitment"] for x in result["ordered"] + result["blocked"]]
    assert everything == ["outstanding"]


def test_deleted_commitments_never_appear(seeded_conn):
    _clear(seeded_conn)
    forgotten = _commitment(seeded_conn, "forgotten", due=_iso(2))
    _commitment(seeded_conn, "kept", due=_iso(3))
    seeded_conn.execute(
        "UPDATE commitments SET deleted_at = '2026-01-01T00:00:00' WHERE commitment_id = ?",
        (forgotten,),
    )

    result = get_open_commitments(seeded_conn)
    assert [x["commitment"] for x in result["ordered"]] == ["kept"]


def test_entity_scoping(seeded_conn):
    _clear(seeded_conn)
    _commitment(seeded_conn, "meridian work", entity_id="ent_meridian")
    _commitment(seeded_conn, "simulink work", entity_id="ent_simulink")

    result = get_open_commitments(seeded_conn, entity_id="ent_meridian")
    assert [x["commitment"] for x in result["ordered"]] == ["meridian work"]


def test_empty_state_is_explicit_not_an_error(seeded_conn):
    _clear(seeded_conn)
    result = get_open_commitments(seeded_conn)
    assert result["ordered"] == [] and result["blocked"] == []
    assert "note" in result


def test_limit_caps_the_list_but_counts_report_the_truth(seeded_conn):
    _clear(seeded_conn)
    for i in range(10):
        _commitment(seeded_conn, f"task {i}", due=_iso(i))

    result = get_open_commitments(seeded_conn, limit=3)
    assert len(result["ordered"]) == 3
    assert result["counts"]["actionable"] == 10
    assert result["counts"]["shown"] == 3


def test_every_entry_explains_its_own_position(seeded_conn):
    """`why_here` is what lets the agent explain rather than assert."""
    _clear(seeded_conn)
    _commitment(seeded_conn, "overdue", due=_iso(-1))
    _commitment(seeded_conn, "scheduled", due=_iso(5))
    _commitment(seeded_conn, "started", status="in_progress")
    _commitment(seeded_conn, "undated")
    _commitment(seeded_conn, "stuck", status="blocked", blocking_reason="waiting on procurement")

    result = get_open_commitments(seeded_conn)
    for entry in result["ordered"] + result["blocked"]:
        assert entry.get("why_here"), f"{entry['commitment']} has no explanation"


def test_ordering_rule_is_disclosed(seeded_conn):
    """The rule is part of the answer, not hidden in the implementation."""
    _clear(seeded_conn)
    _commitment(seeded_conn, "anything")
    assert "must_precede" in get_open_commitments(seeded_conn)["ordering_rule"]


# ---------------------------------------------------------------------------
# Against the real seed narrative
# ---------------------------------------------------------------------------

def test_seed_narrative_surfaces_the_blocked_timeline(seeded_conn):
    """In the seed story the budget question is done and committing to a
    timeline is blocked on confirmation -- so nothing is actionable and the
    blocked item carries the explanation."""
    result = get_open_commitments(seeded_conn)
    blocked = [x["commitment"] for x in result["blocked"]]
    assert "commit to timeline" in blocked
    entry = next(x for x in result["blocked"] if x["commitment"] == "commit to timeline")
    assert "budget" in entry["blocking_reason"].lower()
