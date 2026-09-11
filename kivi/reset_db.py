#!/usr/bin/env python3
"""
Zero-dependency (stdlib only) headless DB reset.

Usage:
    python reset_db.py

Behavior:
    - Deletes the existing DB file at KIVI_DB_PATH (or db/kivi.db by default)
      if present.
    - Re-executes db/schema.sql against a fresh SQLite file.
    - No prompts, no interactive confirmation -- safe to call from CI or a
      batch script. Exits 0 on success, non-zero (with a message on
      stderr) on any failure.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("KIVI_DB_PATH", ROOT / "db" / "kivi.db"))
SCHEMA_PATH = ROOT / "db" / "schema.sql"


def main() -> int:
    try:
        if DB_PATH.exists():
            DB_PATH.unlink()

        DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        if not SCHEMA_PATH.exists():
            print(f"schema file not found: {SCHEMA_PATH}", file=sys.stderr)
            return 1

        schema_sql = SCHEMA_PATH.read_text()
        conn = sqlite3.connect(DB_PATH)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.executescript(schema_sql)
            conn.commit()
        finally:
            conn.close()

        return 0
    except Exception as e:  # noqa: BLE001 -- headless script: report and exit non-zero, never traceback-dump
        print(f"reset_db failed: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
