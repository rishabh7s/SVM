#!/usr/bin/env python3
"""
Headless batch corpus importer.

Usage:
    python import_corpus.py <path_to_corpus_json>

Requires GEMINI_API_KEY in the environment (or .env). Processes each
record through extract_and_triage -> commit_capture_and_memories, with
exponential backoff on 429s, a live progress indicator, and a final
summary + a detailed per-record decision log written to disk.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

from google import genai

from kivi.ingestion.extractor import extract_and_triage
from kivi.ingestion.writer import commit_capture_and_memories

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KIVI_DB_PATH", ROOT / "db" / "kivi.db"))
MODEL_NAME = os.environ.get("KIVI_EXTRACTION_MODEL", "gemini-3.6-flash")
MAX_RETRIES = 5
BASE_BACKOFF_SECONDS = 2.0


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg or "rate limit" in msg.lower()


def _extract_with_backoff(client: genai.Client, model_name: str, text: str):
    last_exc: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return extract_and_triage(client, model_name, text)
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if _is_rate_limit_error(e) and attempt < MAX_RETRIES - 1:
                delay = BASE_BACKOFF_SECONDS * (2**attempt) + random.uniform(0, 1)
                print(f"    [rate limited] backing off {delay:.1f}s (attempt {attempt + 1}/{MAX_RETRIES})", file=sys.stderr)
                time.sleep(delay)
                continue
            raise
    raise last_exc  # pragma: no cover -- unreachable, loop always returns or raises


def main() -> int:
    parser = argparse.ArgumentParser(description="Headless batch import of a JSON capture corpus.")
    parser.add_argument("corpus_path", type=Path, help="Path to a JSON file containing an array of capture records.")
    args = parser.parse_args()

    if not args.corpus_path.exists():
        print(f"corpus file not found: {args.corpus_path}", file=sys.stderr)
        return 1

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY is not set -- cannot run extraction.", file=sys.stderr)
        return 1

    try:
        records = json.loads(args.corpus_path.read_text())
    except json.JSONDecodeError as e:
        print(f"corpus file is not valid JSON: {e}", file=sys.stderr)
        return 1
    if not isinstance(records, list):
        print("corpus file must contain a JSON array of records.", file=sys.stderr)
        return 1

    client = genai.Client(api_key=api_key)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    db_size_before = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    total = len(records)
    memorized = 0
    rejected = 0
    failed = 0
    total_facts = 0
    total_episodes = 0
    total_preferences = 0
    per_record_log: list[dict] = []
    latencies_ms: list[float] = []

    run_start = time.perf_counter()

    for i, raw in enumerate(records, start=1):
        record_start = time.perf_counter()
        content_preview = str(raw.get("content") or raw.get("formatted_text") or raw.get("text") or raw.get("raw_asr") or "")[:60]
        print(f"[{i}/{total}] processing: {content_preview!r}...")

        try:
            text = raw.get("formatted_text") or raw.get("content") or raw.get("raw_asr") or raw.get("text") or ""
            if not text:
                raise ValueError("record has no usable text content")

            triage = _extract_with_backoff(client, MODEL_NAME, text)
            result = commit_capture_and_memories(conn, raw, triage)

            if result["decision"] == "memorized":
                memorized += 1
                total_facts += result["facts_created"]
                total_episodes += result["episodes_created"]
                total_preferences += result["preferences_created"]
            else:
                rejected += 1

            per_record_log.append({"index": i, "status": "ok", **result})

        except Exception as e:  # noqa: BLE001 -- one bad record must never kill the batch
            failed += 1
            print(f"    [FAILED] {e}", file=sys.stderr)
            per_record_log.append({"index": i, "status": "failed", "error": str(e)})

        latencies_ms.append((time.perf_counter() - record_start) * 1000)

    conn.close()
    total_wall_seconds = time.perf_counter() - run_start
    db_size_after = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    log_path = ROOT / "import_decision_log.json"
    log_path.write_text(json.dumps(per_record_log, indent=2, default=str))

    avg_latency_ms = sum(latencies_ms) / len(latencies_ms) if latencies_ms else 0.0

    print("\n" + "=" * 70)
    print("IMPORT SUMMARY")
    print("=" * 70)
    print(f"  total records processed:   {total}")
    print(f"  memorized:                 {memorized}")
    print(f"  rejected (deliberate):     {rejected}")
    print(f"  failed (error):            {failed}")
    print(f"  facts created:             {total_facts}")
    print(f"  episodes created:          {total_episodes}")
    print(f"  preferences created:       {total_preferences}")
    print(f"  total wall-clock time:     {total_wall_seconds:.2f}s")
    print(f"  average latency/record:    {avg_latency_ms:.1f}ms")
    print(f"  db size before -> after:   {db_size_before / 1024:.1f} KB -> {db_size_after / 1024:.1f} KB "
          f"(+{(db_size_after - db_size_before) / 1024:.1f} KB)")
    print(f"  detailed decision log:     {log_path}")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
