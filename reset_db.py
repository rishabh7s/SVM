"""Wipes db/kivi.db and rebuilds it from db/schema.sql. No seed data.

    python reset_db.py

Stdlib only, no prompts, exits non-zero on failure, so a review script can
call it unattended.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
DB_PATH = REPO_ROOT / "db" / "kivi.db"
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"


def reset(db_path: Path = DB_PATH, schema_path: Path = SCHEMA_PATH) -> None:
    if not schema_path.exists():
        raise FileNotFoundError(f"schema file not found: {schema_path}")

    if db_path.exists():
        db_path.unlink()

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(schema_path.read_text())
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        reset()
    except Exception as e:  # noqa: BLE001 -- any failure here must exit non-zero, not print a traceback and hang
        print(f"[reset_db] FAILED: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[reset_db] OK -- {DB_PATH} recreated from {SCHEMA_PATH.name}")
    sys.exit(0)
