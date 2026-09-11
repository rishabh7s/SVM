"""
Streamlit front end for the Kivi semantic-memory demo.

    streamlit run streamlit_app.py

This is the "interface intended for a normal user" the assignment asks
for -- it talks to the real backend (kivi/api/app.py, running separately
via `uvicorn kivi.api.app:app`) over HTTP for every write and every
model-touching operation, and reads db/kivi.db directly (read-only) for
the inspector tab, since that's the one place the assignment explicitly
wants raw visibility into memory state rather than a curated API view.

Five tabs, matching the product's own boundary between modes:
  - Hey Kivi        -- the conversational agent (POST /query). Semantic
                        memory is central here: retrieval, citations, and
                        the fallback save_memory write all happen in this
                        mode.
  - Put Info         -- manual, session-aware ingestion (POST /ingest with
                        session_id). This is Flow 1: an implicit update
                        typed right after a Hey Kivi conversation resolves
                        against that same session's history.
  - Dictation        -- deliberately NOT memory-aware. Typed text is shown
                        back as-is with no extraction, no session, no
                        model call -- this tab exists to make the
                        dictation/Hey Kivi boundary visible in the product
                        itself, not just in the README.
  - Memory inspector  -- browse entities/facts/events/commitments/
                        preferences/relationships and their provenance
                        directly from SQLite.
  - Corpus & reset    -- run the batch pipeline against an uploaded
                        corpus, or reset the database, both as subprocess
                        calls to the real CLI scripts (no separate logic
                        duplicated here).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import uuid
from pathlib import Path

import requests
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "db" / "kivi.db"

st.set_page_config(page_title="Kivi", page_icon="🥝", layout="wide")

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

if "session_id" not in st.session_state:
    st.session_state.session_id = f"sess_{uuid.uuid4().hex[:12]}"
if "chat_history" not in st.session_state:
    st.session_state.chat_history = []  # list of {role, content, citations?, metrics?}
if "base_url" not in st.session_state:
    st.session_state.base_url = "http://localhost:8000"


def api(method: str, path: str, **kwargs):
    url = st.session_state.base_url.rstrip("/") + path
    try:
        resp = requests.request(method, url, timeout=60, **kwargs)
    except requests.exceptions.ConnectionError:
        st.error(
            f"Can't reach the Kivi backend at {st.session_state.base_url}. "
            f"Start it with: uvicorn kivi.api.app:app --reload"
        )
        return None
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        st.error(f"{resp.status_code}: {detail}")
        return None
    return resp.json()


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### kivi")
    st.caption("by Sarvam -- semantic memory demo")

    st.session_state.base_url = st.text_input("Backend URL", value=st.session_state.base_url)

    health = api("GET", "/health")
    if health:
        st.success("Backend reachable")
    else:
        st.warning("Backend unreachable -- Hey Kivi / Put Info will not work until it is.")

    st.divider()
    st.markdown("**Session**")
    st.code(st.session_state.session_id, language=None)
    if st.button("New session", use_container_width=True):
        st.session_state.session_id = f"sess_{uuid.uuid4().hex[:12]}"
        st.session_state.chat_history = []
        st.rerun()

    st.caption(
        "One session_id ties Hey Kivi and Put Info together -- an implicit "
        "update typed in Put Info right after a Hey Kivi conversation "
        "resolves against that same conversation's history."
    )

tab_hey_kivi, tab_put_info, tab_dictation, tab_inspector, tab_admin = st.tabs(
    ["Hey Kivi", "Put info", "Dictation", "Memory inspector", "Corpus & reset"]
)

# ---------------------------------------------------------------------------
# Hey Kivi -- conversational agent
# ---------------------------------------------------------------------------

with tab_hey_kivi:
    st.markdown("Ask Kivi anything grounded in what it has learned. Semantic memory is fully active here.")

    for turn in st.session_state.chat_history:
        with st.chat_message(turn["role"]):
            st.write(turn["content"])
            if turn.get("citations"):
                with st.expander(f"{len(turn['citations'])} citation(s)"):
                    for c in turn["citations"]:
                        st.markdown(
                            f"**{c['source_type']}** `{c['source_id']}` "
                            f"-- {c.get('foreground_app') or 'manual entry'}, {c.get('captured_at') or 'unknown time'}"
                        )
                        st.caption(c.get("snippet", ""))
            if turn.get("metrics"):
                m = turn["metrics"]
                st.caption(
                    f"total {m['total_latency_ms']:.0f}ms · retrieval {m['retrieval_latency_ms']:.0f}ms · "
                    f"{m['tool_calls']} tool call(s) · {m['model_calls']} model call(s)"
                )

    question = st.chat_input("Hey Kivi...")
    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.write(question)
        result = api(
            "POST", "/query",
            json={"question": question, "session_id": st.session_state.session_id},
        )
        if result:
            st.session_state.session_id = result["session_id"]
            with st.chat_message("assistant"):
                if result["response_type"] == "answer":
                    st.write(result["response_text"])
                elif result["response_type"] == "abstain":
                    st.warning(f"Kivi doesn't have enough to answer that: {result.get('abstain_reason', '')}")
                elif result["response_type"] == "needs_disambiguation":
                    st.info("Which did you mean?")
                    for opt in result.get("disambiguation_options") or []:
                        st.write(f"- {opt}")
                elif result["response_type"] == "needs_clarification":
                    st.info(result.get("clarification_prompt", "Could you clarify?"))
                if result.get("condensed_query") and result["condensed_query"] != question:
                    st.caption(f"Understood as: \u201c{result['condensed_query']}\u201d")
            st.session_state.chat_history.append(
                {
                    "role": "assistant",
                    "content": result.get("response_text") or result.get("abstain_reason")
                    or result.get("clarification_prompt") or "(no answer)",
                    "citations": result.get("citations"),
                    "metrics": result.get("metrics"),
                }
            )
            st.rerun()

# ---------------------------------------------------------------------------
# Put Info -- manual, session-aware ingestion
# ---------------------------------------------------------------------------

with tab_put_info:
    st.markdown(
        "Type an update or note directly. If you've just been chatting with "
        "Hey Kivi in this session, an implicit reference (\u201cUpdate the budget "
        "to $400k\u201d) resolves against that conversation before extraction."
    )
    with st.form("put_info_form", clear_on_submit=True):
        content = st.text_area("Update or note", height=120, placeholder="Update the budget to $400k")
        foreground_app = st.text_input("Foreground app (optional -- leave blank for a manual entry)")
        submitted = st.form_submit_button("Save to memory")

    if submitted and content.strip():
        metadata = {"foreground_app": foreground_app} if foreground_app.strip() else None
        result = api(
            "POST", "/ingest",
            json={
                "content": content,
                "session_id": st.session_state.session_id,
                "metadata": metadata,
                "source_type": "selected_text",
            },
        )
        if result:
            if result.get("condensed_content"):
                st.info(f"Resolved to: \u201c{result['condensed_content']}\u201d")
            status = result["extraction_status"]
            if status == "processed":
                st.success("Saved.")
                cols = st.columns(4)
                cols[0].metric("Facts", f"+{result['facts_inserted']}", f"{result['facts_superseded']} superseded")
                cols[1].metric("Events", f"+{result['events_inserted']}")
                cols[2].metric("Commitments", f"+{result['commitments_created']}")
                cols[3].metric(
                    "Preferences", f"+{result['preferences_inserted']}", f"{result['preferences_superseded']} superseded"
                )
                if result["warnings"]:
                    for w in result["warnings"]:
                        st.warning(w)
            else:
                st.warning(f"Not saved -- {status}: {result.get('discard_reason', '')}")
            with st.expander("Raw response"):
                st.json(result)

# ---------------------------------------------------------------------------
# Dictation -- deliberately memory-inert
# ---------------------------------------------------------------------------

with tab_dictation:
    st.markdown(
        "Ordinary dictation. This box makes no network call, touches no "
        "session, and writes nothing to memory -- semantic memory has no "
        "reason to affect this mode, so the product doesn't let it."
    )
    dictation_text = st.text_area("Dictate", height=200, key="dictation_box")
    st.caption(f"{len(dictation_text.split())} words -- not stored, not extracted, not searchable.")

# ---------------------------------------------------------------------------
# Memory inspector -- direct, read-only SQLite view
# ---------------------------------------------------------------------------

with tab_inspector:
    if not DB_PATH.exists():
        st.warning(f"No database found at {DB_PATH}. Run a reset or an ingestion first.")
    else:
        conn = get_db()
        try:
            entities = conn.execute("SELECT * FROM entities ORDER BY created_at DESC").fetchall()
            entity_names = {e["entity_id"]: e["canonical_name"] for e in entities}

            sub_facts, sub_events, sub_commitments, sub_prefs, sub_rel, sub_log, sub_captures = st.tabs(
                ["Facts", "Events", "Commitments", "Preferences", "Relationships", "Decision log", "Captures"]
            )

            with sub_facts:
                rows = conn.execute(
                    "SELECT * FROM declarative_facts ORDER BY created_at DESC LIMIT 200"
                ).fetchall()
                for r in rows:
                    active = "active" if r["is_active"] else "superseded"
                    deleted = " (deleted)" if r["deleted_at"] else ""
                    st.markdown(
                        f"**{entity_names.get(r['entity_id'], r['entity_id'])}** -- "
                        f"{r['attribute']}: {r['value_text']} `{active}{deleted}`"
                    )
                    st.caption(f"fact_id={r['fact_id']} · source_capture={r['source_capture_id']}")
                if not rows:
                    st.caption("No facts yet.")

            with sub_events:
                rows = conn.execute("SELECT * FROM episodic_events ORDER BY created_at DESC LIMIT 200").fetchall()
                for r in rows:
                    st.markdown(f"**{r['event_type']}** -- {r['description']}")
                    st.caption(
                        f"entity={entity_names.get(r['entity_id'], '(none)')} · "
                        f"when={r['resolved_time'] or r['relative_time_expression'] or 'unresolved'}"
                    )
                if not rows:
                    st.caption("No events yet.")

            with sub_commitments:
                rows = conn.execute(
                    "SELECT co.*, cse.status, cse.blocking_reason FROM commitments co "
                    "LEFT JOIN commitment_status_events cse ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
                    "ORDER BY co.created_at DESC LIMIT 200"
                ).fetchall()
                for r in rows:
                    st.markdown(f"**{r['commitment_mention']}** -- {r['description']} `{r['status'] or 'unknown'}`")
                    if r["blocking_reason"]:
                        st.caption(f"blocked: {r['blocking_reason']}")
                if not rows:
                    st.caption("No commitments yet.")

            with sub_prefs:
                rows = conn.execute("SELECT * FROM preferences ORDER BY created_at DESC LIMIT 200").fetchall()
                for r in rows:
                    active = "active" if r["is_active"] else "superseded"
                    scope = entity_names.get(r["entity_id"], "(general)")
                    st.markdown(f"**{scope}** / {r['category'] or '(uncategorized)'} -- {r['preference_text']} `{active}`")
                if not rows:
                    st.caption("No preferences yet.")

            with sub_rel:
                rows = conn.execute("SELECT * FROM relationships ORDER BY created_at DESC LIMIT 200").fetchall()
                for r in rows:
                    st.markdown(
                        f"`{r['source_type']}:{r['source_id']}` **{r['relationship_type']}** "
                        f"`{r['target_type']}:{r['target_id']}`"
                    )
                    if r["reason"]:
                        st.caption(r["reason"])
                if not rows:
                    st.caption("No relationships yet.")

            with sub_log:
                rows = conn.execute("SELECT * FROM decision_logs ORDER BY created_at DESC LIMIT 200").fetchall()
                for r in rows:
                    icon = "\u2705" if r["decision"] == "memorized" else "\u274c"
                    st.markdown(f"{icon} `{r['capture_id']}` -- {r['decision']}")
                    if r["reason"]:
                        st.caption(r["reason"])
                    else:
                        st.caption(
                            f"facts+{r['facts_created']} events+{r['events_created']} "
                            f"commitments+{r['commitments_created']} preferences+{r['preferences_created']} "
                            f"· {r['latency_ms']:.0f}ms" if r["latency_ms"] is not None else ""
                        )
                if not rows:
                    st.caption("No decisions logged yet.")

            with sub_captures:
                rows = conn.execute("SELECT * FROM captures ORDER BY ingested_at DESC LIMIT 200").fetchall()
                for r in rows:
                    st.markdown(f"`{r['capture_id']}` -- {r['extraction_status']} ({r['source_modality']})")
                    st.caption(r["formatted_text"] or r["raw_asr_text"] or "")
                if not rows:
                    st.caption("No captures yet.")
        finally:
            conn.close()

# ---------------------------------------------------------------------------
# Corpus & reset -- admin actions, delegating to the real CLI scripts
# ---------------------------------------------------------------------------

with tab_admin:
    st.markdown("#### Import a corpus")
    st.caption("Runs the real batch pipeline (import_corpus.py) as a subprocess -- no logic duplicated here.")
    uploaded = st.file_uploader("Corpus JSON", type=["json"])
    if uploaded and st.button("Run import"):
        tmp_path = REPO_ROOT / "logs" / "_uploaded_corpus.json"
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_bytes(uploaded.getvalue())
        with st.spinner("Running import_corpus.py..."):
            proc = subprocess.run(
                [sys.executable, str(REPO_ROOT / "import_corpus.py"), str(tmp_path)],
                capture_output=True, text=True, cwd=REPO_ROOT,
            )
        st.code(proc.stdout + proc.stderr or "(no output)")
        if proc.returncode == 0:
            st.success("Import finished.")
        else:
            st.error(f"import_corpus.py exited {proc.returncode}")

    st.divider()
    st.markdown("#### Reset database")
    st.caption("Wipes db/kivi.db and recreates it from db/schema.sql. This cannot be undone.")
    confirm = st.checkbox("I understand this deletes all memory")
    if st.button("Reset now", disabled=not confirm, type="primary"):
        proc = subprocess.run(
            [sys.executable, str(REPO_ROOT / "reset_db.py")], capture_output=True, text=True, cwd=REPO_ROOT,
        )
        st.code(proc.stdout + proc.stderr or "(no output)")
        if proc.returncode == 0:
            st.success("Database reset.")
            st.session_state.chat_history = []
        else:
            st.error(f"reset_db.py exited {proc.returncode}")
