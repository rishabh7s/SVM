"""
Regression tests for the four reported bugs and the secondary issues found
while auditing them. Each test names the behavior that was wrong, so a
failure here says which user-visible symptom came back.
"""

import sqlite3

import pytest

from kivi.formatting import EMPTY_VALUE_PLACEHOLDER, format_value
from kivi.ingestion.writer import canonical_attribute
from kivi.retrieval import tools
from kivi.retrieval.agent import TOOL_REGISTRY
from kivi.retry import backoff_delay, call_with_backoff, is_transient_error


# ---------------------------------------------------------------------------
# Bug 1 -- numeric facts displayed as "None"
# ---------------------------------------------------------------------------

def test_numeric_budget_formats_as_currency_not_none():
    # The exact reported symptom: value_text NULL, value_numeric 40000.0
    assert format_value(None, 40000.0, "USD", "budget") == "$40,000"


def test_currency_inferred_from_attribute_when_unit_missing():
    assert format_value(None, 40000.0, None, "budget") == "$40,000"
    assert format_value(None, 18500.0, None, "annual_cost") == "$18,500"


def test_percent_and_plain_units_do_not_get_currency_treatment():
    assert format_value(None, 15.0, "percent", "budget reduction") == "15%"
    assert format_value(None, 200.0, None, "user_onboarding_count") == "200"
    assert format_value(None, 12.0, "seats", "headcount") == "12 seats"


def test_value_text_wins_over_numeric():
    assert format_value("Sofia Conti", None, None, "owner") == "Sofia Conti"


def test_empty_value_renders_placeholder_never_none():
    rendered = format_value(None, None, None, "budget")
    assert rendered == EMPTY_VALUE_PLACEHOLDER
    assert "None" not in rendered


def test_fractional_and_large_numbers_stay_readable():
    assert format_value(None, 0.001, "s", "solver_step") == "0.001 s"
    assert format_value(None, 4200000.0, "USD", "budget") == "$4,200,000"


def test_fact_snippet_uses_formatted_value(seeded_conn):
    """_snippet_for_node used to print the raw numeric column."""
    fact_id = seeded_conn.execute(
        "SELECT fact_id FROM declarative_facts WHERE value_numeric IS NOT NULL AND is_active = 1 LIMIT 1"
    ).fetchone()["fact_id"]
    snippet = tools._snippet_for_node(seeded_conn, "fact", fact_id)
    assert snippet is not None
    assert "None" not in snippet


# ---------------------------------------------------------------------------
# Bug 2 -- "I have forgotten that" without a durable delete
# ---------------------------------------------------------------------------

def _active_fact(conn: sqlite3.Connection) -> str:
    return conn.execute(
        "SELECT fact_id FROM declarative_facts WHERE is_active = 1 AND deleted_at IS NULL LIMIT 1"
    ).fetchone()["fact_id"]


def test_delete_memory_is_registered_as_an_agent_tool():
    assert "delete_memory" in TOOL_REGISTRY
    # Must be the committing wrapper, not the transaction-neutral memory_ops
    # version -- routing the agent at the latter is what lost every delete.
    assert TOOL_REGISTRY["delete_memory"] is tools.delete_memory


def test_delete_memory_persists_after_the_connection_closes(seeded_conn, tmp_path):
    """The reported bug exactly: the agent said it forgot, the row didn't."""
    fact_id = _active_fact(seeded_conn)
    db_file = seeded_conn.execute("PRAGMA database_list").fetchone()[2]

    result = tools.delete_memory(seeded_conn, node_id=fact_id, reason="user asked to forget")
    assert result.get("deleted") is True
    seeded_conn.close()

    # A brand-new connection sees it only if the write was committed.
    verify = sqlite3.connect(db_file)
    verify.row_factory = sqlite3.Row
    row = verify.execute(
        "SELECT is_active, deleted_at FROM declarative_facts WHERE fact_id = ?", (fact_id,)
    ).fetchone()
    verify.close()
    assert row["deleted_at"] is not None
    assert row["is_active"] == 0


def test_delete_memory_works_for_preferences(seeded_conn):
    """A pref_ id used to raise KeyError out of memory_ops' table lookup."""
    pref = seeded_conn.execute(
        "SELECT preference_id FROM preferences WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    if pref is None:
        pytest.skip("seed data contains no preferences")
    result = tools.delete_memory(seeded_conn, node_id=pref["preference_id"])
    assert result.get("deleted") is True
    row = seeded_conn.execute(
        "SELECT is_active, deleted_at FROM preferences WHERE preference_id = ?",
        (pref["preference_id"],),
    ).fetchone()
    assert row["deleted_at"] is not None and row["is_active"] == 0


def test_delete_memory_accepts_memory_id_synonym(seeded_conn):
    fact_id = _active_fact(seeded_conn)
    assert tools.delete_memory(seeded_conn, memory_id=fact_id).get("deleted") is True


def test_delete_memory_reports_error_rather_than_silent_success(seeded_conn):
    assert "error" in tools.delete_memory(seeded_conn, node_id="fact_does_not_exist")
    assert "error" in tools.delete_memory(seeded_conn, node_id="not_an_id_at_all")
    assert "error" in tools.delete_memory(seeded_conn)


def test_deleted_memory_stops_surfacing_but_stays_auditable(seeded_conn):
    fact_id = _active_fact(seeded_conn)
    attribute = seeded_conn.execute(
        "SELECT attribute FROM declarative_facts WHERE fact_id = ?", (fact_id,)
    ).fetchone()["attribute"]

    tools.delete_memory(seeded_conn, node_id=fact_id)

    assert fact_id not in [r["node_id"] for r in tools.search_nodes(seeded_conn, attribute, limit=50)]
    # ...but still fully inspectable, which is what soft delete is for.
    details = tools.get_node_details(seeded_conn, fact_id)
    assert details["deleted_at"] is not None


def test_deleting_a_commitment_retires_its_active_status(seeded_conn):
    commitment = seeded_conn.execute(
        "SELECT commitment_id FROM commitments WHERE deleted_at IS NULL LIMIT 1"
    ).fetchone()
    tools.delete_memory(seeded_conn, node_id=commitment["commitment_id"])
    still_active = seeded_conn.execute(
        "SELECT 1 FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1",
        (commitment["commitment_id"],),
    ).fetchone()
    assert still_active is None


# ---------------------------------------------------------------------------
# Bug 3 -- compound queries (prompt-level; assert the instruction is present)
# ---------------------------------------------------------------------------

def test_system_prompt_requires_decomposing_multi_part_requests():
    from kivi.retrieval.agent import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "decompose" in lowered
    assert "every part" in lowered


def test_system_prompt_forbids_claiming_an_unmade_deletion():
    from kivi.retrieval.agent import SYSTEM_PROMPT

    assert "FABRICATION" in SYSTEM_PROMPT
    assert "delete_memory" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Bug 4 -- superseded facts answered as current
# ---------------------------------------------------------------------------

def test_search_excludes_superseded_facts_by_default(seeded_conn):
    superseded = seeded_conn.execute(
        "SELECT fact_id, attribute FROM declarative_facts WHERE is_active = 0 LIMIT 1"
    ).fetchone()
    if superseded is None:
        pytest.skip("seed data contains no superseded facts")
    hits = tools.search_nodes(seeded_conn, superseded["attribute"], limit=50)
    assert superseded["fact_id"] not in [h["node_id"] for h in hits]


def test_search_can_opt_into_history_explicitly(seeded_conn):
    superseded = seeded_conn.execute(
        "SELECT fact_id, attribute FROM declarative_facts WHERE is_active = 0 LIMIT 1"
    ).fetchone()
    if superseded is None:
        pytest.skip("seed data contains no superseded facts")
    hits = tools.search_nodes(
        seeded_conn, superseded["attribute"], limit=50, include_superseded=True
    )
    match = [h for h in hits if h["node_id"] == superseded["fact_id"]]
    assert match and match[0]["is_active"] is False


def test_get_entity_facts_returns_only_current_values(seeded_conn):
    entity_id = seeded_conn.execute("SELECT entity_id FROM entities LIMIT 1").fetchone()["entity_id"]
    result = tools.get_entity_facts(seeded_conn, entity_id)

    for fact in result["facts"]:
        row = seeded_conn.execute(
            "SELECT is_active, deleted_at FROM declarative_facts WHERE fact_id = ?", (fact["fact_id"],)
        ).fetchone()
        assert row["is_active"] == 1 and row["deleted_at"] is None
    # exactly one current value per attribute -- no ambiguity to resolve
    attributes = [f["attribute"] for f in result["facts"]]
    assert len(attributes) == len(set(attributes))


def test_get_entity_facts_preformats_values(seeded_conn):
    entity_id = seeded_conn.execute(
        "SELECT entity_id FROM declarative_facts WHERE value_numeric IS NOT NULL AND is_active = 1 LIMIT 1"
    ).fetchone()["entity_id"]
    result = tools.get_entity_facts(seeded_conn, entity_id)
    assert all("None" not in str(f["value"]) for f in result["facts"])


def test_get_entity_facts_unknown_entity_returns_error(seeded_conn):
    assert "error" in tools.get_entity_facts(seeded_conn, "ent_nonexistent")


def test_attribute_synonyms_collapse_to_one_supersession_key():
    for synonym in ("owner", "Project Owner", "project_lead", "DRI", "owned by"):
        assert canonical_attribute(synonym) == "owner"
    for synonym in ("deadline", "Due Date", "ship_date"):
        assert canonical_attribute(synonym) == "deadline"
    # Unrecognized attributes pass through normalized, not forced into a
    # fixed vocabulary.
    assert canonical_attribute("Conversion Rate") == "conversion_rate"


# ---------------------------------------------------------------------------
# Audit 1 -- abstention boundary
# ---------------------------------------------------------------------------

def test_unrelated_query_returns_empty_rather_than_a_weak_match(seeded_conn):
    assert tools.search_nodes(seeded_conn, "coffee machine broken") == []
    assert tools.search_nodes(seeded_conn, "antarctic expedition permits") == []


def test_relevant_query_still_matches(seeded_conn):
    """The abstention floor must not cost genuine recall."""
    assert tools.search_nodes(seeded_conn, "meridian") != []
    assert tools.search_nodes(seeded_conn, "convergence issue") != []


def test_results_rank_by_query_coverage(seeded_conn):
    """A row matching both query terms outranks one matching only a common
    term -- otherwise a small limit cuts the row actually asked about."""
    hits = tools.search_nodes(seeded_conn, "meridian budget", limit=5)
    assert hits
    top = hits[0]["snippet"].lower()
    assert "meridian" in top


# ---------------------------------------------------------------------------
# Audit 2 -- commitment status transitions
# ---------------------------------------------------------------------------

def test_search_results_carry_commitment_status(seeded_conn):
    hits = [h for h in tools.search_nodes(seeded_conn, "meridian", limit=50) if h["node_type"] == "commitment"]
    if not hits:
        pytest.skip("no commitments matched")
    assert all("status" in h for h in hits)


def test_commitment_snippet_reports_status(seeded_conn):
    commitment_id = seeded_conn.execute("SELECT commitment_id FROM commitments LIMIT 1").fetchone()[0]
    assert "status=" in tools._snippet_for_node(seeded_conn, "commitment", commitment_id)


def test_confirmed_completion_is_not_reverted_by_a_default_open_restatement(seeded_conn):
    from kivi.ingestion.writer import WriteSummary, _write_commitment
    from kivi.models.extraction import Commitment

    done = seeded_conn.execute(
        "SELECT co.commitment_id, co.commitment_mention, co.entity_id FROM commitments co "
        "JOIN commitment_status_events cse ON cse.commitment_id = co.commitment_id "
        "WHERE cse.is_active = 1 AND cse.status = 'done' AND cse.status_confirmed_by_user = 1 LIMIT 1"
    ).fetchone()
    if done is None:
        pytest.skip("seed data has no confirmed-done commitment")

    summary = WriteSummary()
    capture_id = seeded_conn.execute("SELECT capture_id FROM captures LIMIT 1").fetchone()[0]
    _write_commitment(
        seeded_conn,
        Commitment(
            commitment_mention=done["commitment_mention"],
            description="mentioned again in passing",
            status="open",  # the extractor's default, not a real reopening
        ),
        done["entity_id"],
        capture_id,
        summary,
    )

    status = seeded_conn.execute(
        "SELECT status FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1",
        (done["commitment_id"],),
    ).fetchone()["status"]
    assert status == "done"
    assert summary.warnings, "the refusal must be visible, not silent"


def test_explicit_reopening_still_supersedes(seeded_conn):
    """The guard must only block the DEFAULT status, never a real change."""
    from kivi.ingestion.writer import WriteSummary, _write_commitment
    from kivi.models.extraction import Commitment

    done = seeded_conn.execute(
        "SELECT co.commitment_id, co.commitment_mention, co.entity_id FROM commitments co "
        "JOIN commitment_status_events cse ON cse.commitment_id = co.commitment_id "
        "WHERE cse.is_active = 1 AND cse.status = 'done' AND cse.status_confirmed_by_user = 1 LIMIT 1"
    ).fetchone()
    if done is None:
        pytest.skip("seed data has no confirmed-done commitment")

    capture_id = seeded_conn.execute("SELECT capture_id FROM captures LIMIT 1").fetchone()[0]
    _write_commitment(
        seeded_conn,
        Commitment(
            commitment_mention=done["commitment_mention"],
            description="turns out it needs redoing",
            status="in_progress",
        ),
        done["entity_id"],
        capture_id,
        WriteSummary(),
    )
    status = seeded_conn.execute(
        "SELECT status FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1",
        (done["commitment_id"],),
    ).fetchone()["status"]
    assert status == "in_progress"


def test_get_entity_facts_separates_open_from_completed(seeded_conn):
    entity_id = seeded_conn.execute(
        "SELECT entity_id FROM commitments WHERE entity_id IS NOT NULL LIMIT 1"
    ).fetchone()["entity_id"]
    result = tools.get_entity_facts(seeded_conn, entity_id)
    assert all(c["status"] == "done" for c in result["completed_commitments"])
    assert all((c["status"] or "open") != "done" for c in result["open_commitments"])


# ---------------------------------------------------------------------------
# Audit 3 -- deterministic ordering
# ---------------------------------------------------------------------------

def test_fact_history_is_ordered_oldest_to_newest_and_is_stable(seeded_conn):
    superseded = seeded_conn.execute(
        "SELECT fact_id FROM declarative_facts WHERE is_active = 0 LIMIT 1"
    ).fetchone()
    if superseded is None:
        pytest.skip("seed data contains no supersession chain")

    history = tools.get_node_history(seeded_conn, superseded["fact_id"])["history"]
    created = [h["created_at"] for h in history]
    assert created == sorted(created)
    # The chain ends on the one live row.
    assert [h["is_active"] for h in history][-1] == 1
    # Stable across repeated calls even when created_at values tie.
    again = tools.get_node_history(seeded_conn, superseded["fact_id"])["history"]
    assert [h["fact_id"] for h in history] == [h["fact_id"] for h in again]


def test_commitment_status_history_is_ordered(seeded_conn):
    commitment_id = seeded_conn.execute(
        "SELECT commitment_id FROM commitment_status_events GROUP BY commitment_id LIMIT 1"
    ).fetchone()[0]
    history = tools.get_node_history(seeded_conn, commitment_id)["history"]
    created = [h["created_at"] for h in history]
    assert created == sorted(created)


def test_events_in_window_are_chronological(seeded_conn):
    events = tools.get_events_in_window(seeded_conn, "2000-01-01", "2100-01-01")
    keys = [e["resolved_time"] or e["created_at"] for e in events]
    assert keys == sorted(keys)


def test_search_results_are_stable_across_identical_calls(seeded_conn):
    first = tools.search_nodes(seeded_conn, "meridian budget", limit=5)
    second = tools.search_nodes(seeded_conn, "meridian budget", limit=5)
    assert [r["node_id"] for r in first] == [r["node_id"] for r in second]


# ---------------------------------------------------------------------------
# Audit 4 -- transient error handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "message",
    [
        "429 Too Many Requests",
        "RESOURCE_EXHAUSTED: quota exceeded",
        "503 Service Unavailable",
        "The model is overloaded. Please try again later.",
        "deadline exceeded",
        "connection reset by peer",
    ],
)
def test_transient_errors_are_recognized(message):
    assert is_transient_error(RuntimeError(message))


@pytest.mark.parametrize(
    "message",
    [
        "400 Invalid argument: bad schema",
        "API key not valid",
        "ValidationError: 2 validation errors for ExtractionResult",
    ],
)
def test_permanent_errors_are_not_retried(message):
    assert not is_transient_error(RuntimeError(message))


def test_status_code_attribute_is_recognized():
    class ProviderError(Exception):
        status_code = 503

    assert is_transient_error(ProviderError("something went wrong"))


def test_wrapped_cause_is_recognized():
    from kivi.ingestion.extractor import ExtractionFailed

    assert is_transient_error(ExtractionFailed("cap_1", RuntimeError("429 rate limit")))
    assert not is_transient_error(ExtractionFailed("cap_1", RuntimeError("400 bad request")))


def test_call_with_backoff_retries_then_succeeds():
    attempts = {"n": 0}
    slept = []

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 rate limit exceeded")
        return "ok"

    assert call_with_backoff(flaky, sleep=slept.append) == "ok"
    assert attempts["n"] == 3
    assert len(slept) == 2


def test_call_with_backoff_does_not_retry_permanent_failures():
    attempts = {"n": 0}

    def broken():
        attempts["n"] += 1
        raise ValueError("400 invalid request")

    with pytest.raises(ValueError):
        call_with_backoff(broken, sleep=lambda _: None)
    assert attempts["n"] == 1


def test_call_with_backoff_gives_up_and_reraises():
    def always_throttled():
        raise RuntimeError("503 service unavailable")

    with pytest.raises(RuntimeError):
        call_with_backoff(always_throttled, max_attempts=3, sleep=lambda _: None)


def test_backoff_grows_and_is_capped():
    from kivi.retry import MAX_BACKOFF_SECONDS

    assert all(0 <= backoff_delay(i) <= MAX_BACKOFF_SECONDS for i in range(10))
    # Full jitter: the ceiling grows even though individual draws are random.
    assert max(backoff_delay(5) for _ in range(50)) > max(backoff_delay(0) for _ in range(50))


# ---------------------------------------------------------------------------
# Addition beyond the reported bugs -- remember_this (full-pipeline capture)
# ---------------------------------------------------------------------------

class _FakeExtractionClient:
    """Stands in for the Gemini client, returning a prepared ExtractionResult."""

    def __init__(self, result):
        self._result = result
        self.calls = 0
        outer = self

        class _Completions:
            def create(self, **kwargs):
                outer.calls += 1
                return outer._result

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


def _extraction(**kwargs):
    from kivi.models.extraction import ExtractionResult

    kwargs.setdefault("capture_id", "cap_test")
    kwargs.setdefault("extraction_status", "processed")
    return ExtractionResult(**kwargs)


def test_remember_this_is_registered_and_self_committing():
    from kivi.retrieval.agent import MUTATING_TOOLS, SELF_COMMITTING_TOOLS

    assert TOOL_REGISTRY["remember_this"] is tools.remember_this
    assert "remember_this" in MUTATING_TOOLS
    assert "remember_this" in SELF_COMMITTING_TOOLS


def test_remember_this_captures_all_four_memory_types(seeded_conn):
    from kivi.models.extraction import Commitment, Event, Fact, Preference

    client = _FakeExtractionClient(_extraction(
        facts=[Fact(
            entity_mention="Helix migration", entity_type="project", attribute="deadline",
            value_text="2026-09-14", precision_class="exact_source", asserter_role="self",
        )],
        events=[Event(event_type="decision", description="Cutover moved to the 14th.")],
        commitments=[Commitment(
            commitment_mention="client summary",
            description="Send the client a summary by Friday.",
            status="open",
        )],
        preferences=[Preference(preference_text="Always put meeting notes in bullet points.")],
    ))

    result = tools.remember_this(
        seeded_conn,
        content=(
            "Helix cutover moved to the 14th, I owe the client a summary by Friday, "
            "and always put meeting notes in bullets."
        ),
        client=client,
    )

    assert result["remembered"] is True
    learned = result["learned"]
    assert learned["facts_inserted"] == 1
    assert learned["events_inserted"] == 1
    assert learned["commitments_created"] == 1
    assert learned["preferences_inserted"] == 1
    assert {item["type"] for item in result["items"]} == {"fact", "event", "commitment", "preference"}


def test_remember_this_persists_across_connections(seeded_conn):
    from kivi.models.extraction import Fact

    client = _FakeExtractionClient(_extraction(facts=[Fact(
        entity_mention="Meridian project", entity_type="project", attribute="owner",
        value_text="Priya Raman", precision_class="exact_source", asserter_role="self",
    )]))
    db_file = seeded_conn.execute("PRAGMA database_list").fetchone()[2]
    assert tools.remember_this(seeded_conn, content="Priya now runs Meridian.", client=client)["remembered"]
    seeded_conn.close()

    verify = sqlite3.connect(db_file)
    verify.row_factory = sqlite3.Row
    row = verify.execute(
        "SELECT value_text FROM declarative_facts WHERE attribute = 'owner' "
        "AND entity_id = 'ent_meridian' AND is_active = 1"
    ).fetchone()
    verify.close()
    assert row["value_text"] == "Priya Raman"


def test_remember_this_supersedes_rather_than_duplicating(seeded_conn):
    """It goes through writer.py, so it inherits supersession for free."""
    from kivi.models.extraction import Fact

    def budget_fact(amount):
        return _extraction(facts=[Fact(
            entity_mention="Meridian project", entity_type="project", attribute="budget",
            value_numeric=amount, unit="INR", precision_class="exact_source", asserter_role="self",
        )])

    tools.remember_this(
        seeded_conn, content="Meridian budget is now 60 lakh.",
        client=_FakeExtractionClient(budget_fact(6000000)),
    )
    active = seeded_conn.execute(
        "SELECT COUNT(*) AS n FROM declarative_facts "
        "WHERE entity_id = 'ent_meridian' AND attribute = 'budget' AND is_active = 1"
    ).fetchone()["n"]
    assert active == 1


def test_remember_this_quarantines_a_credential_before_any_model_call(seeded_conn):
    """Triage runs FIRST -- the secret must never reach the extractor."""
    client = _FakeExtractionClient(_extraction())
    result = tools.remember_this(
        seeded_conn, content="Remember my OTP is 47291 for the vendor portal.", client=client
    )
    assert result["remembered"] is False
    assert result["reason"] == "quarantined_before_extraction"
    assert client.calls == 0, "the extraction model must never see a flagged credential"

    status = seeded_conn.execute(
        "SELECT extraction_status FROM captures WHERE capture_id = ?", (result["capture_id"],)
    ).fetchone()["extraction_status"]
    assert status == "pii_detected"


def test_remember_this_reports_a_model_rejection_honestly(seeded_conn):
    client = _FakeExtractionClient(_extraction(
        extraction_status="transient_discard",
        discard_reason="casual chatter with no durable signal",
    ))
    result = tools.remember_this(seeded_conn, content="ugh, mondays", client=client)
    assert result["remembered"] is False
    assert "chatter" in result["summary"]


def test_remember_this_reports_when_nothing_durable_was_found(seeded_conn):
    client = _FakeExtractionClient(_extraction())  # processed, but empty
    result = tools.remember_this(seeded_conn, content="ok then", client=client)
    assert result["remembered"] is False
    assert result["reason"] == "nothing_durable_found"


def test_remember_this_rejects_empty_content(seeded_conn):
    assert "error" in tools.remember_this(seeded_conn, content="   ")


def test_remember_this_logs_a_decision_for_audit(seeded_conn):
    from kivi.models.extraction import Fact

    client = _FakeExtractionClient(_extraction(facts=[Fact(
        entity_mention="Meridian project", entity_type="project", attribute="status",
        value_text="on track", precision_class="exact_source", asserter_role="self",
    )]))
    result = tools.remember_this(seeded_conn, content="Meridian is on track.", client=client)
    row = seeded_conn.execute(
        "SELECT decision, facts_created FROM decision_logs WHERE capture_id = ?",
        (result["capture_id"],),
    ).fetchone()
    assert row["decision"] == "memorized"
    assert row["facts_created"] == 1


def test_remember_this_capture_provenance_is_honest(seeded_conn):
    """A conversational capture must not be labelled as a dictation."""
    from kivi.models.extraction import Fact

    client = _FakeExtractionClient(_extraction(facts=[Fact(
        entity_mention="Meridian project", entity_type="project", attribute="priority",
        value_text="high", precision_class="exact_source", asserter_role="self",
    )]))
    result = tools.remember_this(seeded_conn, content="Meridian is high priority now.", client=client)
    row = seeded_conn.execute(
        "SELECT source_modality, foreground_app FROM captures WHERE capture_id = ?",
        (result["capture_id"],),
    ).fetchone()
    assert row["source_modality"] == "manual_edit"
    assert row["foreground_app"] == "Hey Kivi"


def test_prompt_distinguishes_the_two_write_paths():
    from kivi.retrieval.agent import SYSTEM_PROMPT

    assert "remember_this" in SYSTEM_PROMPT
    assert "save_memory" in SYSTEM_PROMPT
    assert "Never call both for the same content" in SYSTEM_PROMPT


def test_rendered_prompt_contains_no_jinja_delimiters():
    """instructor treats {{...}} and {%...%} in a system message as a Jinja
    template and refuses the call outright against Google GenAI -- every
    agent case failed with 'Jinja templating is not supported in system
    messages' after a doubled brace reached the rendered prompt through
    TOOL_DESCRIPTIONS, which is a plain string and so keeps its braces
    verbatim rather than having them collapsed by the f-string below it."""
    from kivi.retrieval.agent import SYSTEM_PROMPT

    for token in ("{{", "}}", "{%", "%}"):
        assert token not in SYSTEM_PROMPT, f"rendered system prompt contains {token!r}"
