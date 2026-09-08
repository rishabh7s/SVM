"""
Builds kivi.db from schema.sql + seed.sql using Python's stdlib sqlite3
module -- no separate sqlite3 CLI binary required, so this works the same
way on Windows/macOS/Linux inside the VS Code integrated terminal.

Usage:
    python db/init_db.py            # creates ./db/kivi.db (fails if it exists)
    python db/init_db.py --reset    # deletes any existing kivi.db first
"""

import sqlite3
import sys
from pathlib import Path

DB_DIR = Path(__file__).parent
DB_PATH = DB_DIR / "kivi.db"
SCHEMA_PATH = DB_DIR / "schema.sql"
SEED_PATH = DB_DIR / "seed.sql"


def main() -> None:
    reset = "--reset" in sys.argv

    if DB_PATH.exists():
        if reset:
            DB_PATH.unlink()
            print(f"[init_db] removed existing {DB_PATH}")
        else:
            print(f"[init_db] {DB_PATH} already exists. Re-run with --reset to rebuild it.")
            sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")

    schema_sql = SCHEMA_PATH.read_text()
    seed_sql = SEED_PATH.read_text()

    print(f"[init_db] applying {SCHEMA_PATH.name} ...")
    conn.executescript(schema_sql)

    print(f"[init_db] applying {SEED_PATH.name} ...")
    conn.executescript(seed_sql)

    conn.commit()
    conn.close()

    print(f"[init_db] done. Database created at {DB_PATH}")


if __name__ == "__main__":
    main()
