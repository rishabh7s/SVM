"""Batch ingestion.

    python -m kivi.ingestion.pipeline --input corpus/some_captures.json
    python -m kivi.ingestion.pipeline --retry-failed

Loads a JSON array of captures, then for each pending one: triage (no LLM --
a flagged secret never leaves the machine), extract, write, log the
decision. One transaction per capture, so a crash mid-batch leaves the rest
retryable.
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

# Backoff lives in kivi/retry.py now, inside extract_capture, so /ingest gets
# it too. This only prints progress -- a long wait shouldn't look like a hang.
def _print_retry(attempt: int, delay: float, exc: BaseException) -> None:
    print(f"\n    [transient provider error] retrying in {delay:.1f}s (attempt {attempt}): {exc}", end=" ")


def _extract_with_backoff(conn, client, capture_id, raw_asr_text, formatted_text, captured_at):
    """Thin pass-through to extract_capture, supplying the progress printer."""
    return extract_capture(
        conn, client, capture_id, raw_asr_text, formatted_text, captured_at, on_retry=_print_retry
    )


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
                # Defaults to speech: an imported dictation corpus is spoken by
                # definition, and a foreign corpus is unlikely to carry a field
                # named for this schema's CHECK constraint. Requiring it would
                # abort an otherwise valid import on a purely notational gap.
                r.get("source_modality") or "speech",
                r.get("foreground_app"), r.get("window_title"), r["captured_at"],
            ),
        )
        inserted += 1
    conn.commit()
    return inserted


def run(input_path: Path | None = None, retry_failed: bool = False) -> None:
    """Imports `input_path` (when given) and processes every pending capture."""
    run_started = time.monotonic()
    db_size_before = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    if input_path is not None:
        imported = _load_and_import_captures(conn, input_path)
        print(f"[pipeline] imported {imported} new capture(s) from {input_path.name}")

    if retry_failed:
        requeued = conn.execute(
            "UPDATE captures SET extraction_status = 'pending', discard_reason = NULL "
            "WHERE extraction_status = 'incomplete_capture'"
        ).rowcount
        conn.commit()
        print(f"[pipeline] re-queued {requeued} previously-failed capture(s) for another attempt")

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
    parser.add_argument("--input", type=Path, help="Path to a JSON file of raw capture records")
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="Also re-process captures left in 'incomplete_capture' by an earlier provider "
             "failure (503/429). Does NOT retry content the model rejected or triage quarantined.",
    )
    args = parser.parse_args()

    if args.input is None and not args.retry_failed:
        print("[pipeline] nothing to do: pass --input <corpus.json>, --retry-failed, or both", file=sys.stderr)
        sys.exit(1)

    if args.input is not None and not args.input.exists():
        print(f"[pipeline] input file not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    run(args.input, retry_failed=args.retry_failed)
