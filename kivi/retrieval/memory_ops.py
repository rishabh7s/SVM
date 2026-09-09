"""
Deterministic memory management -- update and soft-delete, with no LLM
involvement anywhere in this file. This is the single implementation behind
BOTH entry points the dual-path requirement asks for:

  - the REST endpoints in kivi/api/app.py (direct UI actions)
  - the agent tools registered in kivi/retrieval/agent.py (conversational
    actions, triggered by tool-calling)

Both paths call the exact same functions below. This is what actually
guarantees "conversational modification can't do anything the deterministic
REST path couldn't" -- not a documentation claim, a shared code path. If the
UI's delete button and the agent's delete_memory tool ever diverged in
behavior, that would be a bug in this design; there is structurally no way
for that behavior to be defined twice.

Design decisions worth being explicit about:
  - update_memory() never mutates a row in place. For a fact or commitment,
    it performs the exact same supersession dance as kivi/ingestion/writer.py
    (new active row, old row deactivated and pointed at the new one) --
    the only difference is the source is a synthetic 'manual_edit' capture
    instead of a dictation. For an event (which has no supersession concept
    in this schema -- an event is a point-in-time occurrence, not a value
    that changes), an update instead soft-deletes the old event and inserts
    a corrected one, linked by a 'corrects' relationship -- reusing the
    existing graph machinery rather than inventing event-versioning.
  - delete_memory() only ever sets deleted_at; the row is never removed
    from the table. Entities cannot be deleted through this module -- doing
    so would orphan every fact/event/commitment attached to them, which is
    a different, larger operation this narrow implementation doesn't
    attempt (see the module-level TODO below).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from kivi.retrieval.tools import get_node_details, node_type_from_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _create_manual_edit_capture(conn: sqlite3.Connection, note: Optional[str]) -> str:
    """Every manual edit is backed by a real, honest capture row --
    source_modality='manual_edit', never mislabeled as speech or
    selected_text -- so provenance for the resulting memory stays accurate:
    a citation for this memory will correctly show it came from a manual
    edit, not a dictation that never happened."""
    capture_id = _new_id("cap")
    conn.execute(
        "INSERT INTO captures "
        "(capture_id, raw_asr_text, formatted_text, source_modality, foreground_app, window_title, captured_at, extraction_status) "
        "VALUES (?, NULL, ?, 'manual_edit', 'Kivi API', 'Memory Editor', ?, 'processed')",
        (capture_id, note, _now_iso()),
    )
    return capture_id


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_memory(conn: sqlite3.Connection, memory_id: str) -> dict:
    """Thin passthrough to get_node_details -- kept as its own function so
    the REST layer and the agent's tools both go through kivi/api/memory_ops.py
    for every memory operation, not just the mutating ones."""
    return get_node_details(conn, memory_id)


# ---------------------------------------------------------------------------
# Update (supersession for facts/commitments, correction for events)
# ---------------------------------------------------------------------------

def update_memory(conn: sqlite3.Connection, memory_id: str, updates: dict[str, Any], note: Optional[str] = None) -> dict:
    """Applies a correction to a memory. Never mutates the target row.

    updates' allowed keys depend on node_type:
      - fact: value_text, value_numeric, unit
      - commitment: status, status_confirmed_by_user, blocking_reason,
        due_date_relative_expression, due_date_resolved
      - event: description (creates a new corrected event + a 'corrects'
        edge back to the original, which is soft-deleted)
    Returns the new/updated node's full details (via get_node_details) on
    success, or {"error": ...} -- never raises.
    """
    node_type = node_type_from_id(memory_id)
    if node_type is None:
        return {"error": f"unrecognized memory_id format: '{memory_id}'"}
    if node_type == "entity":
        return {"error": "entities cannot be updated through this operation; update the associated facts instead"}

    if node_type == "fact":
        return _update_fact(conn, memory_id, updates, note)
    if node_type == "event":
        return _update_event(conn, memory_id, updates, note)
    if node_type == "commitment":
        return _update_commitment(conn, memory_id, updates, note)

    return {"error": f"unhandled node_type: {node_type}"}  # unreachable given node_type_from_id, kept defensive


def _update_fact(conn: sqlite3.Connection, fact_id: str, updates: dict[str, Any], note: Optional[str]) -> dict:
    current = conn.execute("SELECT * FROM declarative_facts WHERE fact_id = ?", (fact_id,)).fetchone()
    if not current:
        return {"error": f"no fact found with id '{fact_id}'"}
    if not current["is_active"]:
        return {
            "error": (
                f"fact '{fact_id}' is not the current version (it was superseded by "
                f"'{current['superseded_by_id']}') -- update the current version instead"
            )
        }
    if current["deleted_at"] is not None:
        return {"error": f"fact '{fact_id}' has been deleted; updating a deleted memory is not supported"}

    allowed = {"value_text", "value_numeric", "unit"}
    unknown = set(updates) - allowed
    if unknown:
        return {"error": f"unsupported update field(s) for a fact: {sorted(unknown)}. Allowed: {sorted(allowed)}"}

    capture_id = _create_manual_edit_capture(conn, note)
    new_fact_id = _new_id("fact")

    conn.execute("UPDATE declarative_facts SET is_active = 0 WHERE fact_id = ?", (fact_id,))
    conn.execute(
        """
        INSERT INTO declarative_facts
            (fact_id, entity_id, attribute, value_text, value_numeric, unit,
             precision_class, asserter_role, relative_time_expression, resolved_time,
             source_capture_id, is_active, superseded_by_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        (
            new_fact_id,
            current["entity_id"],
            current["attribute"],
            updates.get("value_text", current["value_text"]),
            updates.get("value_numeric", current["value_numeric"]),
            updates.get("unit", current["unit"]),
            "exact_source",  # a direct manual edit is exact by definition, not a spoken approximation
            "self",
            None,
            None,
            capture_id,
        ),
    )
    conn.execute("UPDATE declarative_facts SET superseded_by_id = ? WHERE fact_id = ?", (new_fact_id, fact_id))

    return get_node_details(conn, new_fact_id)


def _update_commitment(conn: sqlite3.Connection, commitment_id: str, updates: dict[str, Any], note: Optional[str]) -> dict:
    commitment = conn.execute("SELECT * FROM commitments WHERE commitment_id = ?", (commitment_id,)).fetchone()
    if not commitment:
        return {"error": f"no commitment found with id '{commitment_id}'"}
    if commitment["deleted_at"] is not None:
        return {"error": f"commitment '{commitment_id}' has been deleted; updating a deleted memory is not supported"}

    allowed = {"status", "status_confirmed_by_user", "blocking_reason", "due_date_relative_expression", "due_date_resolved"}
    unknown = set(updates) - allowed
    if unknown:
        return {"error": f"unsupported update field(s) for a commitment: {sorted(unknown)}. Allowed: {sorted(allowed)}"}

    current_status = conn.execute(
        "SELECT * FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1", (commitment_id,)
    ).fetchone()

    new_status = updates.get("status", current_status["status"] if current_status else "open")
    new_confirmed = updates.get(
        "status_confirmed_by_user", bool(current_status["status_confirmed_by_user"]) if current_status else False
    )
    if new_status == "done" and not new_confirmed:
        return {"error": "status='done' requires status_confirmed_by_user=true -- refusing an unconfirmed completion"}
    if new_status == "blocked" and not updates.get("blocking_reason", current_status["blocking_reason"] if current_status else None):
        return {"error": "status='blocked' requires a blocking_reason"}

    capture_id = _create_manual_edit_capture(conn, note)
    new_status_id = _new_id("cse")

    if current_status:
        conn.execute("UPDATE commitment_status_events SET is_active = 0 WHERE status_event_id = ?", (current_status["status_event_id"],))

    conn.execute(
        """
        INSERT INTO commitment_status_events
            (status_event_id, commitment_id, status, status_confirmed_by_user, blocking_reason,
             due_date_relative_expression, due_date_resolved, source_capture_id, is_active, superseded_by_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, NULL)
        """,
        (
            new_status_id,
            commitment_id,
            new_status,
            int(bool(new_confirmed)),
            updates.get("blocking_reason", current_status["blocking_reason"] if current_status else None),
            updates.get("due_date_relative_expression", current_status["due_date_relative_expression"] if current_status else None),
            updates.get("due_date_resolved", current_status["due_date_resolved"] if current_status else None),
            capture_id,
        ),
    )
    if current_status:
        conn.execute(
            "UPDATE commitment_status_events SET superseded_by_id = ? WHERE status_event_id = ?",
            (new_status_id, current_status["status_event_id"]),
        )

    return get_node_details(conn, commitment_id)


def _update_event(conn: sqlite3.Connection, event_id: str, updates: dict[str, Any], note: Optional[str]) -> dict:
    current = conn.execute("SELECT * FROM episodic_events WHERE event_id = ?", (event_id,)).fetchone()
    if not current:
        return {"error": f"no event found with id '{event_id}'"}
    if current["deleted_at"] is not None:
        return {"error": f"event '{event_id}' has already been deleted; updating a deleted memory is not supported"}

    allowed = {"description"}
    unknown = set(updates) - allowed
    if unknown:
        return {"error": f"unsupported update field(s) for an event: {sorted(unknown)}. Allowed: {sorted(allowed)}"}
    if "description" not in updates or not updates["description"]:
        return {"error": "updating an event requires a non-empty 'description'"}

    capture_id = _create_manual_edit_capture(conn, note)
    new_event_id = _new_id("evt")

    conn.execute(
        "INSERT INTO episodic_events (event_id, entity_id, event_type, description, source_capture_id) "
        "VALUES (?, ?, ?, ?, ?)",
        (new_event_id, current["entity_id"], current["event_type"], updates["description"], capture_id),
    )
    conn.execute("UPDATE episodic_events SET deleted_at = ? WHERE event_id = ?", (_now_iso(), event_id))
    conn.execute(
        "INSERT INTO relationships (relationship_id, source_type, source_id, target_type, target_id, relationship_type, reason, source_capture_id) "
        "VALUES (?, 'event', ?, 'event', ?, 'corrects', ?, ?)",
        (_new_id("rel"), new_event_id, event_id, note or "manual correction", capture_id),
    )

    return get_node_details(conn, new_event_id)


# ---------------------------------------------------------------------------
# Delete (soft delete only -- see module docstring)
# ---------------------------------------------------------------------------

def delete_memory(conn: sqlite3.Connection, memory_id: str, reason: Optional[str] = None) -> dict:
    """Soft-deletes a fact, event, or commitment: sets deleted_at, never
    removes the row. The row remains fully visible via get_node_details and
    get_node_history for audit purposes; it's excluded from search_nodes
    and get_connected_edges going forward (see kivi/retrieval/tools.py).

    Entities are not deletable through this operation -- see the module
    docstring.

    TODO (out of scope for this pass): deleting an entity would need to
    decide what happens to every fact/event/commitment/relationship
    attached to it -- cascade-delete, orphan them, or refuse if any exist.
    That's a materially different, larger operation than deleting one
    memory, and isn't implemented here.
    """
    node_type = node_type_from_id(memory_id)
    if node_type is None:
        return {"error": f"unrecognized memory_id format: '{memory_id}'"}
    if node_type == "entity":
        return {"error": "entities cannot be deleted through this operation (see module docstring)"}

    table = {"fact": "declarative_facts", "event": "episodic_events", "commitment": "commitments"}[node_type]
    id_col = {"fact": "fact_id", "event": "event_id", "commitment": "commitment_id"}[node_type]

    row = conn.execute(f"SELECT deleted_at FROM {table} WHERE {id_col} = ?", (memory_id,)).fetchone()
    if row is None:
        return {"error": f"no {node_type} found with id '{memory_id}'"}
    if row["deleted_at"] is not None:
        return {"error": f"{node_type} '{memory_id}' is already deleted (deleted_at={row['deleted_at']})"}

    deleted_at = _now_iso()
    conn.execute(f"UPDATE {table} SET deleted_at = ? WHERE {id_col} = ?", (deleted_at, memory_id))

    return {
        "memory_id": memory_id,
        "node_type": node_type,
        "deleted": True,
        "deleted_at": deleted_at,
        "reason": reason,
    }
