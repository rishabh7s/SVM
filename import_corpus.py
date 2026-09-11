"""
Headless batch import runner.

    python import_corpus.py <path_to_corpus_json>

Thin CLI wrapper around kivi.ingestion.pipeline.run -- the actual
triage -> extract -> write loop, rate-limit backoff, per-record decision
logging (decision_logs table), and the final summary (records processed,
memorized vs. rejected, facts/events/commitments/preferences created,
wall-clock time, avg latency, db growth, decision-log location) all live
there; see kivi/ingestion/pipeline.py's run() docstring/comments for the
implementation. This file exists only so the exact CLI shape requested by
Sarvam's review process (`python import_corpus.py corpus.json`, no
`--input` flag) is available at the repo root, without duplicating any of
the actual pipeline logic.
"""

from __future__ import annotations

import sys
from pathlib import Path

from kivi.ingestion.pipeline import run

if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python import_corpus.py <path_to_corpus_json>", file=sys.stderr)
        sys.exit(1)

    input_path = Path(sys.argv[1])
    if not input_path.exists():
        print(f"[import_corpus] input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    run(input_path)
