"""Batch import.

    python import_corpus.py <corpus.json>

Thin wrapper around kivi.ingestion.pipeline.run, so the flag-free CLI shape
exists at the repo root. All the actual logic is in the pipeline.
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
