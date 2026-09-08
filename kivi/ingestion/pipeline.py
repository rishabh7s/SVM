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
from pathlib import Path

from kivi.ingestion.extractor import ExtractionFailed, extract_capture
from kivi.ingestion.triage import triage
from kivi.ingestion.vocab_log import VocabLogger
from kivi.ingestion.writer import apply_extraction_result
from kivi.llm import get_extraction_client

DB_PATH = Path(__file__).resolve().parents[2] / "db" / "kivi.db"
VOCAB_LOG_PATH = Path(__file__).resolve().parents[2] / "logs" / "vocab_observations.json"


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

    for i, cap in enumerate(pending, start=1):
        print(f"[pipeline] ({i}/{len(pending)}) {cap['capture_id']} ...", end=" ")
        try:
            triage_result = triage(cap["raw_asr_text"] or "", cap["formatted_text"] or "")
            if triage_result.flagged:
                conn.execute(
                    "UPDATE captures SET extraction_status = 'pii_detected', discard_reason = ? WHERE capture_id = ?",
                    (f"pre-LLM triage: {triage_result.reason}", cap["capture_id"]),
                )
                conn.commit()
                quarantined_by_triage += 1
                print(f"QUARANTINED (triage): {triage_result.reason}")
                continue

            if client is None:
                client = get_extraction_client()

            result = extract_capture(
                conn, client, cap["capture_id"], cap["raw_asr_text"] or "", cap["formatted_text"] or "", cap["captured_at"]
            )

            summary = apply_extraction_result(conn, cap["capture_id"], result, vocab_logger)

            new_status = result.extraction_status
            conn.execute(
                "UPDATE captures SET extraction_status = ?, discard_reason = ? WHERE capture_id = ?",
                (new_status, result.discard_reason, cap["capture_id"]),
            )
            conn.commit()

            if new_status == "processed":
                processed += 1
                print(
                    f"OK -- facts:+{summary.facts_inserted}/{summary.facts_superseded}sup "
                    f"events:+{summary.events_inserted} commitments:+{summary.commitments_created}/"
                    f"{summary.commitment_status_changed}chg relationships:+{summary.relationships_resolved}"
                )
                for w in summary.warnings:
                    print(f"    [warn] {w}")
            else:
                quarantined_by_model += 1
                print(f"QUARANTINED (model): {result.discard_reason}")

        except ExtractionFailed as e:
            conn.rollback()
            conn.execute(
                "UPDATE captures SET extraction_status = 'incomplete_capture', discard_reason = ? WHERE capture_id = ?",
                (f"extraction failed after retries: {e.underlying}", cap["capture_id"]),
            )
            conn.commit()
            failed += 1
            print(f"FAILED: {e.underlying}")

    VOCAB_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    vocab_logger.write(VOCAB_LOG_PATH)

    print("\n" + "=" * 70)
    print("PIPELINE RUN SUMMARY")
    print("=" * 70)
    print(f"  processed:                {processed}")
    print(f"  quarantined (triage):     {quarantined_by_triage}")
    print(f"  quarantined (model):      {quarantined_by_model}")
    print(f"  failed (incomplete):      {failed}")
    print(f"  vocab drift log written to: {VOCAB_LOG_PATH}")
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
