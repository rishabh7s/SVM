"""
Batch ingestion CLI.

    python -m kivi.ingestion.pipeline --input corpus/some_captures.json

Behavior:
  1. Loads a JSON array of raw capture records, inserts any not already in
     `captures` (extraction_status='pending'), sorted by captured_at.
  2. Processes every 'pending' capture in chronological order, one
     transaction each:
       a. PII/secret triage on the raw text -- if flagged, quarantine the
          capture directly with NO LLM call (the secret never leaves the
          machine).
       b. Otherwise, call the LLM extractor (kivi/ingestion/extractor.py).
       c. Write the result via kivi/ingestion/writer.py (entity resolution,
          supersession, relationships).
       d. Update the capture's own extraction_status/discard_reason.
  3. Prints progress and a final summary (counts, vocab drift, warnings).

Each capture's triage + extraction + write is one atomic transaction: either
all of it lands, or a rollback leaves that capture untouched for the next
run to retry -- a crash mid-batch never leaves a capture half-written.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from kivi.ingestion.extractor import ExtractionFailed, extract_capture
from kivi.ingestion.triage import triage
from kivi.ingestion.vocab_log import VocabLogger
from kivi.ingestion.writer import apply_extraction_result, log_decision
from kivi.llm import get_extraction_client

DB_PATH = Path(__file__).resolve().parents[2] / "db" / "kivi.db"
VOCAB_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "vocab_observations.json"

# Rate-limit backoff: a 429 (or any transient provider error whose message
# mentions "429"/"rate limit") is retried with exponential backoff rather
# than immediately failing the capture -- a real ~500-record corpus run
# will hit rate limits somewhere in the middle, and one throttled call
# should not cost a capture its extraction the way a genuine malformed-
# response failure should. Distinct from instructor's own max_retries
# inside extract_capture, which handles "model returned invalid JSON," not
# "the provider is throttling us."
MAX_RATE_LIMIT_RETRIES = 5
INITIAL_BACKOFF_SECONDS = 2.0


def _is_rate_limit_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "resource_exhausted" in text


def _extract_with_backoff(conn, client, capture_id, raw_asr_text, formatted_text, captured_at):
    """Wraps extract_capture with exponential backoff on rate-limit errors
    specifically. Any other ExtractionFailed propagates immediately --
    only throttling is worth waiting out here."""
    backoff = INITIAL_BACKOFF_SECONDS
    for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return extract_capture(conn, client, capture_id, raw_asr_text, formatted_text, captured_at)
        except ExtractionFailed as e:
            if attempt >= MAX_RATE_LIMIT_RETRIES or not _is_rate_limit_error(e.underlying):
                raise
            print(f"    [rate-limited] retrying in {backoff:.1f}s (attempt {attempt + 1}/{MAX_RATE_LIMIT_RETRIES})...")
            time.sleep(backoff)
            backoff *= 2


def _load_and_import_captures(conn: sqlite3.Connection, input_path: Path) -> int:
    records = json.loads(input_path.read_text())
    records.sort(key=lambda r: r["captured_at"])

    inserted = 0
    for r in records:
        existing = conn.execute(
            "SELECT 1 FROM captures WHERE capture_id = ?", (r["capture_id"],)
        ).fetchone()
        if existing:
            continue
        conn.execute(
            """
            INSERT INTO captures
                (capture_id, raw_asr_text, formatted_text, source_modality,
                 foreground_app, window_title, captured_at, extraction_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                r["capture_id"], r.get("raw_asr_text", ""), r.get("formatted_text", ""),
                r["source_modality"], r.get("foreground_app"), r.get("window_title"), r["captured_at"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def run(input_path: Path) -> None:
    run_started = time.monotonic()
    db_size_before = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    imported = _load_and_import_captures(conn, input_path)
    print(f"[pipeline] imported {imported} new capture(s) from {input_path.name}")

    pending = conn.execute(
        "SELECT * FROM captures WHERE extraction_status = 'pending' ORDER BY captured_at ASC"
    ).fetchall()
    print(f"[pipeline] {len(pending)} capture(s) pending processing")

    if not pending:
        conn.close()
        return

    client = None  # lazily constructed -- not needed at all if every pending capture gets triaged out
    vocab_logger = VocabLogger()

    processed, quarantined_by_triage, quarantined_by_model, failed = 0, 0, 0, 0
    facts_total, events_total, preferences_total, commitments_total = 0, 0, 0, 0
    latencies_ms: list[float] = []

    for i, cap in enumerate(pending, start=1):
        print(f"[{i}/{len(pending)}] processing {cap['capture_id']} ...", end=" ")
        record_started = time.monotonic()
        try:
            triage_result = triage(cap["raw_asr_text"] or "", cap["formatted_text"] or "")
            if triage_result.flagged:
                reason = f"pre-LLM triage: {triage_result.reason}"
                conn.execute(
                    "UPDATE captures SET extraction_status = 'pii_detected', discard_reason = ? WHERE capture_id = ?",
                    (reason, cap["capture_id"]),
                )
                log_decision(conn, cap["capture_id"], "rejected", reason=reason, latency_ms=(time.monotonic() - record_started) * 1000)
                conn.commit()
                quarantined_by_triage += 1
                latencies_ms.append((time.monotonic() - record_started) * 1000)
                print(f"QUARANTINED (triage): {triage_result.reason}")
                continue

            if client is None:
                client = get_extraction_client()

            result = _extract_with_backoff(
                conn, client, cap["capture_id"], cap["raw_asr_text"] or "", cap["formatted_text"] or "", cap["captured_at"]
            )

            summary = apply_extraction_result(conn, cap["capture_id"], result, vocab_logger)

            new_status = result.extraction_status
            conn.execute(
                "UPDATE captures SET extraction_status = ?, discard_reason = ? WHERE capture_id = ?",
                (new_status, result.discard_reason, cap["capture_id"]),
            )
            latency_ms = (time.monotonic() - record_started) * 1000
            latencies_ms.append(latency_ms)
            log_decision(
                conn, cap["capture_id"], "memorized" if new_status == "processed" else "rejected",
                reason=None if new_status == "processed" else result.discard_reason,
                summary=summary, latency_ms=latency_ms,
            )
            conn.commit()

            if new_status == "processed":
                processed += 1
                facts_total += summary.facts_inserted
                events_total += summary.events_inserted
                preferences_total += summary.preferences_inserted
                commitments_total += summary.commitments_created
                print(
                    f"OK -- facts:+{summary.facts_inserted}/{summary.facts_superseded}sup "
                    f"events:+{summary.events_inserted} commitments:+{summary.commitments_created}/"
                    f"{summary.commitment_status_changed}chg "
                    f"preferences:+{summary.preferences_inserted}/{summary.preferences_superseded}sup "
                    f"relationships:+{summary.relationships_resolved} ({latency_ms:.0f}ms)"
                )
                for w in summary.warnings:
                    print(f"    [warn] {w}")
            else:
                quarantined_by_model += 1
                print(f"QUARANTINED (model): {result.discard_reason} ({latency_ms:.0f}ms)")

        except ExtractionFailed as e:
            conn.rollback()
            latency_ms = (time.monotonic() - record_started) * 1000
            latencies_ms.append(latency_ms)
            reason = f"extraction failed after retries: {e.underlying}"
            conn.execute(
                "UPDATE captures SET extraction_status = 'incomplete_capture', discard_reason = ? WHERE capture_id = ?",
                (reason, cap["capture_id"]),
            )
            log_decision(conn, cap["capture_id"], "rejected", reason=reason, latency_ms=latency_ms)
            conn.commit()
            failed += 1
            print(f"FAILED: {e.underlying}")

    VOCAB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    vocab_logger.write(VOCAB_LOG_PATH)

    wall_clock_s = time.monotonic() - run_started
    avg_latency_ms = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0
    db_size_after = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    growth_kb = (db_size_after - db_size_before) / 1024

    print("\n" + "=" * 70)
    print("PIPELINE RUN SUMMARY")
    print("=" * 70)
    print(f"  records processed:            {len(pending)}")
    print(f"  memorized:                     {processed}")
    print(f"  rejected (triage):             {quarantined_by_triage}")
    print(f"  rejected (model triage):       {quarantined_by_model}")
    print(f"  failed (incomplete):           {failed}")
    print(f"  facts created:                 {facts_total}")
    print(f"  events created:                {events_total}")
    print(f"  commitments created:           {commitments_total}")
    print(f"  preferences created:           {preferences_total}")
    print(f"  wall-clock time:               {wall_clock_s:.1f}s")
    print(f"  avg latency / record:          {avg_latency_ms:.0f}ms")
    print(f"  database growth:               {growth_kb:.1f} KB (kivi.db)")
    print(f"  decision log:                  decision_logs table in {DB_PATH} (query by capture_id)")
    print(f"  vocab drift log written to:    {VOCAB_LOG_PATH}")
    print(vocab_logger.summary())
    print("=" * 70)

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-ingest a JSON corpus of raw captures into kivi.db")
    parser.add_argument("--input", required=True, type=Path, help="Path to a JSON file of raw capture records")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[pipeline] input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    run(args.input)
