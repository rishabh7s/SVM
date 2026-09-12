"""The retrieval tools. Plain SQL, no LLM anywhere in this file, so every one
of them returns the same thing given the same database.

search_nodes and get_entity_facts only return current rows (is_active=1,
deleted_at IS NULL). get_node_details and get_node_history do not filter -- showing exactly what one row says, flags included, is the
whole point of them.

Node ids carry a type prefix (fact_ / evt_ / com_ / pref_ / ent_) so the
tools can dispatch on the id alone.
"""

from __future__ import annotations

import math
import re
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from kivi.formatting import format_value
from kivi.ingestion.entity_resolution import get_l1_context, resolve_entity
from kivi.text_match import normalize

NodeType = Literal["entity", "fact", "event", "commitment", "preference"]

# Relevance floor for search_nodes. Terms are OR'd for recall, which means
# one incidental shared word ("machine" in "coffee machine broken") is
# enough for a hit -- and one hit is enough for the agent to build an answer
# around the wrong record. Below this floor, return nothing and let it
# abstain.
MIN_TERM_COVERAGE = 0.34
MIN_DISTINCTIVE_TERM_LENGTH = 3

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


_TABLE_FOR_TYPE: dict[str, str] = {
    "fact": "declarative_facts",
    "event": "episodic_events",
    "commitment": "commitments",
    "preference": "preferences",
}

_ID_COLUMN_FOR_TYPE: dict[str, str] = {
    "fact": "fact_id",
    "event": "event_id",
    "commitment": "commitment_id",
    "preference": "preference_id",
}

# Only these two carry is_active; an event is a point-in-time occurrence and
# a commitment's currency lives in commitment_status_events, not on the row.
_VERSIONED_TYPES = {"fact", "preference"}


def _is_deleted(conn: sqlite3.Connection, node_type: str, node_id: str) -> bool:
    """True if the given fact/event/commitment/preference has deleted_at set
    (see kivi/api/memory_ops.py's delete_memory)."""
    table = _TABLE_FOR_TYPE.get(node_type)
    if table is None:
        return False
    id_col = _ID_COLUMN_FOR_TYPE[node_type]
    row = conn.execute(f"SELECT deleted_at FROM {table} WHERE {id_col} = ?", (node_id,)).fetchone()
    return bool(row and row["deleted_at"] is not None)


def _is_superseded(conn: sqlite3.Connection, node_type: str, node_id: str) -> bool:
    """True if this row has been replaced by a newer version (is_active=0)."""
    if node_type not in _VERSIONED_TYPES:
        return False
    table = _TABLE_FOR_TYPE[node_type]
    id_col = _ID_COLUMN_FOR_TYPE[node_type]
    row = conn.execute(f"SELECT is_active FROM {table} WHERE {id_col} = ?", (node_id,)).fetchone()
    return bool(row and not row["is_active"])


def _commitment_status(conn: sqlite3.Connection, commitment_id: str) -> Optional[str]:
    """The commitment's CURRENT status, from its one active
    commitment_status_events row."""
    row = conn.execute(
        "SELECT status FROM commitment_status_events "
        "WHERE commitment_id = ? AND is_active = 1 "
        "ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (commitment_id,),
    ).fetchone()
    return row["status"] if row else None


def _term_coverage(query_terms: list[str], content: str) -> int:
    """How many of the query's terms this row's text actually contains."""
    content_tokens = set(normalize(content).split())
    if not content_tokens:
        return 0

    matched = 0
    for term in query_terms:
        if term in content_tokens:
            matched += 1
            continue
        # 'converging' should count against 'convergence'. Prefix match, same idea
        # as text_match but kept local so entity resolution isn't affected.
        if len(term) >= 5 and any(t.startswith(term[:5]) for t in content_tokens):
            matched += 1
    return matched


def _relevance_ok(query_terms: list[str], content: str) -> bool:
    """Post-MATCH relevance floor -- see MIN_TERM_COVERAGE above."""
    if not query_terms:
        return False

    matched = _term_coverage(query_terms, content)
    if matched == 0:
        return False

    required = max(1, math.ceil(len(query_terms) * MIN_TERM_COVERAGE))
    if matched >= required:
        return True

    # one distinctive term can still carry a short query ("Meridian")
    distinctive = [t for t in query_terms if len(t) >= MIN_DISTINCTIVE_TERM_LENGTH]
    content_tokens = set(normalize(content).split())
    return len(distinctive) <= 2 and any(t in content_tokens for t in distinctive)

# ---------------------------------------------------------------------------
# 1. search_nodes -- the entry point into the graph
# ---------------------------------------------------------------------------

def search_nodes(
    conn: sqlite3.Connection,
    query: str,
    limit: int = 10,
    include_superseded: bool = False,
) -> list[dict]:
    """FTS5 (Porter-stemmed) search across entities, facts, events,
    commitments, and preferences in one call."""
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
            # rowid tiebreak -- bm25 ties constantly, and a tie makes LIMIT arbitrary
            "ORDER BY rank ASC, rowid ASC "
            # Over-fetch, because the three filters below run in Python and
            # would otherwise thin a full page down to a handful of results.
            "LIMIT ?",
            (fts_query, max(limit * 5, limit)),
        ).fetchall()
    except sqlite3.OperationalError:
        return []

    # dedupe -- an entity can match on multiple alias rows; keep only its
    # best-ranked (first, since already ORDER BY rank) occurrence
    seen: set[tuple[str, str]] = set()
    scored: list[tuple[int, int, dict]] = []
    for position, row in enumerate(rows):
        key = (row["source_type"], row["source_id"])
        if key in seen:
            continue
        seen.add(key)

        node_type = row["source_type"]
        if node_type != "entity":
            if _is_deleted(conn, node_type, row["source_id"]):
                continue
            if not include_superseded and _is_superseded(conn, node_type, row["source_id"]):
                continue

        coverage = _term_coverage(terms, row["content"])
        if not _relevance_ok(terms, row["content"]):
            continue

        result = {
            "node_id": row["source_id"],
            "node_type": node_type,
            "snippet": row["content"][:200],
        }
        if node_type in _VERSIONED_TYPES:
            result["is_active"] = True if not include_superseded else not _is_superseded(
                conn, node_type, row["source_id"]
            )
        if node_type == "commitment":
            # Surfaced so a done commitment is never mistaken for
            # outstanding work just because it matched the query.
            result["status"] = _commitment_status(conn, row["source_id"])
        scored.append((coverage, position, result))

    # Rank by how much of the query each row covers, then by bm25. bm25 alone
    # scores a short row matching one common term above the row matching both
    # terms, and a small limit then cuts the one that was asked about.
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [result for _, _, result in scored[:limit]]


# ---------------------------------------------------------------------------
# 2. get_node_details -- full row + provenance for one node
# ---------------------------------------------------------------------------

def get_node_details(conn: sqlite3.Connection, node_id: str) -> dict:
    """Fetches the full stored details for one node, dispatched by its ID
    prefix."""
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
            "SELECT alias, source_capture_id, created_at FROM entity_aliases "
            "WHERE entity_id = ? ORDER BY created_at ASC, rowid ASC",
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
# 2b. get_entity_facts -- every CURRENT fact about one entity
# ---------------------------------------------------------------------------

def get_entity_facts(conn: sqlite3.Connection, entity_id: str) -> dict:
    """Returns the entity's complete CURRENT state: every active, non-deleted
    fact, plus its open commitments and active preferences."""
    entity = conn.execute(
        "SELECT entity_id, canonical_name, entity_type FROM entities WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()
    if not entity:
        return {"error": f"no entity found with id '{entity_id}'"}

    fact_rows = conn.execute(
        "SELECT f.fact_id, f.attribute, f.value_text, f.value_numeric, f.unit, "
        "       f.asserter_role, f.precision_class, f.resolved_time, f.created_at, "
        "       c.capture_id, c.captured_at, c.foreground_app, c.formatted_text "
        "FROM declarative_facts f "
        "JOIN captures c ON c.capture_id = f.source_capture_id "
        "WHERE f.entity_id = ? AND f.is_active = 1 AND f.deleted_at IS NULL "
        "ORDER BY f.created_at DESC, f.rowid DESC",
        (entity_id,),
    ).fetchall()

    facts = []
    for row in fact_rows:
        fact = dict(row)
        fact["value"] = format_value(
            value_text=row["value_text"],
            value_numeric=row["value_numeric"],
            unit=row["unit"],
            attribute=row["attribute"],
        )
        facts.append(fact)

    commitment_rows = conn.execute(
        "SELECT co.commitment_id, co.commitment_mention, co.description, "
        "       cse.status, cse.blocking_reason, cse.due_date_resolved, cse.created_at "
        "FROM commitments co "
        "LEFT JOIN commitment_status_events cse "
        "  ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
        "WHERE co.entity_id = ? AND co.deleted_at IS NULL "
        "ORDER BY co.created_at DESC, co.rowid DESC",
        (entity_id,),
    ).fetchall()

    preference_rows = conn.execute(
        "SELECT preference_id, category, preference_text, created_at "
        "FROM preferences "
        "WHERE entity_id = ? AND is_active = 1 AND deleted_at IS NULL "
        "ORDER BY created_at DESC, rowid DESC",
        (entity_id,),
    ).fetchall()

    commitments = [dict(r) for r in commitment_rows]
    return {
        "entity_id": entity["entity_id"],
        "canonical_name": entity["canonical_name"],
        "entity_type": entity["entity_type"],
        "facts": facts,
        # Split rather than merged, so "what's still outstanding" never has
        # to be re-derived from a status string by the model.
        "open_commitments": [c for c in commitments if (c["status"] or "open") != "done"],
        "completed_commitments": [c for c in commitments if c["status"] == "done"],
        "preferences": [dict(r) for r in preference_rows],
        "note": (
            "Every row here is the CURRENT version: superseded and deleted records are "
            "excluded. Use get_node_history on a fact_id to see what it replaced."
        ),
    }


# ---------------------------------------------------------------------------
# 3. get_connected_edges -- relationships table traversal
# ---------------------------------------------------------------------------

def _snippet_for_node(conn: sqlite3.Connection, node_type: str, node_id: str) -> Optional[str]:
    """Short human-readable text for a connected node, without a full
    get_node_details round trip -- mirrors search_nodes' snippet shape."""
    if node_type == "event":
        row = conn.execute(
            "SELECT description, deleted_at FROM episodic_events WHERE event_id = ?", (node_id,)
        ).fetchone()
        if not row:
            return None
        return f"{row['description']}{' [deleted]' if row['deleted_at'] else ''}"
    if node_type == "fact":
        row = conn.execute(
            "SELECT attribute, value_text, value_numeric, unit, is_active, deleted_at "
            "FROM declarative_facts WHERE fact_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return None
        # format_value, not a raw column read: this used to print the bare
        # value_numeric (or None when both columns were null).
        value = format_value(
            value_text=row["value_text"],
            value_numeric=row["value_numeric"],
            unit=row["unit"],
            attribute=row["attribute"],
        )
        flags = []
        if not row["is_active"]:
            flags.append("superseded")
        if row["deleted_at"]:
            flags.append("deleted")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"{row['attribute']}: {value}{suffix}"
    if node_type == "commitment":
        row = conn.execute(
            "SELECT commitment_mention, deleted_at FROM commitments WHERE commitment_id = ?", (node_id,)
        ).fetchone()
        if not row:
            return None
        status = _commitment_status(conn, node_id)
        flags = [f"status={status or 'none recorded'}"]
        if row["deleted_at"]:
            flags.append("deleted")
        return f"{row['commitment_mention']} [{', '.join(flags)}]"
    if node_type == "preference":
        row = conn.execute(
            "SELECT preference_text, is_active, deleted_at FROM preferences WHERE preference_id = ?",
            (node_id,),
        ).fetchone()
        if not row:
            return None
        flags = []
        if not row["is_active"]:
            flags.append("superseded")
        if row["deleted_at"]:
            flags.append("deleted")
        suffix = f" [{', '.join(flags)}]" if flags else ""
        return f"{row['preference_text']}{suffix}"
    return None


def get_connected_edges(
    conn: sqlite3.Connection, source_node_id: str, edge_type: Optional[str] = None
) -> list[dict]:
    """Returns every relationship touching source_node_id, in EITHER direction
    (the relationships table records edges with an explicit source/target,
    but a 'resolves' edge is equally worth finding whether the node you have
    is the problem or the resolution)."""
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
    """Returns the full version history for a node, oldest to newest: - fact:
    every row ever recorded for that fact's (entity_id, attribute), i.e."""
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
            "FROM declarative_facts WHERE entity_id = ? AND attribute = ? "
            "ORDER BY created_at ASC, rowid ASC",
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
            "FROM commitment_status_events WHERE commitment_id = ? "
            "ORDER BY created_at ASC, rowid ASC",
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
            # unscoped preferences are never superseded, so history is just this row
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
            "FROM preferences WHERE entity_id = ? AND category = ? "
            "ORDER BY created_at ASC, rowid ASC",
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
    """Deterministic arithmetic."""
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


def get_open_commitments(
    conn: sqlite3.Connection,
    entity_id: Optional[str] = None,
    limit: int = 25,
) -> dict:
    """Answers "what should I work on first?" -- the user's outstanding
    commitments, ordered, with the reason for each position."""
    all_rows = conn.execute(
        "SELECT co.commitment_id, co.commitment_mention, co.description, co.entity_id, "
        "       co.created_at, "
        "       COALESCE(cse.status, 'open') AS status, cse.blocking_reason, "
        "       cse.due_date_resolved, cse.due_date_relative_expression, "
        "       e.canonical_name AS entity_name "
        "FROM commitments co "
        "LEFT JOIN commitment_status_events cse "
        "  ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
        "LEFT JOIN entities e ON e.entity_id = co.entity_id "
        "WHERE co.deleted_at IS NULL "
        "  AND COALESCE(cse.status, 'open') != 'done' "
        "ORDER BY co.created_at ASC, co.rowid ASC"
    ).fetchall()

    # Scoping by entity in SQL split dependency pairs: "what do I need before
    # the Trellis rewrite?" returned the task with its prerequisite invisible,
    # because the prerequisite belongs to another entity. Widen by one hop.
    if entity_id is not None:
        in_scope = {r["commitment_id"] for r in all_rows if r["entity_id"] == entity_id}
        neighbours: set[str] = set()
        for rel in conn.execute(
            "SELECT source_id, target_id FROM relationships "
            "WHERE source_type = 'commitment' AND target_type = 'commitment' "
            "  AND LOWER(relationship_type) LIKE '%precede%'"
        ).fetchall():
            if rel["source_id"] in in_scope:
                neighbours.add(rel["target_id"])
            if rel["target_id"] in in_scope:
                neighbours.add(rel["source_id"])
        keep = in_scope | neighbours
        rows = [r for r in all_rows if r["commitment_id"] in keep]
    else:
        rows = list(all_rows)

    if not rows:
        return {
            "ordered": [],
            "blocked": [],
            "note": (
                "No outstanding commitments"
                + (" for this entity." if entity_id else " on record.")
            ),
        }

    today = datetime.now(timezone.utc).replace(tzinfo=None).date().isoformat()
    by_id = {r["commitment_id"]: r for r in rows}

    # --- must_precede edges, restricted to the commitments in this set ---
    edges: list[tuple[str, str]] = []
    for rel in conn.execute(
        "SELECT source_id, target_id FROM relationships "
        "WHERE source_type = 'commitment' AND target_type = 'commitment' "
        "  AND LOWER(relationship_type) LIKE '%precede%'"
    ).fetchall():
        if rel["source_id"] in by_id and rel["target_id"] in by_id:
            edges.append((rel["source_id"], rel["target_id"]))

    prerequisites_of: dict[str, list[str]] = {cid: [] for cid in by_id}
    unlocks: dict[str, list[str]] = {cid: [] for cid in by_id}
    for source, target in edges:
        prerequisites_of[target].append(source)
        unlocks[source].append(target)

    actionable = [r for r in rows if r["status"] != "blocked"]
    blocked_rows = [r for r in rows if r["status"] == "blocked"]

    def urgency_key(row: sqlite3.Row) -> tuple:
        due = row["due_date_resolved"]
        if due and due[:10] < today:
            bucket, when = 0, due[:10]          # overdue
        elif due:
            bucket, when = 1, due[:10]          # scheduled
        else:
            bucket, when = 2, ""                # undated
        started = 0 if row["status"] == "in_progress" else 1
        return (bucket, when, started, row["created_at"] or "", row["commitment_id"])

    # --- Kahn's algorithm, picking the most urgent available node each step ---
    actionable_ids = {r["commitment_id"] for r in actionable}
    remaining_prereqs = {
        cid: {p for p in prerequisites_of[cid] if p in actionable_ids} for cid in actionable_ids
    }
    ordered_ids: list[str] = []
    warnings: list[str] = []
    while remaining_prereqs:
        available = [cid for cid, prereqs in remaining_prereqs.items() if not prereqs]
        if not available:
            # A cycle in the recorded edges. Report it and degrade to pure
            # urgency order for what is left, rather than dropping work.
            stuck = sorted(remaining_prereqs)
            warnings.append(
                "circular must_precede relationships between "
                + ", ".join(stuck[:5])
                + " -- ordered by urgency instead; the recorded dependencies need correcting"
            )
            available = stuck
        chosen = min(available, key=lambda cid: urgency_key(by_id[cid]))
        ordered_ids.append(chosen)
        remaining_prereqs.pop(chosen)
        for prereqs in remaining_prereqs.values():
            prereqs.discard(chosen)

    def entry(row: sqlite3.Row, rank: Optional[int] = None) -> dict:
        due = row["due_date_resolved"]
        item = {
            "commitment_id": row["commitment_id"],
            "commitment": row["commitment_mention"],
            "description": row["description"],
            "entity": row["entity_name"],
            "status": row["status"],
            "due_date": due,
            "due_expression": row["due_date_relative_expression"],
        }
        if rank is not None:
            item["rank"] = rank
        prereqs = [by_id[p]["commitment_mention"] for p in prerequisites_of[row["commitment_id"]]]
        unlocked = [by_id[u]["commitment_mention"] for u in unlocks[row["commitment_id"]]]
        if prereqs:
            item["must_follow"] = prereqs
        if unlocked:
            item["unblocks"] = unlocked
        return item

    ordered = []
    for rank, cid in enumerate(ordered_ids[:limit], start=1):
        row = by_id[cid]
        item = entry(row, rank)
        due = row["due_date_resolved"]
        if item.get("must_follow"):
            item["why_here"] = f"must happen after {', '.join(item['must_follow'])}"
        elif due and due[:10] < today:
            item["why_here"] = f"overdue -- was due {due[:10]}"
        elif due:
            item["why_here"] = f"due {due[:10]}"
        elif row["status"] == "in_progress":
            item["why_here"] = "already in progress, no date recorded"
        else:
            item["why_here"] = "no due date recorded"
        if item.get("unblocks"):
            item["why_here"] += f"; unblocks {', '.join(item['unblocks'])}"
        ordered.append(item)

    blocked = []
    for row in sorted(blocked_rows, key=urgency_key)[:limit]:
        item = entry(row)
        item["blocking_reason"] = row["blocking_reason"]
        item["why_here"] = (
            f"blocked -- {row['blocking_reason']}" if row["blocking_reason"] else "blocked, no reason recorded"
        )
        blocked.append(item)

    result = {
        "ordered": ordered,
        "blocked": blocked,
        "counts": {
            "actionable": len(ordered_ids),
            "blocked": len(blocked_rows),
            "shown": len(ordered),
        },
        "ordering_rule": (
            "recorded must_precede dependencies first, then overdue, then by due date, "
            "then in-progress before not-started, then oldest first. Blocked commitments "
            "are listed separately because they are not actionable."
        ),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def get_events_in_window(
    conn: sqlite3.Connection, start: str, end: str, entity_id: Optional[str] = None
) -> list[dict]:
    """Temporal slicing -- events whose resolved_time OR created_at falls in
    [start, end]."""
    query = (
        "SELECT * FROM episodic_events "
        "WHERE COALESCE(resolved_time, created_at) BETWEEN ? AND ?"
    )
    params: list = [start, end]
    if entity_id is not None:
        query += " AND entity_id = ?"
        params.append(entity_id)
    query += " ORDER BY COALESCE(resolved_time, created_at) ASC, rowid ASC"
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
    """Durably persists ONE explicit fact the user asserted mid-conversation --
    "David is now our tech lead" becomes save_memory(entity="David",
    attribute="role", value="tech lead")."""
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
            # deactivate first, same constraint as writer._write_fact
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


# ---------------------------------------------------------------------------
# 6. remember_this -- conversational capture through the FULL pipeline
# ---------------------------------------------------------------------------

def remember_this(
    conn: sqlite3.Connection,
    content: str,
    context: Optional[str] = None,
    client: Optional[object] = None,
) -> dict:
    """Ingests one free-form utterance the user asked Kivi to remember, through
    the SAME triage -> extract -> write path every dictation takes."""
    from kivi.ingestion.extractor import ExtractionFailed, extract_capture
    from kivi.ingestion.triage import triage
    from kivi.ingestion.writer import apply_extraction_result, log_decision

    text = (content or "").strip()
    if not text:
        return {"error": "remember_this requires non-empty 'content' -- nothing was written."}

    started = time.perf_counter()
    capture_id = f"cap_{uuid.uuid4().hex[:12]}"
    captured_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    try:
        conn.execute(
            "INSERT INTO captures "
            "(capture_id, raw_asr_text, formatted_text, source_modality, foreground_app, window_title, "
            " captured_at, extraction_status) "
            "VALUES (?, ?, ?, 'manual_edit', 'Hey Kivi', ?, ?, 'pending')",
            (capture_id, text, text, (context or "Remember This")[:200], captured_at),
        )
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        return {"error": f"remember_this failed before extraction -- nothing was written: {e}"}

    # --- 1. Pre-LLM triage. A flagged secret never reaches the provider. ---
    triage_result = triage(text, text)
    if triage_result.flagged:
        discard_reason = f"pre-LLM triage: {triage_result.reason}"
        try:
            conn.execute(
                "UPDATE captures SET extraction_status = 'pii_detected', discard_reason = ? WHERE capture_id = ?",
                (discard_reason, capture_id),
            )
            log_decision(conn, capture_id, "rejected", reason=discard_reason,
                         latency_ms=(time.perf_counter() - started) * 1000)
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        return {
            "remembered": False,
            "capture_id": capture_id,
            "reason": "quarantined_before_extraction",
            "detail": triage_result.reason,
            "summary": (
                "Not saved -- that looked like it contained a credential or one-time code, so it was "
                "quarantined locally and never sent for extraction."
            ),
        }

    # --- 2. Extraction (retries transient provider failures internally) ---
    try:
        if client is None:
            from kivi.llm import get_client

            client = get_client()
        result = extract_capture(conn, client, capture_id, text, text, captured_at)
    except ExtractionFailed as e:
        conn.rollback()
        discard_reason = f"extraction failed after retries: {e.underlying}"
        try:
            conn.execute(
                "UPDATE captures SET extraction_status = 'incomplete_capture', discard_reason = ? WHERE capture_id = ?",
                (discard_reason, capture_id),
            )
            log_decision(conn, capture_id, "rejected", reason=discard_reason,
                         latency_ms=(time.perf_counter() - started) * 1000)
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        return {
            "remembered": False,
            "capture_id": capture_id,
            "reason": "extraction_failed",
            "detail": str(e.underlying),
            "summary": "Not saved -- extraction failed, so nothing was recorded. Worth trying again.",
        }
    except Exception as e:  # noqa: BLE001 -- e.g. no API key configured
        conn.rollback()
        return {
            "remembered": False,
            "capture_id": capture_id,
            "reason": "extraction_unavailable",
            "detail": str(e),
            "summary": "Not saved -- the extraction model was unavailable, so nothing was recorded.",
        }

    # --- 3. The model itself rejected it (chatter, hypothetical, secret) ---
    if result.extraction_status != "processed":
        try:
            conn.execute(
                "UPDATE captures SET extraction_status = ?, discard_reason = ? WHERE capture_id = ?",
                (result.extraction_status, result.discard_reason, capture_id),
            )
            log_decision(conn, capture_id, "rejected", reason=result.discard_reason,
                         latency_ms=(time.perf_counter() - started) * 1000)
            conn.commit()
        except Exception:  # noqa: BLE001
            conn.rollback()
        return {
            "remembered": False,
            "capture_id": capture_id,
            "reason": result.extraction_status,
            "detail": result.discard_reason,
            "summary": f"Not saved -- {result.discard_reason}",
        }

    # --- 4. Write, with the same supersession discipline as batch ingest ---
    try:
        summary = apply_extraction_result(conn, capture_id, result, None)
        conn.execute(
            "UPDATE captures SET extraction_status = 'processed', discard_reason = NULL WHERE capture_id = ?",
            (capture_id,),
        )
        log_decision(conn, capture_id, "memorized", summary=summary,
                     latency_ms=(time.perf_counter() - started) * 1000)
        conn.commit()
    except Exception as e:  # noqa: BLE001 -- a partial write must never be left committed
        conn.rollback()
        return {"error": f"remember_this failed while writing -- nothing was saved: {e}"}

    learned = {
        "facts_inserted": summary.facts_inserted,
        "facts_superseded": summary.facts_superseded,
        "facts_unchanged": summary.facts_unchanged,
        "events_inserted": summary.events_inserted,
        "commitments_created": summary.commitments_created,
        "commitment_status_changed": summary.commitment_status_changed,
        "preferences_inserted": summary.preferences_inserted,
        "preferences_superseded": summary.preferences_superseded,
        "relationships_resolved": summary.relationships_resolved,
    }
    total_written = sum(
        learned[k] for k in (
            "facts_inserted", "facts_superseded", "events_inserted",
            "commitments_created", "commitment_status_changed",
            "preferences_inserted", "preferences_superseded",
        )
    )

    # Item-level detail, so the agent can say WHAT it remembered rather than
    # reciting counts at the user.
    items = []
    for fact in result.facts:
        value = format_value(fact.value_text, fact.value_numeric, fact.unit, fact.attribute)
        items.append({"type": "fact", "text": f"{fact.entity_mention} -- {fact.attribute}: {value}"})
    for event in result.events:
        items.append({"type": "event", "text": event.description})
    for commitment in result.commitments:
        items.append({"type": "commitment", "text": f"{commitment.description} (status: {commitment.status})"})
    for preference in result.preferences:
        items.append({"type": "preference", "text": preference.preference_text})

    if total_written == 0 and learned["facts_unchanged"] == 0:
        return {
            "remembered": False,
            "capture_id": capture_id,
            "reason": "nothing_durable_found",
            "learned": learned,
            "summary": (
                "Nothing durable to record in that -- no fact, event, commitment or preference "
                "was extracted from it."
            ),
        }

    return {
        "remembered": True,
        "capture_id": capture_id,
        "learned": learned,
        "items": items,
        "entity_resolutions": [
            {"entity_id": r.entity_id, "method": r.method, "matched_alias": r.matched_alias}
            for r in summary.entity_resolutions
        ],
        "warnings": summary.warnings,
        "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "summary": (
            f"Recorded {total_written} memory item(s) from that: "
            + "; ".join(f"[{item['type']}] {item['text']}" for item in items[:6])
            + (f" (+{len(items) - 6} more)" if len(items) > 6 else "")
            + (f" | {learned['facts_unchanged']} already on file, unchanged"
               if learned["facts_unchanged"] else "")
        ),
    }


# ---------------------------------------------------------------------------
# 7. delete_memory -- conversational "forget this"
# ---------------------------------------------------------------------------

def delete_memory(
    conn: sqlite3.Connection,
    node_id: Optional[str] = None,
    reason: Optional[str] = None,
    memory_id: Optional[str] = None,
) -> dict:
    """Soft-deletes one memory the user explicitly asked to forget, and COMMITS
    it."""
    from kivi.api import memory_ops  # local import: memory_ops imports this module at import time

    target = node_id or memory_id
    if not target:
        return {"error": "delete_memory requires a node_id (e.g. 'fact_ab12...') -- nothing was deleted."}

    try:
        result = memory_ops.delete_memory(conn, target, reason=reason)
    except Exception as e:  # noqa: BLE001 -- never leave a half-applied delete committed
        conn.rollback()
        return {"error": f"delete_memory failed -- nothing was deleted: {e}"}

    if "error" in result:
        conn.rollback()
        return result

    conn.commit()
    return result
