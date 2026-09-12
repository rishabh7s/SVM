"""Tests for the corpus loader's tolerance of a foreign corpus.

The reviewer imports their own ~500-record corpus through this path, so the
loader has to accept records that carry the fields a dictation log naturally
has without also carrying fields named for this schema's constraints.
"""

from __future__ import annotations

import json

from kivi.ingestion.pipeline import _load_and_import_captures


def _write_corpus(tmp_path, records):
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(records))
    return path


def test_source_modality_defaults_to_speech_when_absent(seeded_conn, tmp_path):
    path = _write_corpus(tmp_path, [{
        "capture_id": "foreign_001",
        "captured_at": "2026-02-01T09:00:00Z",
        "raw_asr_text": "the atlas redesign slipped to march",
        "formatted_text": "The Atlas redesign slipped to March.",
    }])

    assert _load_and_import_captures(seeded_conn, path) == 1

    row = seeded_conn.execute(
        "SELECT source_modality, extraction_status FROM captures WHERE capture_id = ?",
        ("foreign_001",),
    ).fetchone()
    assert row["source_modality"] == "speech"
    assert row["extraction_status"] == "pending"


def test_explicit_source_modality_is_preserved(seeded_conn, tmp_path):
    path = _write_corpus(tmp_path, [{
        "capture_id": "foreign_002",
        "captured_at": "2026-02-01T09:05:00Z",
        "source_modality": "selected_text",
        "formatted_text": "Highlighted passage.",
    }])

    _load_and_import_captures(seeded_conn, path)

    row = seeded_conn.execute(
        "SELECT source_modality FROM captures WHERE capture_id = ?", ("foreign_002",)
    ).fetchone()
    assert row["source_modality"] == "selected_text"


def test_null_source_modality_also_falls_back(seeded_conn, tmp_path):
    """`or "speech"` rather than `.get(..., "speech")`, because an explicit
    null in the JSON would otherwise reach the CHECK constraint and abort."""
    path = _write_corpus(tmp_path, [{
        "capture_id": "foreign_003",
        "captured_at": "2026-02-01T09:10:00Z",
        "source_modality": None,
        "formatted_text": "Null modality.",
    }])

    _load_and_import_captures(seeded_conn, path)

    row = seeded_conn.execute(
        "SELECT source_modality FROM captures WHERE capture_id = ?", ("foreign_003",)
    ).fetchone()
    assert row["source_modality"] == "speech"


def test_reimporting_the_same_records_is_idempotent(seeded_conn, tmp_path):
    """Documented in RUN.md: re-running against an overlapping file is safe."""
    path = _write_corpus(tmp_path, [{
        "capture_id": "foreign_004",
        "captured_at": "2026-02-01T09:15:00Z",
        "formatted_text": "Imported twice.",
    }])

    assert _load_and_import_captures(seeded_conn, path) == 1
    assert _load_and_import_captures(seeded_conn, path) == 0

    count = seeded_conn.execute(
        "SELECT COUNT(*) FROM captures WHERE capture_id = ?", ("foreign_004",)
    ).fetchone()[0]
    assert count == 1
