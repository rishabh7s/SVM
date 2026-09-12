"""
Tests for kivi/ingestion/writer.py, using hand-crafted ExtractionResult
objects as a stand-in for what the LLM would return -- no network call, no
dependency on model behavior. This tests the deterministic part of the
pipeline: entity resolution, fact/commitment supersession, and relationship
linking against the real schema.
"""

import shutil
import sqlite3
from pathlib import Path

import pytest

from kivi.ingestion.vocab_log import VocabLogger
from kivi.ingestion.writer import apply_extraction_result
from kivi.models.extraction import Commitment, Event, ExtractionResult, Fact, Relationship

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "kivi.db"


@pytest.fixture
def conn(tmp_path):
    """A fresh temp copy of the real seeded db per test -- never touches the real file."""
    temp_db = tmp_path / "kivi_test.db"
    shutil.copy(DB_PATH, temp_db)
    connection = sqlite3.connect(temp_db)
    connection.execute("PRAGMA foreign_keys = ON;")
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


def _insert_test_capture(conn: sqlite3.Connection, capture_id: str, captured_at: str = "2026-09-01T00:00:00"):
    conn.execute(
        "INSERT INTO captures (capture_id, raw_asr_text, formatted_text, source_modality, captured_at, extraction_status) "
        "VALUES (?, 'test raw', 'test formatted', 'speech', ?, 'pending')",
        (capture_id, captured_at),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fact supersession
# ---------------------------------------------------------------------------

def test_new_fact_for_known_entity_supersedes_old_active_fact(conn):
    """Meridian budget is currently 4500000 (fact_002, seeded)."""
    _insert_test_capture(conn, "cap_w001")
    result = ExtractionResult(
        capture_id="cap_w001",
        extraction_status="processed",
        facts=[
            Fact(
                entity_mention="meridian",  # matches existing alias -> resolves to ent_meridian via L1
                entity_type="project",
                attribute="budget",
                value_numeric=4200000,
                unit="INR",
                precision_class="exact_source",
                asserter_role="self",
            )
        ],
    )
    summary = apply_extraction_result(conn, "cap_w001", result)
    conn.commit()

    assert summary.facts_superseded == 1
    active = conn.execute(
        "SELECT value_numeric FROM declarative_facts WHERE entity_id='ent_meridian' AND attribute='budget' AND is_active=1"
    ).fetchone()
    assert active["value_numeric"] == 4200000

    old = conn.execute("SELECT is_active FROM declarative_facts WHERE fact_id='fact_002'").fetchone()
    assert old["is_active"] == 0


def test_identical_fact_value_is_a_no_op(conn):
    """Re-reporting the exact same active value should not create a new row."""
    _insert_test_capture(conn, "cap_w002")
    result = ExtractionResult(
        capture_id="cap_w002",
        extraction_status="processed",
        facts=[
            Fact(
                entity_mention="meridian",
                entity_type="project",
                attribute="budget",
                value_numeric=4500000,  # exactly matches current active fact_002
                unit="INR",
                precision_class="exact_source",
                asserter_role="self",
            )
        ],
    )
    before_count = conn.execute("SELECT COUNT(*) AS n FROM declarative_facts").fetchone()["n"]
    summary = apply_extraction_result(conn, "cap_w002", result)
    conn.commit()
    after_count = conn.execute("SELECT COUNT(*) AS n FROM declarative_facts").fetchone()["n"]

    assert summary.facts_unchanged == 1
    assert summary.facts_superseded == 0
    assert before_count == after_count


# ---------------------------------------------------------------------------
# Commitment supersession
# ---------------------------------------------------------------------------

def test_commitment_status_change_supersedes_not_mutates(conn):
    """com_002 is currently 'blocked' (seeded)."""
    _insert_test_capture(conn, "cap_w003")
    result = ExtractionResult(
        capture_id="cap_w003",
        extraction_status="processed",
        commitments=[
            Commitment(
                commitment_mention="commit to timeline",  # matches existing com_002
                description="Commit to a Meridian rollout timeline.",
                entity_mention="meridian",
                status="done",
                status_confirmed_by_user=True,
            )
        ],
    )
    summary = apply_extraction_result(conn, "cap_w003", result)
    conn.commit()

    assert summary.commitments_created == 0  # reused existing commitment identity
    assert summary.commitment_status_changed == 1

    active = conn.execute(
        "SELECT status FROM commitment_status_events WHERE commitment_id='com_002' AND is_active=1"
    ).fetchone()
    assert active["status"] == "done"

    old_blocked = conn.execute(
        "SELECT is_active, superseded_by_id FROM commitment_status_events WHERE status_event_id='cse_003'"
    ).fetchone()
    assert old_blocked["is_active"] == 0
    assert old_blocked["superseded_by_id"] is not None


def test_new_commitment_mention_creates_new_identity(conn):
    _insert_test_capture(conn, "cap_w004")
    result = ExtractionResult(
        capture_id="cap_w004",
        extraction_status="processed",
        commitments=[
            Commitment(
                commitment_mention="schedule kickoff call",
                description="Schedule the kickoff call with the vendor.",
                status="open",
                status_confirmed_by_user=False,
            )
        ],
    )
    summary = apply_extraction_result(conn, "cap_w004", result)
    conn.commit()

    assert summary.commitments_created == 1
    row = conn.execute(
        "SELECT commitment_id FROM commitments WHERE commitment_mention='schedule kickoff call'"
    ).fetchone()
    assert row is not None


# ---------------------------------------------------------------------------
# Relationships (within-capture resolution)
# ---------------------------------------------------------------------------

def test_relationship_resolves_within_same_capture(conn):
    _insert_test_capture(conn, "cap_w005")
    result = ExtractionResult(
        capture_id="cap_w005",
        extraction_status="processed",
        events=[
            Event(entity_mention="a new gadget", event_type="problem_encountered", description="widget overheats under load"),
            Event(entity_mention="a new gadget", event_type="resolution_found", description="added a heatsink"),
        ],
        relationships=[
            Relationship(
                source_mention="problem_encountered",
                target_mention="resolution_found",
                relationship_type="resolves",
            )
        ],
    )
    vocab = VocabLogger()
    summary = apply_extraction_result(conn, "cap_w005", result, vocab)
    conn.commit()

    assert summary.relationships_resolved == 1
    assert summary.relationships_skipped == 0
    assert vocab.relationship_types["resolves"] == 1
    assert vocab.event_types["problem_encountered"] == 1


def test_relationship_to_unresolvable_mention_is_skipped_not_crashed(conn):
    """A relationship referencing something not produced by this capture
    should be skipped with a warning, never raise."""
    _insert_test_capture(conn, "cap_w006")
    result = ExtractionResult(
        capture_id="cap_w006",
        extraction_status="processed",
        commitments=[
            Commitment(
                commitment_mention="draft the proposal",
                description="Draft the client proposal.",
                status="open",
                status_confirmed_by_user=False,
            )
        ],
        relationships=[
            Relationship(
                source_mention="draft the proposal",
                target_mention="something mentioned three weeks ago",  # not in this capture
                relationship_type="must_precede",
            )
        ],
    )
    summary = apply_extraction_result(conn, "cap_w006", result)
    conn.commit()

    assert summary.relationships_resolved == 0
    assert summary.relationships_skipped == 1
    assert len(summary.warnings) == 1


# ---------------------------------------------------------------------------
# Discarded captures write nothing
# ---------------------------------------------------------------------------

def test_discarded_capture_writes_nothing(conn):
    _insert_test_capture(conn, "cap_w007")
    result = ExtractionResult(
        capture_id="cap_w007",
        extraction_status="transient_discard",
        discard_reason="casual chatter",
    )
    before = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("declarative_facts", "episodic_events", "commitments", "relationships")
    }
    summary = apply_extraction_result(conn, "cap_w007", result)
    conn.commit()
    after = {
        t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        for t in ("declarative_facts", "episodic_events", "commitments", "relationships")
    }
    assert before == after
    assert summary.facts_inserted == 0
