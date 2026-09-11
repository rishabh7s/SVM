"""
The deterministic, SQL-backed tool suite for the "Atomic Neighborhood
Tools" retrieval architecture. No LLM calls happen anywhere in this file --
every function is pure, testable Python that behaves identically every time
given the same database state. The agent's ReAct loop (kivi/retrieval/agent.py)
is the only place model output influences behavior.

This replaces the earlier single macro-tool (retrieve_entity_context_with_
provenance), which bundled entity resolution + data fetch + provenance join
+ graph traversal into one hardcoded call. That reduced latency but meant
the LLM could only ever see exactly the shape of context the macro-tool's
author decided to bundle -- it couldn't, for instance, check who asserted a
fact, follow a resolution's OWN resolution, or decide for itself whether a
superseded value mattered to the question being asked. The tools below
instead expose the underlying graph directly: find a starting node, inspect
it, walk its edges, or pull its version history, one atomic step at a time.
This intentionally trades latency (more round trips) for investigative
flexibility (the model decides how far to explore, not a hardcoded bundler).

Node ID convention: every node_id carries a type prefix set at creation time
elsewhere in the codebase (fact_ / evt_ / com_ / ent_ -- see
kivi/ingestion/writer.py and kivi/ingestion/entity_resolution.py). All four
tools below dispatch on this prefix rather than requiring a separate
node_type parameter, since search_nodes already returns node_id values in
this form and the LLM only ever needs to pass one identifier through the
whole exploration.

Tools:
  - search_nodes         -- FTS5 (Porter-stemmed) entry point: text -> candidate nodes
  - get_node_details      -- full row + provenance for one specific node
  - get_connected_edges   -- relationships table traversal, either direction, optional type filter
  - get_node_history      -- version history: fact supersession chain, or commitment status chain
  - compute                -- deterministic arithmetic, unrelated to graph traversal but still needed
  - get_events_in_window   -- temporal slicing ("what happened last week"), also orthogonal to graph traversal
  - save_memory            -- proactive write: durably persists ONE fact asserted mid-conversation
                               (see save_memory's own docstring below for why this is deliberately
                               narrower than the full ingestion pipeline)
"""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from kivi.ingestion.entity_resolution import get_l1_context, resolve_entity

NodeType = Literal["entity", "fact", "event", "commitment", "preference"]

_PREFIX_TO_TYPE: dict[str, NodeType] = {
    "ent_": "entity",
    "fact_": "fact",
    "evt_": "event",
    "com_": "commitment",
    "pref_": "preference",
}


def _node_type_from_id(node_id: str) -> Optional[NodeType]:
    for prefix, node_type in _PREFIX_TO_TYPE.items():
        if node_id.startswith(prefix):
            return node_type
    return None
node_type_from_id = _node_type_from_id


def _is_deleted(conn: sqlite3.Connection, node_type: str, node_id: str) -> bool:
    """True if the given fact/event/commitment has deleted_at set (see
    kivi/api/memory_ops.py's delete_memory). deleted_at is the ONLY
    soft-delete column on these tables (db/schema.sql) -- there is no
    separate boolean is_deleted or deleted_reason column. Entities are
    never deletable through that operation, so always False for 'entity'."""
    table = {
        "fact": "declarative_facts",
        "event": "episodic_events",
        "commitment": "commitments",
        "preference": "preferences",
    }.get(node_type)
    if table is None:
        return False
    id_col = {
        "fact": "fact_id",
        "event": "event_id",
        "commitment": "commitment_id",
        "preference": "preference_id",
    }[node_type]
    row = conn.execute(f"SELECT deleted_at FROM {table} WHERE {id_col} = ?", (node_id,)).fetchone()
    return bool(row and row["deleted_at"] is not None)

# ---------------------------------------------------------------------------
# 1. search_nodes -- the entry point into the graph
# ---------------------------------------------------------------------------

def search_nodes(conn: sqlite3.Connection, query: str, limit: int = 10) -> list[dict]:
    """FTS5 (Porter-stemmed) search across entities, facts, events, and
    commitments in one call. Returns a lightweight list -- node_id,
    node_type, and a short snippet -- so the LLM can decide which node(s)
    are worth a full get_node_details call, without paying for full detail
    fetches on every candidate.

    Query terms are stripped of punctuation before being sent to FTS5 --
    FTS5's query syntax gives special meaning to characters like '-' (an
    embedded hyphen in a bareword, e.g. 'fixed-step', is parsed as an
    operator and raises a SQL error rather than matching literally), even
    though the tokenizer itself splits 'fixed-step' into 'fixed' and 'step'
    at INDEX time. Terms are OR-joined so a multi-word query doesn't require
    every word to appear in the same row.
    """
    if not query or not query.strip():
        return []

    cleaned = re.sub(r"[^a-z0-9]+", " ", query.lower())
    terms = [t for t in cleaned.split() if t]
    if not terms:
        return []
    fts_query = " OR ".join(terms)

    try:
        rows = conn.execute(
            "SELECT source_type, source_id, content, bm25(unified_search) AS rank "
            "FROM unified_search WHERE unified_search MATCH ? "
            "ORDER BY rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    # dedupe -- an entity can match on multiple alias rows; keep only its
    # best-ranked (first, since already ORDER BY rank) occurrence
    seen: set[tuple[str, str]] = set()
    results = []
    for row in rows:
        key = (row["source_type"], row["source_id"])
        if key in seen:
            continue
        seen.add(key)
        if row["source_type"] != "entity" and _is_deleted(conn, row["source_type"], row["source_id"]):
            # A deleted memory shouldn't surface as a starting point for
            # further exploration -- see memory_ops.delete_memory's
            # docstring. The row itself is untouched (soft delete only), so
            # it stays reachable directly via get_node_details for audit.
            continue
        snippet = row["content"]
        results.append(
            {
                "node_id": row["source_id"],
                "node_type": row["source_type"],
                "snippet": snippet[:200],
            }
        )
    return results


# ---------------------------------------------------------------------------
# 2. get_node_details -- full row + provenance for one node
# ---------------------------------------------------------------------------

def get_node_details(conn: sqlite3.Connection, node_id: str) -> dict:
    """Fetches the full stored details for one node, dispatched by its ID
    prefix. Facts and events and commitments always include their source
    capture's provenance (capture_id, captured_at, foreground_app,
    formatted_text) joined in, so a citation never requires a separate
    lookup. Returns {"error": ...} for an unrecognized prefix or a node_id
    that doesn't exist -- never raises, so a bad ID from the model is just
    another tool result it can react to.

    Deliberately does NOT auto-resolve a superseded fact to its current
    value, and does NOT auto-follow a commitment's status history -- this
    tool answers "what does this exact node say," including its own
    is_active / superseded_by_id / current-status pointers where relevant,
    so the LLM can decide for itself whether to follow those pointers via
    get_node_history or a further get_node_details call. Auto-resolving
    here would silently reintroduce the macro-tool's hardcoded judgment
    calls that this redesign exists to remove.
    """
    node_type = _node_type_from_id(node_id)
    if node_type is None:
        return {"error": f"unrecognized node_id format: '{node_id}'"}

    if node_type == "entity":
        entity = conn.execute(
            "SELECT entity_id, canonical_name, entity_type, created_at FROM entities WHERE entity_id = ?",
            (node_id,),
        ).fetchone()
        if not entity:
            return {"error": f"no entity found with id '{node_id}'"}
        aliases = conn.execute(
            "SELECT alias, source_capture_id, created_at FROM entity_aliases WHERE entity_id = ? ORDER BY created_at ASC",
            (node_id,),
        ).fetchall()
        return {"node_type": "entity", **dict(entity), "aliases": [dict(a) for a in aliases]}

    if node_type == "fact":
        row = conn.execute(
            "SELECT f.fact_id, f.entity_id, f.attribute, f.value_text, f.value_numeric, f.unit, "
            "       f.precision_class, f.asserter_role, f.relative_time_expression, f.resolved_time, "
            "       f.is_active, f.superseded_by_id, f.deleted_at, f.created_at, "
            "       c.capture_id, c.captured_at, c.foreground_app, c.formatted_text "
            "FROM declarative_facts f "
            "JOIN captures c ON c.capture_id = f.source_capture_id "
            "WHERE f.fact_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"error": f"no fact found with id '{node_id}'"}
        return {"node_type": "fact", **dict(row)}

    if node_type == "event":
        row = conn.execute(
            "SELECT e.event_id, e.entity_id, e.event_type, e.description, "
            "       e.relative_time_expression, e.resolved_time, e.asserter_role, e.deleted_at, e.created_at, "
            "       c.capture_id, c.captured_at, c.foreground_app, c.formatted_text "
            "FROM episodic_events e "
            "JOIN captures c ON c.capture_id = e.source_capture_id "
            "WHERE e.event_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"error": f"no event found with id '{node_id}'"}
        return {"node_type": "event", **dict(row)}

    if node_type == "commitment":
        row = conn.execute(
            "SELECT co.commitment_id, co.entity_id, co.commitment_mention, co.description, co.deleted_at, co.created_at, "
            "       cse.status, cse.status_confirmed_by_user, cse.blocking_reason, "
            "       cse.due_date_relative_expression, cse.due_date_resolved, "
            "       c.capture_id, c.captured_at, c.foreground_app, c.formatted_text "
            "FROM commitments co "
            "JOIN captures c ON c.capture_id = co.source_capture_id "
            "LEFT JOIN commitment_status_events cse ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
            "WHERE co.commitment_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"error": f"no commitment found with id '{node_id}'"}
        return {"node_type": "commitment", **dict(row)}

    if node_type == "preference":
        row = conn.execute(
            "SELECT p.preference_id, p.entity_id, p.category, p.preference_text, "
            "       p.is_active, p.superseded_by_id, p.deleted_at, p.created_at, "
            "       c.capture_id, c.captured_at, c.foreground_app, c.formatted_text "
            "FROM preferences p "
            "JOIN captures c ON c.capture_id = p.source_capture_id "
            "WHERE p.preference_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return {"error": f"no preference found with id '{node_id}'"}
        return {"node_type": "preference", **dict(row)}

    return {"error": f"unhandled node_type: {node_type}"}  # unreachable given _PREFIX_TO_TYPE, kept defensive


# ---------------------------------------------------------------------------
# 3. get_connected_edges -- relationships table traversal
# ---------------------------------------------------------------------------

def _snippet_for_node(conn: sqlite3.Connection, node_type: str, node_id: str) -> Optional[str]:
    """Short human-readable text for a connected node, without a full
    get_node_details round trip -- mirrors search_nodes' snippet shape."""
    if node_type == "event":
        row = conn.execute("SELECT description FROM episodic_events WHERE event_id = ?", (node_id,)).fetchone()
        return row["description"] if row else None
    if node_type == "fact":
        row = conn.execute(
            "SELECT attribute, value_text, value_numeric FROM declarative_facts WHERE fact_id = ?", (node_id,)
        ).fetchone()
        if not row:
            return None
        value = row["value_text"] if row["value_text"] is not None else row["value_numeric"]
        return f"{row['attribute']}: {value}"
    if node_type == "commitment":
        row = conn.execute(
            "SELECT commitment_mention FROM commitments WHERE commitment_id = ?", (node_id,)
        ).fetchone()
        return row["commitment_mention"] if row else None
    if node_type == "preference":
        row = conn.execute(
            "SELECT preference_text FROM preferences WHERE preference_id = ?", (node_id,)
        ).fetchone()
        return row["preference_text"] if row else None
    return None


def get_connected_edges(
    conn: sqlite3.Connection, source_node_id: str, edge_type: Optional[str] = None
) -> list[dict]:
    """Returns every relationship touching source_node_id, in EITHER
    direction (the relationships table records edges with an explicit
    source/target, but a 'resolves' edge is equally worth finding whether
    the node you have is the problem or the resolution). Each result
    includes the connected node's id, type, a short snippet, the
    relationship_type, and which direction the edge runs relative to
    source_node_id ('outgoing' if source_node_id is the edge's source,
    'incoming' if it's the target).

    edge_type, if given, filters case-insensitively as a substring match
    against relationship_type -- e.g. edge_type='resolve' matches
    'resolves', 'resolution_of', etc., since relationship_type is free text
    with no fixed vocabulary (see kivi_extraction_schema_v2.json).

    Only fact/event/commitment nodes participate in the relationships
    table -- entities don't have edges of their own in this schema, so this
    returns an empty list for an entity node_id rather than an error.
    """
    node_type = _node_type_from_id(source_node_id)
    if node_type is None:
        return []
    if node_type == "entity":
        return []

    query = (
        "SELECT relationship_id, source_type, source_id, target_type, target_id, relationship_type, reason "
        "FROM relationships WHERE ((source_type = ? AND source_id = ?) OR (target_type = ? AND target_id = ?))"
    )
    params: list = [node_type, source_node_id, node_type, source_node_id]
    if edge_type:
        query += " AND LOWER(relationship_type) LIKE LOWER(?)"
        params.append(f"%{edge_type}%")

    rows = conn.execute(query, params).fetchall()

    results = []
    for row in rows:
        is_outgoing = row["source_id"] == source_node_id and row["source_type"] == node_type
        connected_type = row["target_type"] if is_outgoing else row["source_type"]
        connected_id = row["target_id"] if is_outgoing else row["source_id"]
        results.append(
            {
                "relationship_id": row["relationship_id"],
                "relationship_type": row["relationship_type"],
                "direction": "outgoing" if is_outgoing else "incoming",
                "reason": row["reason"],
                "connected_node_id": connected_id,
                "connected_node_type": connected_type,
                "connected_snippet": _snippet_for_node(conn, connected_type, connected_id),
            }
        )
    return results


# ---------------------------------------------------------------------------
# 4. get_node_history -- version history
# ---------------------------------------------------------------------------

def get_node_history(conn: sqlite3.Connection, node_id: str) -> dict:
    """Returns the full version history for a node, oldest to newest:
    - fact: every row ever recorded for that fact's (entity_id, attribute),
      i.e. the full supersession chain, regardless of which specific
      fact_id in that chain was passed in.
    - commitment: every commitment_status_events row for that commitment_id.
    - event / entity: these aren't versioned in this schema (an event is a
      point-in-time occurrence; an entity's identity doesn't change), so
      this returns an empty history with a note explaining why, rather than
      an error -- "no history exists" is a valid, different answer from
      "this node doesn't exist."
    """
    node_type = _node_type_from_id(node_id)
    if node_type is None:
        return {"error": f"unrecognized node_id format: '{node_id}'"}

    if node_type == "fact":
        anchor = conn.execute(
            "SELECT entity_id, attribute FROM declarative_facts WHERE fact_id = ?", (node_id,)
        ).fetchone()
        if not anchor:
            return {"error": f"no fact found with id '{node_id}'"}
        rows = conn.execute(
            "SELECT fact_id, value_text, value_numeric, unit, is_active, superseded_by_id, "
            "       deleted_at, source_capture_id, created_at "
            "FROM declarative_facts WHERE entity_id = ? AND attribute = ? ORDER BY created_at ASC",
            (anchor["entity_id"], anchor["attribute"]),
        ).fetchall()
        return {"node_type": "fact", "history": [dict(r) for r in rows]}

    if node_type == "commitment":
        exists = conn.execute("SELECT 1 FROM commitments WHERE commitment_id = ?", (node_id,)).fetchone()
        if not exists:
            return {"error": f"no commitment found with id '{node_id}'"}
        rows = conn.execute(
            "SELECT status_event_id, status, status_confirmed_by_user, blocking_reason, "
            "       is_active, superseded_by_id, source_capture_id, created_at "
            "FROM commitment_status_events WHERE commitment_id = ? ORDER BY created_at ASC",
            (node_id,),
        ).fetchall()
        return {"node_type": "commitment", "history": [dict(r) for r in rows]}

    if node_type == "preference":
        anchor = conn.execute(
            "SELECT entity_id, category FROM preferences WHERE preference_id = ?", (node_id,)
        ).fetchone()
        if not anchor:
            return {"error": f"no preference found with id '{node_id}'"}
        if anchor["entity_id"] is None or anchor["category"] is None:
            # Unscoped preferences (no entity_id/category) are never
            # superseded -- each is its own standalone row, mirroring
            # kivi/ingestion/writer.py's append-only rule for that case --
            # so "history" here is just this one row, not an error.
            row = conn.execute(
                "SELECT preference_id, preference_text, is_active, superseded_by_id, "
                "       deleted_at, source_capture_id, created_at "
                "FROM preferences WHERE preference_id = ?",
                (node_id,),
            ).fetchone()
            return {"node_type": "preference", "history": [dict(row)]}
        rows = conn.execute(
            "SELECT preference_id, preference_text, is_active, superseded_by_id, "
            "       deleted_at, source_capture_id, created_at "
            "FROM preferences WHERE entity_id = ? AND category = ? ORDER BY created_at ASC",
            (anchor["entity_id"], anchor["category"]),
        ).fetchall()
        return {"node_type": "preference", "history": [dict(r) for r in rows]}

    if node_type in ("event", "entity"):
        return {"node_type": node_type, "history": [], "note": f"{node_type} nodes are not versioned in this schema"}

    return {"error": f"unhandled node_type: {node_type}"}  # unreachable given _PREFIX_TO_TYPE, kept defensive


# ---------------------------------------------------------------------------
# Orthogonal tools, kept from the earlier design -- neither is a graph
# traversal, so neither fits the search/details/edges/history shape above.
# ---------------------------------------------------------------------------

def compute(operation: Literal["sum", "difference", "average", "multiply", "divide"], values: list[float]) -> float:
    """Deterministic arithmetic. The agent must route ANY calculation
    through this tool rather than computing a total in free-form text
    generation -- this is what makes a multi-hop numeric answer auditable
    rather than a number the model happened to produce."""
    if not values:
        raise ValueError("compute() requires at least one value")
    if operation == "sum":
        return sum(values)
    if operation == "difference":
        result = values[0]
        for v in values[1:]:
            result -= v
        return result
    if operation == "average":
        return sum(values) / len(values)
    if operation == "multiply":
        result = 1.0
        for v in values:
            result *= v
        return result
    if operation == "divide":
        result = values[0]
        for v in values[1:]:
            result /= v
        return result
    raise ValueError(f"unknown operation: {operation}")


def get_events_in_window(
    conn: sqlite3.Connection, start: str, end: str, entity_id: Optional[str] = None
) -> list[dict]:
    """Temporal slicing -- events whose resolved_time OR created_at falls
    in [start, end]. entity_id filters to one entity; omit for a
    cross-entity slice ('what happened last week'). This is a time-range
    query, not a graph traversal from a known starting node, which is why
    it doesn't fit the search/details/edges/history shape -- kept
    separately for questions with no specific entity or node to start from."""
    query = (
        "SELECT * FROM episodic_events "
        "WHERE COALESCE(resolved_time, created_at) BETWEEN ? AND ?"
    )
    params: list = [start, end]
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    query += " ORDER BY COALESCE(resolved_time, created_at) ASC"
    rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 5. save_memory -- proactive write during conversation
# ---------------------------------------------------------------------------

def save_memory(
    conn: sqlite3.Connection,
    entity: str,
    attribute: str,
    value: str,
    context: Optional[str] = None,
) -> str:
    """Durably persists ONE explicit fact the user asserted mid-conversation
    -- "David is now our tech lead" becomes
    save_memory(entity="David", attribute="role", value="tech lead"). This is
    the AGENT'S proactive counterpart to the batch ingestion pipeline
    (kivi/ingestion/pipeline.py): same underlying write path (entity
    resolution via kivi/ingestion/entity_resolution.py, then the exact
    three-step supersession order used by kivi/ingestion/writer.py's
    _write_fact -- deactivate old active row -> insert new -> point old at
    new, since the DB's own partial unique index checks this immediately,
    not deferred), but triggered synchronously inside the tool-calling loop
    instead of an offline batch run, and WITHOUT an LLM extraction call in
    the middle -- the agent has already done the "what is the fact here"
    judgment call itself by deciding to invoke this tool with these
    specific arguments, so there's nothing left for a second model call to
    extract.

    Every save is backed by a real capture row (source_modality=
    'manual_edit', matching the same honest-provenance convention
    kivi/api/memory_ops.py uses for REST-driven edits) so a citation for
    this memory later will correctly show it came from a conversational
    save, not a dictation that never happened. `context`, if given, becomes
    that capture's formatted_text -- the closest thing to "what was
    actually said" available to this tool, since the agent only passes
    structured arguments through, not the raw turn.

    Returns a plain-language, human-readable STRING (not a dict, unlike
    update_memory/delete_memory) -- this is fed straight back into the
    agent's tool-result message, so it needs to read naturally as something
    the agent can quote or paraphrase directly in its final answer.

    Deliberately narrow, on purpose:
      - No entity_type parameter -- a newly-created entity gets
        entity_type='unspecified'. This tool trades a little schema
        precision for a signature simple enough for the model to call
        reliably; entity_type barely matters for retrieval anyway, since
        search_nodes and entity resolution both match on name/alias text,
        not type.
      - No attribute vocabulary enforcement -- `attribute` is free text,
        consistent with the rest of this schema's "generalized" design
        (see db/schema.sql's own comments on attribute/event_type/
        relationship_type all being open text on purpose).
      - Exactly one fact per call. The agent should call this once per
        distinct fact in a turn, not try to encode multiple updates into
        one call -- keeps each write's provenance and audit trail
        (memory_mutation_log-equivalent: this capture row + the fact's own
        source_capture_id) unambiguous.
    """
    entity = (entity or "").strip()
    attribute = (attribute or "").strip()
    value = (value or "").strip()
    if not entity:
        return "save_memory failed -- 'entity' was empty; nothing was written."
    if not attribute:
        return "save_memory failed -- 'attribute' was empty; nothing was written."
    if not value:
        return "save_memory failed -- 'value' was empty; nothing was written."

    try:
        capture_id = f"cap_{uuid.uuid4().hex[:12]}"
        captured_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        provenance_text = context.strip() if context and context.strip() else f"{entity} -- {attribute}: {value}"

        conn.execute(
            "INSERT INTO captures "
            "(capture_id, raw_asr_text, formatted_text, source_modality, foreground_app, window_title, "
            " captured_at, extraction_status) "
            "VALUES (?, NULL, ?, 'manual_edit', 'Hey Kivi', 'Conversational Save', ?, 'processed')",
            (capture_id, provenance_text, captured_at),
        )

        l1_context = get_l1_context(conn)
        resolution = resolve_entity(
            conn, entity, entity_type="unspecified", source_capture_id=capture_id, l1_context=l1_context
        )

        existing = conn.execute(
            "SELECT * FROM declarative_facts WHERE entity_id = ? AND attribute = ? AND is_active = 1",
            (resolution.entity_id, attribute),
        ).fetchone()

        if existing and existing["value_text"] == value:
            conn.commit()
            return f"No change needed -- {entity}'s {attribute} was already recorded as '{value}' ({existing['fact_id']})."

        new_fact_id = f"fact_{uuid.uuid4().hex[:12]}"

        if existing:
            # Same ordering constraint as kivi/ingestion/writer.py's
            # _write_fact: deactivate the old active row before inserting
            # the new one, since the partial unique index on
            # (entity_id, attribute) WHERE is_active=1 is checked
            # immediately, not deferred to commit.
            conn.execute("UPDATE declarative_facts SET is_active = 0 WHERE fact_id = ?", (existing["fact_id"],))

        conn.execute(
            """
            INSERT INTO declarative_facts
                (fact_id, entity_id, attribute, value_text, value_numeric, unit,
                 precision_class, asserter_role, relative_time_expression, resolved_time,
                 source_capture_id, is_active, superseded_by_id)
            VALUES (?, ?, ?, ?, NULL, NULL, 'exact_source', 'self', NULL, NULL, ?, 1, NULL)
            """,
            (new_fact_id, resolution.entity_id, attribute, value, capture_id),
        )

        if existing:
            # Old row can only point at the new one AFTER the new row
            # exists -- the FK on superseded_by_id requires it.
            conn.execute(
                "UPDATE declarative_facts SET superseded_by_id = ? WHERE fact_id = ?",
                (new_fact_id, existing["fact_id"]),
            )

        conn.commit()

        if existing:
            return (
                f"Saved. Updated {entity}'s {attribute} to '{value}' "
                f"(fact_id={new_fact_id}, supersedes {existing['fact_id']})."
            )
        return (
            f"Saved. Recorded that {entity}'s {attribute} is '{value}' "
            f"(fact_id={new_fact_id}, entity_id={resolution.entity_id}, "
            f"entity_resolution={resolution.method})."
        )

    except Exception as e:  # noqa: BLE001 -- a partial write must never be left committed
        conn.rollback()
        return f"save_memory failed -- nothing was written due to an error: {e}"
