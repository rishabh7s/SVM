"""Runs the evaluation.

    python run_evaluation.py --offline                  # no API key, seconds
    python run_evaluation.py                             # full pipeline
    python run_evaluation.py --ingest kivi_corpus.json   # rebuild first

Two layers. Retrieval checks interrogate memory state through the same tools
the agent uses, with no model call, so the memory layer is testable without
credentials or variance. Agent checks run the real loop and score the
answer. Post-checks assert the database afterwards, which is what catches
"the model said it forgot" against a row that never changed.

Exit codes: 0 passed, 1 a case failed, 2 the database doesn't hold the
corpus this eval set was written for. Always runs on a copy -- some cases
delete things.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import statistics
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = REPO_ROOT / "db" / "kivi.db"
DEFAULT_EVAL_SET = REPO_ROOT / "evals" / "eval_set.json"
RESULTS_ROOT = REPO_ROOT / "evals" / "results"

# Set these to price a run. Unset = token counts only, no invented cost.
COST_PER_1M_INPUT = os.environ.get("KIVI_EVAL_COST_PER_1M_INPUT")
COST_PER_1M_OUTPUT = os.environ.get("KIVI_EVAL_COST_PER_1M_OUTPUT")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def db_stats(path: Path) -> dict:
    """Row counts + file size -- the 'database growth' half of the report."""
    if not path.exists():
        return {"size_bytes": 0}
    conn = open_db(path)
    try:
        stats: dict[str, Any] = {"size_bytes": path.stat().st_size}
        for table in (
            "captures", "entities", "declarative_facts", "episodic_events",
            "commitments", "commitment_status_events", "preferences",
            "relationships", "decision_logs",
        ):
            try:
                stats[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                stats[table] = None
        stats["active_facts"] = conn.execute(
            "SELECT COUNT(*) FROM declarative_facts WHERE is_active = 1 AND deleted_at IS NULL"
        ).fetchone()[0]
        stats["superseded_facts"] = conn.execute(
            "SELECT COUNT(*) FROM declarative_facts WHERE is_active = 0"
        ).fetchone()[0]
        stats["rejected_captures"] = conn.execute(
            "SELECT COUNT(*) FROM captures WHERE extraction_status != 'processed'"
        ).fetchone()[0]
        return stats
    finally:
        conn.close()


def ingestion_report(path: Path) -> dict:
    """What ingestion learned and what it ignored, straight out of
    decision_logs -- the table that exists so this question never has to be
    answered by diffing tables by hand."""
    conn = open_db(path)
    try:
        decisions = {
            row["decision"]: row["n"]
            for row in conn.execute(
                "SELECT decision, COUNT(*) AS n FROM decision_logs GROUP BY decision"
            )
        }
        rejected = [
            dict(row)
            for row in conn.execute(
                "SELECT capture_id, extraction_status, discard_reason FROM captures "
                "WHERE extraction_status != 'processed' "
                "ORDER BY captured_at ASC LIMIT 25"
            )
        ]
        latencies = [
            row["latency_ms"]
            for row in conn.execute(
                "SELECT latency_ms FROM decision_logs WHERE latency_ms IS NOT NULL"
            )
        ]
        return {
            "decisions": decisions,
            "deliberately_ignored_sample": rejected,
            "ingestion_latency_ms": {
                "count": len(latencies),
                "mean": round(statistics.mean(latencies), 1) if latencies else None,
                "median": round(statistics.median(latencies), 1) if latencies else None,
                "p95": round(sorted(latencies)[int(len(latencies) * 0.95)], 1) if len(latencies) >= 20 else None,
                "max": round(max(latencies), 1) if latencies else None,
            },
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Retrieval checks -- deterministic, no model call
# ---------------------------------------------------------------------------

def _resolve_entity_id(conn: sqlite3.Connection, name: str) -> Optional[str]:
    row = conn.execute(
        "SELECT entity_id FROM entities WHERE lower(canonical_name) = lower(?)", (name,)
    ).fetchone()
    if row:
        return row["entity_id"]
    row = conn.execute(
        "SELECT entity_id FROM entity_aliases WHERE lower(alias) = lower(?) LIMIT 1", (name,)
    ).fetchone()
    return row["entity_id"] if row else None


def check_grounding(conn: sqlite3.Connection, eval_set: dict, db_path: Path) -> Optional[str]:
    """Verifies the database actually contains the corpus this eval set is
    written against, BEFORE running a single case."""
    required = eval_set.get("requires_entities") or []
    missing = [name for name in required if _resolve_entity_id(conn, name) is None]
    if not missing:
        return None

    capture_count = conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
    looks_like_seed = capture_count < 50
    corpus = eval_set.get("corpus_file", "kivi_corpus.json")

    lines = [
        "=" * 70,
        "EVALUATION NOT RUN -- the database does not contain the corpus",
        "=" * 70,
        f"  database:      {db_path}",
        f"  grounded in:   {corpus}",
        f"  missing:       {', '.join(missing)}",
        f"  captures found: {capture_count}"
        + ("  (this looks like db/seed.sql only -- the seed narrative, not the corpus)" if looks_like_seed else ""),
        "",
        "  These cases ask about entities that live in the corpus, so running them",
        "  now would report failures that say more about the database than about",
        "  the system. Do ONE of the following:",
        "",
        "  1. Use the pre-ingested database committed with this repository",
        "     (fastest -- no model calls, no API key, runs in seconds):",
        "",
        "         python run_evaluation.py --offline",
        "",
        f"  2. Rebuild the memory state from {corpus} and evaluate that",
        "     (~500 extraction calls; budget 40-60 minutes, needs GEMINI_API_KEY):",
        "",
        f"         python run_evaluation.py --ingest {corpus}",
        "",
        "  3. Evaluate a different corpus, with an eval set written for it:",
        "",
        "         python run_evaluation.py --eval-set path/to/your_cases.json",
        "",
        "=" * 70,
    ]
    return "\n".join(lines)


def run_retrieval_check(conn: sqlite3.Connection, check: dict) -> dict:
    """Executes one deterministic assertion against memory state."""
    from kivi.retrieval import tools

    kind = check["kind"]
    detail: dict[str, Any] = {"kind": kind}
    started = time.perf_counter()

    if kind == "search_returns_nothing":
        results = tools.search_nodes(conn, check["query"], limit=10)
        detail["results"] = [r["node_id"] for r in results]
        passed = len(results) == 0
        detail["reason"] = (
            "no candidate cleared the relevance floor, so the agent is forced to abstain"
            if passed else f"{len(results)} candidate(s) surfaced for a question the corpus cannot answer"
        )

    elif kind == "search_finds":
        results = tools.search_nodes(conn, check["query"], limit=check.get("limit", 10))
        detail["results"] = [{"node_id": r["node_id"], "node_type": r["node_type"]} for r in results]
        wanted_type = check.get("node_type")
        matching = [r for r in results if wanted_type is None or r["node_type"] == wanted_type]
        passed = len(matching) >= check.get("min_results", 1)
        detail["reason"] = f"{len(matching)} matching result(s)"

    elif kind == "entity_fact":
        entity_id = _resolve_entity_id(conn, check["entity"])
        if entity_id is None:
            passed = False
            detail["reason"] = f"entity {check['entity']!r} not found in this database"
        else:
            state = tools.get_entity_facts(conn, entity_id)
            attribute = check["attribute"]
            matches = [f for f in state["facts"] if f["attribute"] == attribute]
            detail["found"] = [{"fact_id": f["fact_id"], "value": f["value"]} for f in matches]
            detail["provenance"] = [
                {"capture_id": f["capture_id"], "captured_at": f["captured_at"],
                 "foreground_app": f["foreground_app"], "formatted_text": f["formatted_text"]}
                for f in matches
            ]
            values = " ".join(str(f["value"]).lower() for f in matches)
            passed = bool(matches)
            if passed and check.get("expect_value"):
                passed = check["expect_value"].lower() in values
            for forbidden in check.get("must_not_contain", []):
                if forbidden.lower() in values:
                    passed = False
                    detail.setdefault("violations", []).append(forbidden)
            detail["reason"] = (
                f"current {attribute} = {[f['value'] for f in matches]}"
                if matches else f"no active {attribute} fact for this entity"
            )

    elif kind == "no_duplicate_attribute":
        entity_id = _resolve_entity_id(conn, check["entity"])
        if entity_id is None:
            passed = False
            detail["reason"] = f"entity {check['entity']!r} not found"
        else:
            rows = conn.execute(
                "SELECT attribute, COUNT(*) AS n FROM declarative_facts "
                "WHERE entity_id = ? AND is_active = 1 AND deleted_at IS NULL "
                "GROUP BY attribute HAVING n > 1",
                (entity_id,),
            ).fetchall()
            detail["duplicates"] = [dict(r) for r in rows]
            passed = not rows
            detail["reason"] = "one active value per attribute" if passed else f"{len(rows)} attribute(s) with multiple live values"

    elif kind == "relationship_exists":
        # proves the edge exists -- the backbone of solution reuse and ordering
        matches = conn.execute(
            "SELECT rel.relationship_type, rel.source_type, rel.source_id, rel.target_id, "
            "  COALESCE((SELECT description FROM episodic_events WHERE event_id = rel.source_id), "
            "           (SELECT commitment_mention FROM commitments WHERE commitment_id = rel.source_id)) AS src, "
            "  COALESCE((SELECT description FROM episodic_events WHERE event_id = rel.target_id), "
            "           (SELECT commitment_mention FROM commitments WHERE commitment_id = rel.target_id)) AS tgt "
            "FROM relationships rel WHERE LOWER(rel.relationship_type) LIKE LOWER(?)",
            (f"%{check['relationship_type']}%",),
        ).fetchall()
        wanted_src = (check.get("source_contains") or "").lower()
        wanted_tgt = (check.get("target_contains") or "").lower()
        hits = [
            dict(m) for m in matches
            if wanted_src in (m["src"] or "").lower() and wanted_tgt in (m["tgt"] or "").lower()
        ]
        detail["edges_found"] = [{"source": h["src"], "target": h["tgt"]} for h in hits[:5]]
        passed = len(hits) >= check.get("min_count", 1)
        detail["reason"] = (
            f"{len(hits)} matching '{check['relationship_type']}' edge(s) of {len(matches)} total"
        )

    elif kind == "commitment_status":
        row = conn.execute(
            "SELECT co.commitment_id, cse.status FROM commitments co "
            "LEFT JOIN commitment_status_events cse "
            "  ON cse.commitment_id = co.commitment_id AND cse.is_active = 1 "
            "WHERE lower(co.commitment_mention) LIKE lower(?) AND co.deleted_at IS NULL "
            "ORDER BY co.created_at DESC LIMIT 1",
            (f"%{check['mention']}%",),
        ).fetchone()
        detail["found"] = dict(row) if row else None
        passed = row is not None and row["status"] == check["expect_status"]
        detail["reason"] = f"status={row['status'] if row else 'not found'}"

    else:
        passed = False
        detail["reason"] = f"unknown check kind {kind!r}"

    detail["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    detail["passed"] = passed
    return detail


# ---------------------------------------------------------------------------
# Agent checks -- the real Hey Kivi loop
# ---------------------------------------------------------------------------

def run_agent_case(conn: sqlite3.Connection, client, model: str, case: dict) -> dict:
    """Runs one question through the agent and scores the response against the
    case's expectations."""
    from kivi.retrieval import agent as agent_module

    expect = case.get("agent_expect") or {}
    started = time.perf_counter()
    try:
        result = agent_module.run(conn, client, model, case["question"], headless_mode=False)
    except Exception as e:  # noqa: BLE001 -- one broken case must not end the run
        return {
            "passed": False,
            "error": f"{type(e).__name__}: {e}",
            "traceback": traceback.format_exc(limit=4),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }

    response = result.response
    answer_text = response.derived_answer or response.abstain_reason or ""
    lowered = answer_text.lower()

    # A case the provider refused was never evaluated. Scoring it as a product
    # failure is wrong: on a rate-limited key a clean run reports failures for
    # cases that pass on their own.
    if response.response_type == "abstain" and response.abstain_reason and (
        "transient provider error" in response.abstain_reason
        or response.abstain_reason.startswith("agent call failed")
    ):
        return {
            "passed": None,
            "provider_error": True,
            "abstain_reason": response.abstain_reason[:400],
            "note": "not evaluated -- the model provider was unavailable or rate-limited",
            "metrics": {
                "model_calls": result.metrics.model_calls,
                "total_latency_ms": round(result.metrics.total_latency_ms, 2),
            },
        }

    failures: list[str] = []

    expected_type = expect.get("response_type")
    if expected_type and response.response_type != expected_type:
        failures.append(f"expected response_type={expected_type!r}, got {response.response_type!r}")

    for needle in expect.get("must_include", []):
        if needle.lower() not in lowered:
            failures.append(f"answer does not mention {needle!r}")

    for needle in expect.get("must_not_include", []):
        if needle.lower() in lowered:
            failures.append(f"answer mentions {needle!r}, which it must not")

    if expect.get("must_cite") and not response.citations:
        failures.append("answer carries no citation")

    for tool_name in expect.get("must_call_tools", []):
        called = [s.tool_call.tool_name for s in result.steps if s.tool_call is not None]
        if tool_name not in called:
            failures.append(f"{tool_name} was never called (called: {called or 'none'})")

    # resolved here rather than trusted from the model
    from kivi.retrieval.tools import get_node_details

    citations = []
    for citation in response.citations:
        details = get_node_details(conn, citation.source_id)
        citations.append({
            "source_type": citation.source_type,
            "source_id": citation.source_id,
            "snippet": citation.snippet,
            "capture_id": details.get("capture_id"),
            "captured_at": details.get("captured_at"),
            "foreground_app": details.get("foreground_app"),
            "source_text": details.get("formatted_text"),
            "is_active": details.get("is_active"),
            "deleted_at": details.get("deleted_at"),
        })
        # A stale citation is a failure even when the text reads correctly --
        # except where staleness is the point: a history question should cite the
        # superseded row, and a delete should cite what it just deleted.
        if not expect.get("allow_stale_citations"):
            if details.get("is_active") == 0:
                failures.append(f"citation {citation.source_id} is a SUPERSEDED record")
            if details.get("deleted_at"):
                failures.append(f"citation {citation.source_id} is a DELETED record")

    return {
        "passed": not failures,
        "failures": failures,
        "response_type": response.response_type,
        "answer": response.derived_answer,
        "abstain_reason": response.abstain_reason,
        "disambiguation_options": response.disambiguation_options,
        "citations": citations,
        "trace": [
            {
                "thought": step.thought,
                "action": step.action,
                "tool": step.tool_call.tool_name if step.tool_call else None,
                "arguments": step.tool_call.arguments_json if step.tool_call else None,
            }
            for step in result.steps
        ],
        "metrics": {
            "model_calls": result.metrics.model_calls,
            "tool_calls": result.metrics.tool_calls,
            "retrieval_latency_ms": round(result.metrics.retrieval_latency_ms, 2),
            "generation_latency_ms": round(result.metrics.generation_latency_ms, 2),
            "total_latency_ms": round(result.metrics.total_latency_ms, 2),
            "prompt_tokens": result.metrics.prompt_tokens,
            "completion_tokens": result.metrics.completion_tokens,
            "total_tokens": result.metrics.total_tokens,
        },
    }


def run_post_checks(conn: sqlite3.Connection, case: dict) -> list[dict]:
    """Deterministic assertions about DATABASE STATE after the agent ran --
    this is what catches "the model said it forgot, and nothing changed"."""
    results = []
    for check in case.get("post_checks", []):
        kind = check["kind"]
        if kind == "fact_deleted":
            row = conn.execute(
                "SELECT f.is_active, f.deleted_at FROM declarative_facts f "
                "JOIN entities e ON e.entity_id = f.entity_id "
                "WHERE lower(e.canonical_name) = lower(?) AND f.attribute = ? "
                "ORDER BY f.created_at DESC, f.rowid DESC LIMIT 1",
                (check["entity"], check["attribute"]),
            ).fetchone()
            passed = bool(row and row["deleted_at"] is not None and row["is_active"] == 0)
            results.append({
                "kind": kind,
                "passed": passed,
                "observed": dict(row) if row else None,
                "reason": (
                    "the delete is durable in the database, not just in the reply"
                    if passed else "the agent's reply claimed a deletion the database did not record"
                ),
            })
        elif kind == "fact_value":
            row = conn.execute(
                "SELECT f.value_text, f.value_numeric FROM declarative_facts f "
                "JOIN entities e ON e.entity_id = f.entity_id "
                "WHERE lower(e.canonical_name) = lower(?) AND f.attribute = ? "
                "  AND f.is_active = 1 AND f.deleted_at IS NULL LIMIT 1",
                (check["entity"], check["attribute"]),
            ).fetchone()
            observed = (row["value_text"] if row and row["value_text"] is not None
                        else (row["value_numeric"] if row else None))
            passed = row is not None and str(check["expect_value"]).lower() in str(observed).lower()
            results.append({"kind": kind, "passed": passed, "observed": observed})
        else:
            results.append({"kind": kind, "passed": False, "reason": f"unknown post-check {kind!r}"})
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def estimate_cost(prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
    if COST_PER_1M_INPUT is None or COST_PER_1M_OUTPUT is None:
        return None
    try:
        return round(
            (prompt_tokens or 0) / 1_000_000 * float(COST_PER_1M_INPUT)
            + (completion_tokens or 0) / 1_000_000 * float(COST_PER_1M_OUTPUT),
            6,
        )
    except ValueError:
        return None


def write_summary_markdown(path: Path, report: dict) -> None:
    lines: list[str] = []
    a = lines.append
    a(f"# Kivi evaluation -- {report['started_at']}")
    a("")
    a(f"- mode: **{report['mode']}**")
    a(f"- database: `{report['database']}`")
    a(f"- eval set: `{report['eval_set']}` ({report['totals']['cases']} cases)")
    a("")
    a("## Results")
    a("")
    t = report["totals"]
    a(f"- retrieval checks: **{t['retrieval_passed']}/{t['retrieval_total']} passed**")
    if t["agent_total"]:
        a(f"- agent cases: **{t['agent_passed']}/{t['agent_total']} passed**")
    if t["post_total"]:
        a(f"- post-run state checks: **{t['post_passed']}/{t['post_total']} passed**")
    a("")

    failures = [c for c in report["cases"] if c["passed"] is False]
    if failures:
        a("## Failures (kept visible on purpose)")
        a("")
        for case in failures:
            a(f"### {case['case_id']} -- {case['question']}")
            a(f"- why it matters: {case.get('why', '(not stated)')}")
            for check in case.get("retrieval_checks", []):
                if not check["passed"]:
                    a(f"- retrieval check `{check['kind']}` failed: {check.get('reason')}")
            agent = case.get("agent")
            if agent and not agent.get("passed", True):
                for failure in agent.get("failures", []) or [agent.get("error")]:
                    a(f"- agent: {failure}")
            for check in case.get("post_checks", []):
                if not check["passed"]:
                    a(f"- post-check `{check['kind']}` failed: {check.get('reason')}")
            a("")
    else:
        a("No failures.")
        a("")

    a("## Database")
    a("")
    before, after = report["db_before"], report["db_after"]
    a("| metric | before | after |")
    a("|---|---:|---:|")
    for key in sorted(set(before) | set(after)):
        a(f"| {key} | {before.get(key)} | {after.get(key)} |")
    a("")

    ing = report.get("ingestion", {})
    if ing:
        a("## Ingestion decisions")
        a("")
        a(f"- decisions: `{ing.get('decisions')}`")
        a(f"- latency (ms): `{ing.get('ingestion_latency_ms')}`")
        ignored = ing.get("deliberately_ignored_sample") or []
        if ignored:
            a("")
            a("What ingestion deliberately ignored (sample):")
            a("")
            for row in ignored[:10]:
                a(f"- `{row['capture_id']}` -- {row['extraction_status']}: {row['discard_reason']}")
        a("")

    perf = report.get("performance", {})
    if perf:
        a("## Performance and usage")
        a("")
        for key, value in perf.items():
            a(f"- {key}: `{value}`")
        a("")

    path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Kivi evaluation suite.")
    parser.add_argument("--eval-set", type=Path, default=DEFAULT_EVAL_SET)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="Source database; it is COPIED, never modified.")
    parser.add_argument("--offline", action="store_true", help="Run only the deterministic retrieval checks (no API key, no cost).")
    parser.add_argument("--ingest", type=Path, default=None, help="Ingest this corpus into a FRESH database before evaluating.")
    parser.add_argument("--out", type=Path, default=RESULTS_ROOT)
    parser.add_argument("--case", action="append", default=None, help="Run only these case_ids (repeatable).")
    args = parser.parse_args()

    if not args.eval_set.exists():
        print(f"[eval] eval set not found: {args.eval_set}", file=sys.stderr)
        return 1

    eval_set = json.loads(args.eval_set.read_text(encoding="utf-8"))
    cases = eval_set["cases"]
    if args.case:
        cases = [c for c in cases if c["case_id"] in set(args.case)]
        if not cases:
            print(f"[eval] no cases matched {args.case}", file=sys.stderr)
            return 1

    started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    run_dir = args.out / datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)

    # --- Optional ingestion pass -------------------------------------------
    work_db = run_dir / "eval.db"
    ingestion_wall_clock = None
    if args.ingest:
        from kivi.ingestion.pipeline import run as pipeline_run
        import kivi.ingestion.pipeline as pipeline_module

        print(f"[eval] building a fresh database and ingesting {args.ingest} ...")
        conn = sqlite3.connect(work_db)
        conn.executescript((REPO_ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        conn.commit()
        conn.close()
        original_db_path = pipeline_module.DB_PATH
        pipeline_module.DB_PATH = work_db
        try:
            t0 = time.perf_counter()
            pipeline_run(args.ingest)
            ingestion_wall_clock = round(time.perf_counter() - t0, 1)
        finally:
            pipeline_module.DB_PATH = original_db_path
    else:
        if not args.db.exists():
            print(
                f"[eval] no database at {args.db}. Create one first:\n"
                f"    python db/init_db.py --reset\n"
                f"  or evaluate against a corpus directly:\n"
                f"    python run_evaluation.py --ingest kivi_corpus.json",
                file=sys.stderr,
            )
            return 1
        shutil.copy(args.db, work_db)

    db_before = db_stats(work_db)

    # --- Client, if we need one --------------------------------------------
    client = None
    model = None
    mode = "offline (retrieval checks only)"
    if not args.offline:
        try:
            from kivi.llm import RETRIEVAL_MODEL, get_client

            client = get_client()
            model = RETRIEVAL_MODEL
            mode = f"full pipeline (agent model: {model})"
        except Exception as e:  # noqa: BLE001 -- missing key is an expected, explainable condition
            print(f"[eval] no usable LLM client ({e}); falling back to --offline.", file=sys.stderr)
            args.offline = True

    conn = open_db(work_db)

    # refuse to run against the wrong database; exit 2 is not exit 1
    grounding_error = check_grounding(conn, eval_set, args.db if not args.ingest else work_db)
    if grounding_error is not None:
        conn.close()
        print(grounding_error, file=sys.stderr)
        return 2

    case_reports: list[dict] = []
    totals = {
        "cases": len(cases), "retrieval_total": 0, "retrieval_passed": 0,
        "agent_total": 0, "agent_passed": 0, "post_total": 0, "post_passed": 0,
        "provider_errors": 0,
    }
    latencies: list[float] = []
    retrieval_latencies: list[float] = []
    token_totals = {"prompt": 0, "completion": 0, "total": 0}
    model_calls = 0

    try:
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case['case_id']} ... ", end="", flush=True)
            report: dict[str, Any] = {
                "case_id": case["case_id"],
                "question": case.get("question"),
                "category": case.get("category"),
                "why": case.get("why"),
                "retrieval_checks": [],
                "agent": None,
                "post_checks": [],
            }

            for check in case.get("retrieval_checks", []):
                outcome = run_retrieval_check(conn, check)
                report["retrieval_checks"].append(outcome)
                totals["retrieval_total"] += 1
                totals["retrieval_passed"] += int(outcome["passed"])
                retrieval_latencies.append(outcome["latency_ms"])

            if not args.offline and case.get("agent_expect") is not None:
                agent_result = run_agent_case(conn, client, model, case)
                report["agent"] = agent_result
                if agent_result.get("provider_error"):
                    # Never evaluated -- excluded from the denominator too.
                    totals["provider_errors"] += 1
                else:
                    totals["agent_total"] += 1
                    totals["agent_passed"] += int(agent_result["passed"])
                metrics = agent_result.get("metrics") or {}
                if metrics.get("total_latency_ms"):
                    latencies.append(metrics["total_latency_ms"])
                model_calls += metrics.get("model_calls") or 0
                for key, field in (("prompt", "prompt_tokens"), ("completion", "completion_tokens"), ("total", "total_tokens")):
                    token_totals[key] += metrics.get(field) or 0

                if not agent_result.get("provider_error"):
                    post = run_post_checks(conn, case)
                    report["post_checks"] = post
                    totals["post_total"] += len(post)
                    totals["post_passed"] += sum(1 for p in post if p["passed"])

            agent_report = report["agent"]
            if agent_report is not None and agent_report.get("provider_error"):
                report["passed"] = None   # not evaluated
            else:
                report["passed"] = (
                    all(c["passed"] for c in report["retrieval_checks"])
                    and (agent_report is None or agent_report["passed"])
                    and all(p["passed"] for p in report["post_checks"])
                )
            case_reports.append(report)
            print("PASS" if report["passed"] else ("SKIPPED (provider unavailable)"
                                                   if report["passed"] is None else "FAIL"))
    finally:
        conn.close()

    db_after = db_stats(work_db)

    performance = {
        "agent_model_calls": model_calls,
        "prompt_tokens": token_totals["prompt"] or None,
        "completion_tokens": token_totals["completion"] or None,
        "total_tokens": token_totals["total"] or None,
        "estimated_cost_usd": estimate_cost(token_totals["prompt"], token_totals["completion"])
        or ("not configured -- set KIVI_EVAL_COST_PER_1M_INPUT / _OUTPUT to price a run"),
        "retrieval_check_latency_ms_mean": round(statistics.mean(retrieval_latencies), 2) if retrieval_latencies else None,
        "agent_latency_ms_mean": round(statistics.mean(latencies), 1) if latencies else None,
        "agent_latency_ms_median": round(statistics.median(latencies), 1) if latencies else None,
        "agent_latency_ms_max": round(max(latencies), 1) if latencies else None,
        "ingestion_wall_clock_s": ingestion_wall_clock,
        "db_growth_bytes": db_after.get("size_bytes", 0) - db_before.get("size_bytes", 0),
    }

    report = {
        "started_at": started_at,
        "mode": mode,
        "database": str(work_db),
        "eval_set": str(args.eval_set),
        "totals": totals,
        "performance": performance,
        "db_before": db_before,
        "db_after": db_after,
        "ingestion": ingestion_report(work_db),
        "cases": case_reports,
    }

    (run_dir / "results.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    write_summary_markdown(run_dir / "summary.md", report)
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    (RESULTS_ROOT / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    shutil.copy(run_dir / "summary.md", RESULTS_ROOT / "latest.md")

    failed = [c["case_id"] for c in case_reports if c["passed"] is False]
    not_evaluated = [c["case_id"] for c in case_reports if c["passed"] is None]
    print("\n" + "=" * 70)
    print("EVALUATION SUMMARY")
    print("=" * 70)
    print(f"  mode:                  {mode}")
    print(f"  cases:                 {totals['cases']}")
    print(f"  retrieval checks:      {totals['retrieval_passed']}/{totals['retrieval_total']}")
    if totals["agent_total"]:
        print(f"  agent cases:           {totals['agent_passed']}/{totals['agent_total']}")
    if totals["post_total"]:
        print(f"  post-run state checks: {totals['post_passed']}/{totals['post_total']}")
    if not_evaluated:
        print(f"  NOT EVALUATED:         {', '.join(not_evaluated)}")
        print("                         (the model provider was rate-limited or unavailable --")
        print("                          these say nothing about the system; re-run them with")
        print("                          --case <id>, or wait for quota to reset)")
    if failed:
        print(f"  FAILED:                {', '.join(failed)}")
    print(f"  results:               {run_dir / 'results.json'}")
    print(f"  summary:               {run_dir / 'summary.md'}")
    print(f"  evaluated database:    {work_db}  (a copy -- {args.db} was not modified)")
    print("=" * 70)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
