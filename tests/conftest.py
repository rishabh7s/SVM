"""Builds one seeded database per test session and points each test module's
DB_PATH at it.

The modules all copy DB_PATH into a tmp_path before touching it, which was
fine locally but broke on a fresh clone -- db/kivi.db is generated, so it
either didn't exist or held the corpus rather than the seed. Now they copy
from a template that always exists and always holds the seed narrative.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "db" / "schema.sql"
SEED_PATH = REPO_ROOT / "db" / "seed.sql"


def build_seeded_db(target: Path) -> Path:
    """Creates a fresh database at `target` from schema.sql + seed.sql."""
    if target.exists():
        target.unlink()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.executescript(SCHEMA_PATH.read_text())
        conn.executescript(SEED_PATH.read_text())
        conn.commit()
    finally:
        conn.close()
    return target


@pytest.fixture(scope="session")
def seeded_db_template(tmp_path_factory) -> Path:
    """One seeded database per session, copied (never mutated) by tests."""
    return build_seeded_db(tmp_path_factory.mktemp("kivi_seed") / "kivi_seeded.db")


@pytest.fixture(autouse=True)
def _bind_module_db_path(request, seeded_db_template, monkeypatch):
    """Points every test module's own DB_PATH constant at the seeded template,
    so `shutil.copy(DB_PATH, tmp)` inside each module's fixture works
    identically on a fresh clone with no db/kivi.db present."""
    module = request.module
    if module is not None and hasattr(module, "DB_PATH"):
        monkeypatch.setattr(module, "DB_PATH", seeded_db_template)


@pytest.fixture
def seeded_conn(tmp_path, seeded_db_template):
    """A writable connection to a private copy of the seeded database --
    for new tests that don't want to repeat the copy/connect boilerplate."""
    target = tmp_path / "kivi_test.db"
    shutil.copy(seeded_db_template, target)
    conn = sqlite3.connect(target)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()
