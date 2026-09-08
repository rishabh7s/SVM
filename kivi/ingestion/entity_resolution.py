"""
Two-tier entity resolution.

L1: the ~50 most recently active entities (by their most recent alias
    insertion) are matched against by cheap exact/normalized string
    comparison -- this is meant to be injected into the extraction prompt
    too, so the LLM itself snaps mentions to known entities before this
    module even runs.
L2: anything L1 didn't catch falls back to an FTS5 lexical search against
    entity_search, accepted only if a hardcoded token-overlap threshold is
    cleared. This is deliberately NOT a learned/embedding matcher for v1 --
    a hardcoded threshold is enough to prove the mechanism and cheap to
    replace later if precision turns out to matter more.
Anything neither tier resolves registers a new entity.

Every resolution decision is returned with a `method` tag
('l1' / 'l2_fuzzy' / 'new') so the caller can log resolution decisions for
inspection -- this is what makes entity drift debuggable later.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from kivi.text_match import normalize, token_overlap

L2_SIMILARITY_THRESHOLD = 0.5  # hardcoded starting point, not learned -- see module docstring


def _normalize(text: str) -> str:
    return normalize(text)


def _token_overlap(a: str, b: str) -> float:
    return token_overlap(a, b)


@dataclass
class ResolutionResult:
    entity_id: str
    method: str  # 'l1' | 'l2_fuzzy' | 'new'
    matched_alias: str | None = None
    similarity: float | None = None


def get_l1_context(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """Returns the ~limit most recently active entities as
    {entity_id, canonical_name, entity_type, aliases: [...]} -- this is the
    payload meant to be injected into the extraction prompt so the LLM
    itself can snap mentions to existing entities before resolution runs."""
    rows = conn.execute(
        """
        SELECT e.entity_id, e.canonical_name, e.entity_type,
               MAX(a.created_at) AS last_active
        FROM entities e
        LEFT JOIN entity_aliases a ON a.entity_id = e.entity_id
        GROUP BY e.entity_id
        ORDER BY last_active DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    context = []
    for row in rows:
        aliases = conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (row["entity_id"],)
        ).fetchall()
        context.append(
            {
                "entity_id": row["entity_id"],
                "canonical_name": row["canonical_name"],
                "entity_type": row["entity_type"],
                "aliases": [a["alias"] for a in aliases],
            }
        )
    return context


def _match_l1(mention: str, l1_context: list[dict]) -> str | None:
    normalized_mention = _normalize(mention)
    for entity in l1_context:
        candidates = [entity["canonical_name"]] + entity["aliases"]
        if any(_normalize(c) == normalized_mention for c in candidates):
            return entity["entity_id"]
    return None


def _match_l2_fuzzy(conn: sqlite3.Connection, mention: str) -> tuple[str, str, float] | None:
    """FTS5 lexical search fallback. Returns (entity_id, matched_alias, similarity)
    or None if nothing clears the threshold.

    Builds an OR query across the mention's individual tokens -- FTS5 treats
    space-separated terms as an implicit AND, which would require an alias to
    contain every single word of the mention to match at all. An OR query
    casts a wider net of candidates, and the actual similarity decision is
    made afterward by _token_overlap, not by FTS5's own ranking.
    """
    tokens = _normalize(mention).split()
    if not tokens:
        return None

    fts_query = " OR ".join(tokens)

    try:
        rows = conn.execute(
            "SELECT entity_id, alias FROM entity_search WHERE entity_search MATCH ? LIMIT 20",
            (fts_query,),
        ).fetchall()
    except sqlite3.OperationalError:
        # malformed FTS5 query syntax (e.g. a lone stopword-like token) -- treat as no match
        return None

    best: tuple[str, str, float] | None = None
    for row in rows:
        score = _token_overlap(mention, row["alias"])
        if best is None or score > best[2]:
            best = (row["entity_id"], row["alias"], score)

    if best and best[2] >= L2_SIMILARITY_THRESHOLD:
        return best
    return None


def resolve_entity(
    conn: sqlite3.Connection,
    mention: str,
    entity_type: str,
    source_capture_id: str,
    l1_context: list[dict],
) -> ResolutionResult:
    """Resolves a raw entity mention to a canonical entity_id, creating a new
    entity if neither tier matches. Also writes a new alias row whenever the
    mention text differs from anything already on file for that entity, so
    future L1 lookups catch it directly next time."""

    # --- L1 ---
    if entity_id := _match_l1(mention, l1_context):
        _ensure_alias(conn, mention, entity_id, source_capture_id)
        return ResolutionResult(entity_id=entity_id, method="l1", matched_alias=mention)

    # --- L2 ---
    if match := _match_l2_fuzzy(conn, mention):
        entity_id, matched_alias, similarity = match
        _ensure_alias(conn, mention, entity_id, source_capture_id)
        return ResolutionResult(entity_id=entity_id, method="l2_fuzzy", matched_alias=matched_alias, similarity=similarity)

    # --- New entity ---
    entity_id = f"ent_{uuid.uuid4().hex[:12]}"
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name) VALUES (?, ?, ?)",
        (entity_id, entity_type, mention),
    )
    _ensure_alias(conn, mention, entity_id, source_capture_id)
    return ResolutionResult(entity_id=entity_id, method="new")


def _ensure_alias(conn: sqlite3.Connection, alias: str, entity_id: str, source_capture_id: str) -> None:
    existing = conn.execute(
        "SELECT 1 FROM entity_aliases WHERE alias = ? AND entity_id = ?", (alias, entity_id)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO entity_aliases (alias, entity_id, source_capture_id) VALUES (?, ?, ?)",
            (alias, entity_id, source_capture_id),
        )
