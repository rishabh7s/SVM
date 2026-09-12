"""
Verification loop: runs a series of checks against kivi.db and prints
PASS/FAIL for each. This is the sanity check that the schema and seed data
actually behave the way they're supposed to, before any pipeline/LLM code is
built on top of them.
"""

import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "kivi.db"

results: list[tuple[str, bool, str]] = []  # (check_name, passed, detail)


def check(name: str):
    """Decorator: runs a check function, catches AssertionError, records result."""
    def decorator(fn):
        try:
            detail = fn()
            results.append((name, True, detail or "ok"))
        except AssertionError as e:
            results.append((name, False, str(e)))
        except Exception as e:  # noqa: BLE001 -- want to surface any error, not just assertions
            results.append((name, False, f"unexpected error: {e}"))
        return fn
    return decorator


def main() -> None:
    if not DB_PATH.exists():
        print(f"[verify] {DB_PATH} does not exist. Run `python db/init_db.py` first.")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    # ---------------------------------------------------------------
    # 1. Supersession: active fact for Meridian budget is the newer value
    # ---------------------------------------------------------------
    @check("supersession: active budget fact is the revised (45L) value")
    def _():
        row = conn.execute(
            "SELECT value_numeric, source_capture_id FROM declarative_facts "
            "WHERE entity_id = 'ent_meridian' AND attribute = 'budget' AND is_active = 1"
        ).fetchone()
        assert row is not None, "no active budget fact found"
        assert row["value_numeric"] == 4500000, f"expected 4500000, got {row['value_numeric']}"
        assert row["source_capture_id"] == "cap_002", f"expected cap_002, got {row['source_capture_id']}"
        return f"active value={row['value_numeric']} from {row['source_capture_id']}"

    @check("supersession: old budget fact is inactive and points to the new one")
    def _():
        row = conn.execute(
            "SELECT is_active, superseded_by_id FROM declarative_facts WHERE fact_id = 'fact_001'"
        ).fetchone()
        assert row["is_active"] == 0, "fact_001 should be inactive"
        assert row["superseded_by_id"] == "fact_002", f"expected fact_002, got {row['superseded_by_id']}"
        return "fact_001 correctly superseded by fact_002"

    # ---------------------------------------------------------------
    # 2. Database-level constraint: can't have two active facts for the
    #    same (entity_id, attribute) at once.
    # ---------------------------------------------------------------
    @check("constraint: partial unique index rejects a second active fact")
    def _():
        try:
            conn.execute(
                "INSERT INTO declarative_facts "
                "(fact_id, entity_id, attribute, value_numeric, unit, precision_class, "
                " asserter_role, source_capture_id, is_active) "
                "VALUES ('fact_999', 'ent_meridian', 'budget', 9999999, 'INR', "
                "        'exact_source', 'self', 'cap_002', 1)"
            )
            conn.rollback()
            raise AssertionError("insert succeeded but should have violated the unique index")
        except sqlite3.IntegrityError:
            conn.rollback()
            return "insert correctly rejected by idx_one_active_fact_per_attribute"

    # ---------------------------------------------------------------
    # 3. Database-level constraint: can't mark a commitment done without
    #    explicit confirmation.
    # ---------------------------------------------------------------
    @check("constraint: CHECK rejects status='done' without confirmation")
    def _():
        try:
            conn.execute(
                "INSERT INTO commitment_status_events "
                "(status_event_id, commitment_id, status, status_confirmed_by_user, source_capture_id) "
                "VALUES ('cse_999', 'com_002', 'done', 0, 'cap_006')"
            )
            conn.rollback()
            raise AssertionError("insert succeeded but should have violated the CHECK constraint")
        except sqlite3.IntegrityError:
            conn.rollback()
            return "insert correctly rejected by commitment_status_events status/confirmation CHECK"

    # ---------------------------------------------------------------
    # 3b. Database-level constraint: can't have two active status rows for
    #     the same commitment at once -- same discipline as declarative_facts.
    # ---------------------------------------------------------------
    @check("constraint: partial unique index rejects a second active commitment status")
    def _():
        try:
            conn.execute(
                "INSERT INTO commitment_status_events "
                "(status_event_id, commitment_id, status, status_confirmed_by_user, source_capture_id) "
                "VALUES ('cse_998', 'com_001', 'in_progress', 0, 'cap_007')"
            )
            conn.rollback()
            raise AssertionError("insert succeeded but should have violated the unique index")
        except sqlite3.IntegrityError:
            conn.rollback()
            return "insert correctly rejected by idx_one_active_status_per_commitment"

    # ---------------------------------------------------------------
    # 4. Relationship traversal: solution reuse (resolves)
    # ---------------------------------------------------------------
    @check("relationship: problem event resolves to the correct resolution event")
    def _():
        row = conn.execute(
            "SELECT e2.description AS resolution "
            "FROM relationships r "
            "JOIN episodic_events e1 ON r.source_type = 'event' AND r.source_id = e1.event_id "
            "JOIN episodic_events e2 ON r.target_type = 'event' AND r.target_id = e2.event_id "
            "WHERE e1.event_id = 'evt_001' AND r.relationship_type = 'resolves'"
        ).fetchone()
        assert row is not None, "no resolves relationship found from evt_001"
        assert "fixed-step solver" in row["resolution"], f"unexpected resolution text: {row['resolution']}"
        return f"evt_001 resolves via: {row['resolution'][:60]}..."

    # ---------------------------------------------------------------
    # 5. Relationship traversal: ordering (must_precede)
    # ---------------------------------------------------------------
    @check("relationship: commitment ordering (ask about budget before timeline)")
    def _():
        row = conn.execute(
            "SELECT c1.commitment_mention AS predecessor, c2.commitment_mention AS successor, r.reason "
            "FROM relationships r "
            "JOIN commitments c1 ON r.source_type = 'commitment' AND r.source_id = c1.commitment_id "
            "JOIN commitments c2 ON r.target_type = 'commitment' AND r.target_id = c2.commitment_id "
            "WHERE r.relationship_type = 'must_precede'"
        ).fetchone()
        assert row is not None, "no must_precede relationship found"
        assert row["predecessor"] == "ask about budget"
        assert row["successor"] == "commit to timeline"
        return f"{row['predecessor']!r} must precede {row['successor']!r} -- {row['reason']}"

    # ---------------------------------------------------------------
    # 6. Commitment status: current status is read via is_active=1 join,
    #    exactly like declarative_facts, never from a mutated column.
    # ---------------------------------------------------------------
    @check("commitment status: 'ask about budget' current status is done and confirmed")
    def _():
        row = conn.execute(
            "SELECT status, status_confirmed_by_user, source_capture_id "
            "FROM commitment_status_events WHERE commitment_id = 'com_001' AND is_active = 1"
        ).fetchone()
        assert row is not None, "no active status row for com_001"
        assert row["status"] == "done"
        assert row["status_confirmed_by_user"] == 1
        assert row["source_capture_id"] == "cap_007"
        return f"com_001 current status=done, confirmed, from {row['source_capture_id']}"

    @check("commitment status: 'commit to timeline' current status is blocked, not silently done")
    def _():
        row = conn.execute(
            "SELECT status, blocking_reason "
            "FROM commitment_status_events WHERE commitment_id = 'com_002' AND is_active = 1"
        ).fetchone()
        assert row is not None, "no active status row for com_002"
        assert row["status"] == "blocked"
        assert row["blocking_reason"] is not None
        return f"com_002 blocked: {row['blocking_reason']}"

    # ---------------------------------------------------------------
    # 6b. the superseded 'open' status is kept and points at its successor
    # ---------------------------------------------------------------
    @check("commitment status history: superseded 'open' status is preserved, not erased")
    def _():
        row = conn.execute(
            "SELECT status, is_active, superseded_by_id FROM commitment_status_events WHERE status_event_id = 'cse_001'"
        ).fetchone()
        assert row is not None, "cse_001 (the original 'open' status) no longer exists -- it was deleted, not superseded"
        assert row["status"] == "open"
        assert row["is_active"] == 0
        assert row["superseded_by_id"] == "cse_002"
        return "cse_001 ('open') preserved, inactive, superseded_by cse_002 ('done')"

    @check("commitment status history: full chain for com_001 is queryable oldest-to-newest")
    def _():
        rows = conn.execute(
            "SELECT status, created_at FROM commitment_status_events "
            "WHERE commitment_id = 'com_001' ORDER BY created_at ASC"
        ).fetchall()
        statuses = [r["status"] for r in rows]
        assert statuses == ["open", "done"], f"expected ['open', 'done'], got {statuses}"
        return f"full history for com_001: {statuses}"

    # ---------------------------------------------------------------
    # 7. FTS5 entity search (fuzzy alias lookup)
    # ---------------------------------------------------------------
    @check("FTS5: entity_search finds the Meridian entity by alias")
    def _():
        row = conn.execute(
            "SELECT entity_id FROM entity_search WHERE entity_search MATCH 'meridian' LIMIT 1"
        ).fetchone()
        assert row is not None, "no FTS5 match for 'meridian'"
        assert row["entity_id"] == "ent_meridian"
        return f"FTS5 match -> {row['entity_id']}"

    # ---------------------------------------------------------------
    # 8. Quarantine: pii_detected capture never produced any facts/events
    # ---------------------------------------------------------------
    @check("quarantine: pii_detected capture produced zero facts/events/commitments")
    def _():
        cap_row = conn.execute(
            "SELECT extraction_status, discard_reason FROM captures WHERE capture_id = 'cap_008'"
        ).fetchone()
        assert cap_row["extraction_status"] == "pii_detected"
        assert cap_row["discard_reason"] is not None

        counts = {}
        for table in ("declarative_facts", "episodic_events", "commitments", "commitment_status_events"):
            n = conn.execute(
                f"SELECT COUNT(*) AS n FROM {table} WHERE source_capture_id = 'cap_008'"
            ).fetchone()["n"]
            counts[table] = n
        assert all(n == 0 for n in counts.values()), f"cap_008 leaked into: {counts}"
        return f"cap_008 quarantined cleanly ({cap_row['discard_reason'][:50]}...)"

    # ---------------------------------------------------------------
    # Report
    # ---------------------------------------------------------------
    conn.close()

    print("\n" + "=" * 70)
    print("KIVI DB VERIFICATION LOOP")
    print("=" * 70)
    passed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        print(f"       {detail}")
        if ok:
            passed += 1
    print("-" * 70)
    print(f"{passed}/{len(results)} checks passed")
    print("=" * 70 + "\n")

    if passed != len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
