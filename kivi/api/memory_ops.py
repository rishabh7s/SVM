"""Update and soft-delete. No LLM in this file.

Both entry points run this same code: the REST endpoints in
kivi/api/app.py and the agent's tools. That shared path is what actually
guarantees a spoken "forget that" can't do anything the delete button
can't.

Nothing is updated in place and nothing is removed. An update supersedes;
a delete sets deleted_at and clears is_active. Callers own the transaction.
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
    """Thin passthrough to get_node_details -- kept as its own function so the
    REST layer and the agent's tools both go through kivi/api/memory_ops.py
    for every memory operation, not just the mutating ones."""
    return get_node_details(conn, memory_id)


# ---------------------------------------------------------------------------
# Update (supersession for facts/commitments, correction for events)
# ---------------------------------------------------------------------------

def update_memory(conn: sqlite3.Connection, memory_id: str, updates: dict[str, Any], note: Optional[str] = None) -> dict:
    """Applies a correction to a memory."""
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
    # Deleted before superseded: a delete also clears is_active, so the other
    # order reports a deleted fact as "superseded by 'None'".
    if current["deleted_at"] is not None:
        return {"error": f"fact '{fact_id}' has been deleted; updating a deleted memory is not supported"}
    if not current["is_active"]:
        return {
            "error": (
                f"fact '{fact_id}' is not the current version (it was superseded by "
                f"'{current['superseded_by_id']}') -- update the current version instead"
            )
        }

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

_DELETABLE_TABLES = {
    "fact": ("declarative_facts", "fact_id"),
    "event": ("episodic_events", "event_id"),
    "commitment": ("commitments", "commitment_id"),
    # preferences were missing here -- a pref_ id raised KeyError, which the
    # agent then paraphrased back as success
    "preference": ("preferences", "preference_id"),
}

# Tables that also carry is_active, which a delete must clear -- see the
# docstring below for why deleting now deactivates as well as marks.
_HAS_IS_ACTIVE = {"fact", "preference"}


def delete_memory(conn: sqlite3.Connection, memory_id: str, reason: Optional[str] = None) -> dict:
    """Soft-deletes a fact, event, commitment, or preference: sets deleted_at
    (and clears is_active where that column exists), never removes the row."""
    node_type = node_type_from_id(memory_id)
    if node_type is None:
        return {"error": f"unrecognized memory_id format: '{memory_id}'"}
    if node_type == "entity":
        return {"error": "entities cannot be deleted through this operation (see module docstring)"}
    if node_type not in _DELETABLE_TABLES:
        return {"error": f"memories of type '{node_type}' cannot be deleted through this operation"}

    table, id_col = _DELETABLE_TABLES[node_type]

    row = conn.execute(f"SELECT deleted_at FROM {table} WHERE {id_col} = ?", (memory_id,)).fetchone()
    if row is None:
        return {"error": f"no {node_type} found with id '{memory_id}'"}
    if row["deleted_at"] is not None:
        return {"error": f"{node_type} '{memory_id}' is already deleted (deleted_at={row['deleted_at']})"}

    deleted_at = _now_iso()
    if node_type in _HAS_IS_ACTIVE:
        conn.execute(
            f"UPDATE {table} SET deleted_at = ?, is_active = 0 WHERE {id_col} = ?",
            (deleted_at, memory_id),
        )
    else:
        conn.execute(f"UPDATE {table} SET deleted_at = ? WHERE {id_col} = ?", (deleted_at, memory_id))

    # Status lives in commitment_status_events, so retire that too. Otherwise
    # the deleted commitment still reads as open through the join.
    if node_type == "commitment":
        conn.execute(
            "UPDATE commitment_status_events SET is_active = 0 "
            "WHERE commitment_id = ? AND is_active = 1",
            (memory_id,),
        )

    return {
        "memory_id": memory_id,
        "node_type": node_type,
        "deleted": True,
        "deleted_at": deleted_at,
        "reason": reason,
    }
