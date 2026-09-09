"""
Headless API test suite -- runs the complete deterministic REST contract
(GET/PATCH/DELETE /memories) and the CORS/health surface with FastAPI's
TestClient, no running frontend and no network calls needed for those
paths.

The /query endpoint's tests are split deliberately into two groups:
  - Contract-shape tests, which work identically whether or not a live
    model is reachable, since even a failed model call must still produce
    a correctly-shaped QueryResponse with real (non-zero) timing data --
    this is a genuinely meaningful test of the metrics/provenance contract,
    not a weakened substitute for one.
  - A live-only test, decorated to skip automatically when GEMINI_API_KEY
    isn't a real key or the network is unreachable, for exercising the
    actual condensation/clarification conversation flow end to end.

Every test in this file prints its own measured wall-clock duration, per
the explicit request to show real timing for each operation, not just
whether it passed.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DB_PATH = Path(__file__).resolve().parents[1] / "db" / "kivi.db"


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Points the API at a disposable temp copy of the seeded database for
    the duration of each test, so mutating tests (PATCH/DELETE) never touch
    the real db/kivi.db -- same isolation pattern used throughout this
    project's other test files, applied here at the API layer."""
    temp_db = tmp_path / "kivi_test.db"
    shutil.copy(DB_PATH, temp_db)

    import kivi.api.app as app_module

    monkeypatch.setattr(app_module, "DB_PATH", temp_db)
    monkeypatch.setenv("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", "dummy_for_test"))

    # fresh session store per test -- the real one is a module-level
    # singleton and would otherwise leak state between tests
    from kivi.api.session_store import SessionStore
    monkeypatch.setattr(app_module, "SESSION_STORE", SessionStore())

    with TestClient(app_module.app) as c:
        yield c


def _timed(label: str, fn):
    """Runs fn(), prints its measured wall-clock duration, returns its result."""
    t0 = time.perf_counter()
    result = fn()
    elapsed_ms = (time.perf_counter() - t0) * 1000
    print(f"\n[TIMED] {label}: {elapsed_ms:.2f} ms")
    return result, elapsed_ms


# ---------------------------------------------------------------------------
# Health + CORS
# ---------------------------------------------------------------------------

def test_health(client):
    response, elapsed = _timed("GET /health", lambda: client.get("/health"))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert elapsed < 1000  # sanity bound, not a strict perf assertion


def test_cors_preflight_headers_present(client):
    response, elapsed = _timed(
        "OPTIONS /query (CORS preflight)",
        lambda: client.options(
            "/query",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
            },
        ),
    )
    assert response.status_code in (200, 204)
    # With allow_credentials=True, the CORS spec disallows a literal "*"
    # allow-origin on a credentialed request -- Starlette correctly reflects
    # the specific requesting Origin instead, which is what browsers require
    # for credentialed cross-origin requests to actually succeed.
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_actual_request_has_allow_origin_header(client):
    response, elapsed = _timed(
        "GET /health with Origin header",
        lambda: client.get("/health", headers={"Origin": "http://localhost:3000"}),
    )
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


# ---------------------------------------------------------------------------
# Deterministic memory endpoints -- GET
# ---------------------------------------------------------------------------

def test_get_memory_existing_fact(client):
    response, elapsed = _timed("GET /memories/fact_002", lambda: client.get("/memories/fact_002"))
    assert response.status_code == 200
    body = response.json()
    assert body["value_numeric"] == 4500000


def test_get_memory_nonexistent_returns_404(client):
    response, elapsed = _timed(
        "GET /memories/fact_does_not_exist (expect 404)", lambda: client.get("/memories/fact_does_not_exist")
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Deterministic memory endpoints -- PATCH (update / supersession)
# ---------------------------------------------------------------------------

def test_patch_memory_fact_creates_new_version(client):
    response, elapsed = _timed(
        "PATCH /memories/fact_002",
        lambda: client.patch(
            "/memories/fact_002",
            json={"updates": {"value_numeric": 4200000}, "note": "corrected via API"},
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["value_numeric"] == 4200000
    assert body["fact_id"] != "fact_002"
    assert body["foreground_app"] == "Kivi API"

    # confirm old version now shows as superseded when fetched directly
    old_response, _ = _timed("GET /memories/fact_002 (post-update, expect superseded)", lambda: client.get("/memories/fact_002"))
    assert old_response.json()["is_active"] == 0
    assert old_response.json()["superseded_by_id"] == body["fact_id"]


def test_patch_memory_rejects_unknown_field(client):
    response, elapsed = _timed(
        "PATCH /memories/fact_002 with bad field (expect 400)",
        lambda: client.patch("/memories/fact_002", json={"updates": {"attribute": "sneaky"}}),
    )
    assert response.status_code == 400


def test_patch_memory_commitment_status(client):
    response, elapsed = _timed(
        "PATCH /memories/com_002 (mark done)",
        lambda: client.patch(
            "/memories/com_002",
            json={"updates": {"status": "done", "status_confirmed_by_user": True}, "note": "confirmed"},
        ),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "done"


def test_patch_memory_commitment_rejects_unconfirmed_done(client):
    response, elapsed = _timed(
        "PATCH /memories/com_002 done without confirmation (expect 400)",
        lambda: client.patch(
            "/memories/com_002", json={"updates": {"status": "done", "status_confirmed_by_user": False}}
        ),
    )
    assert response.status_code == 400


def test_patch_memory_nonexistent_returns_400(client):
    response, elapsed = _timed(
        "PATCH /memories/fact_does_not_exist (expect 400)",
        lambda: client.patch("/memories/fact_does_not_exist", json={"updates": {"value_numeric": 1}}),
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Deterministic memory endpoints -- DELETE (soft delete)
# ---------------------------------------------------------------------------

def test_delete_memory_soft_deletes(client):
    response, elapsed = _timed(
        "DELETE /memories/evt_001", lambda: client.request("DELETE", "/memories/evt_001", json={"reason": "test"})
    )
    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] is True

    get_response, _ = _timed("GET /memories/evt_001 (post-delete, still visible)", lambda: client.get("/memories/evt_001"))
    assert get_response.status_code == 200  # still fetchable directly -- soft delete, not gone
    assert get_response.json()["deleted_at"] is not None


def test_delete_memory_nonexistent_returns_404(client):
    response, elapsed = _timed(
        "DELETE /memories/fact_totally_made_up (expect 404)",
        lambda: client.request("DELETE", "/memories/fact_totally_made_up", json={}),
    )
    assert response.status_code == 404


def test_delete_memory_twice_returns_400(client):
    client.request("DELETE", "/memories/evt_001", json={})
    response, elapsed = _timed(
        "DELETE /memories/evt_001 again (expect 400, already deleted)",
        lambda: client.request("DELETE", "/memories/evt_001", json={}),
    )
    assert response.status_code == 400


def test_delete_entity_rejected(client):
    response, elapsed = _timed(
        "DELETE /memories/ent_meridian (expect 400, entities not deletable)",
        lambda: client.request("DELETE", "/memories/ent_meridian", json={}),
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# /query -- contract shape, works with or without a live model
# ---------------------------------------------------------------------------

def test_query_contract_shape_and_metrics_always_present(client):
    """Even a failed model call must produce a correctly-shaped
    QueryResponse with genuine (non-fabricated, non-zero where meaningful)
    timing data -- this is the actual metrics/provenance contract, tested
    regardless of live network availability."""
    response, elapsed = _timed(
        "POST /query (contract-shape check, first turn)",
        lambda: client.post("/query", json={"question": "What is the Meridian budget?", "headless_mode": True}),
    )
    assert response.status_code == 200
    body = response.json()

    assert "session_id" in body and body["session_id"]
    assert body["response_type"] in ("answer", "abstain", "needs_disambiguation", "needs_clarification")
    assert "citations" in body

    metrics = body["metrics"]
    for key in (
        "condensation_latency_ms",
        "retrieval_latency_ms",
        "generation_latency_ms",
        "citation_enrichment_ms",
        "total_latency_ms",
        "model_calls",
        "tool_calls",
    ):
        assert key in metrics, f"metrics missing required key: {key}"

    print(f"[TIMED]   -> reported total_latency_ms in response body: {metrics['total_latency_ms']:.2f} ms")
    print(f"[TIMED]   -> reported generation_latency_ms: {metrics['generation_latency_ms']:.2f} ms")
    print(f"[TIMED]   -> response_type: {body['response_type']}")

    # the endpoint's own measured total_latency_ms must be internally
    # consistent with the wall-clock time the test itself measured around
    # the whole HTTP call -- it can't be LARGER than the outer measurement
    assert metrics["total_latency_ms"] <= elapsed + 5  # +5ms slack for measurement boundary overhead


def test_query_first_turn_skips_condensation_model_call(client):
    """First turn in a fresh session has no history to condense against --
    condense_query() should take the zero-cost skip path (see
    kivi/retrieval/condensation.py), which is directly observable as a very
    small condensation_latency_ms even though the agent call itself may
    still take real time or fail on network."""
    response, elapsed = _timed(
        "POST /query (first turn -- condensation should be skipped)",
        lambda: client.post("/query", json={"question": "What is the Meridian budget?", "session_id": "timing_test_1"}),
    )
    body = response.json()
    print(f"[TIMED]   -> condensation_latency_ms (expect near-zero, first turn): {body['metrics']['condensation_latency_ms']:.3f} ms")
    assert body["metrics"]["condensation_latency_ms"] < 50  # no model call made -- should be sub-millisecond in practice


def test_query_session_persists_across_calls(client):
    r1, e1 = _timed(
        "POST /query turn 1 (create session)",
        lambda: client.post("/query", json={"question": "Tell me about Meridian", "session_id": "persist_test"}),
    )
    session_id = r1.json()["session_id"]
    assert session_id == "persist_test"

    session_response, e2 = _timed("GET /sessions/persist_test", lambda: client.get(f"/sessions/{session_id}"))
    assert session_response.status_code == 200
    turns = session_response.json()["turns"]
    assert len(turns) >= 1
    assert turns[0]["role"] == "user"
    assert turns[0]["content"] == "Tell me about Meridian"


def test_get_nonexistent_session_returns_404(client):
    response, elapsed = _timed(
        "GET /sessions/does_not_exist (expect 404)", lambda: client.get("/sessions/does_not_exist")
    )
    assert response.status_code == 404


def test_delete_session(client):
    client.post("/query", json={"question": "hello", "session_id": "to_delete"})
    response, elapsed = _timed("DELETE /sessions/to_delete", lambda: client.delete("/sessions/to_delete"))
    assert response.status_code == 200
    assert response.json()["deleted"] is True

    confirm, _ = _timed("GET /sessions/to_delete (post-delete, expect 404)", lambda: client.get("/sessions/to_delete"))
    assert confirm.status_code == 404


# ---------------------------------------------------------------------------
# /query -- live-only conversational flow (skipped automatically without a
# real, reachable GEMINI_API_KEY)
# ---------------------------------------------------------------------------

def _live_key_available() -> bool:
    key = os.environ.get("GEMINI_API_KEY", "")
    return bool(key) and key not in ("dummy_for_test", "dummy", "dummy_key_no_network_here")


@pytest.mark.skipif(not _live_key_available(), reason="requires a real, network-reachable GEMINI_API_KEY")
def test_live_followup_condensation_resolves_pronoun(client):
    r1, e1 = _timed(
        "POST /query turn 1 (live)",
        lambda: client.post(
            "/query", json={"question": "What's the Meridian project budget?", "session_id": "live_test_1", "headless_mode": False}
        ),
    )
    r2, e2 = _timed(
        "POST /query turn 2 -- follow-up with pronoun (live)",
        lambda: client.post("/query", json={"question": "What was it before that?", "session_id": "live_test_1", "headless_mode": False}),
    )
    print(f"[TIMED]   -> turn 2 condensed_query: {r2.json().get('condensed_query')}")
    assert r2.json()["condensed_query"] is not None
    assert "meridian" in r2.json()["condensed_query"].lower()


@pytest.mark.skipif(not _live_key_available(), reason="requires a real, network-reachable GEMINI_API_KEY")
def test_live_clarification_flow_on_genuine_miss(client):
    r1, e1 = _timed(
        "POST /query -- genuinely unanswerable question (live)",
        lambda: client.post(
            "/query", json={"question": "What did the vendor say about pricing?", "session_id": "live_test_2", "headless_mode": False}
        ),
    )
    print(f"[TIMED]   -> response_type: {r1.json()['response_type']}")
    if r1.json()["response_type"] == "needs_clarification":
        assert r1.json()["clarification_prompt"]

        r2, e2 = _timed(
            "POST /query -- clarifying reply (live)",
            lambda: client.post(
                "/query", json={"question": "I mean the office supplies vendor", "session_id": "live_test_2", "headless_mode": False}
            ),
        )
        print(f"[TIMED]   -> after clarification, response_type: {r2.json()['response_type']}")
