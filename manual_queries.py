"""
Manual interactive-mode test queries, run once against a real GEMINI_API_KEY
to sanity-check response tone and citation formatting before running the
full corpus through the agent. NOT part of the automated test suite -- makes
real network calls.

Run with:
    python -m kivi.retrieval.manual_queries

Each query below is chosen to exercise a specific behavior already unit-
tested in isolation (test_agent_deterministic.py / test_retrieval_tools.py):
this script is the first place those pieces run together through an actual
model, not a replacement for those tests.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from kivi.llm import DEFAULT_EXTRACTION_MODEL, get_extraction_client
from kivi.retrieval.agent import run

DB_PATH = "db/kivi.db"
QUERIES = [
    # Straightforward active-fact lookup -- expect response_type='answer' with a citation to cap_002.
    "What's the current budget for the Meridian project?",
    # Supersession/history -- expect the agent to distinguish current vs. original value.
    "What was the original Meridian budget before it changed?",
    # Solution reuse -- expect find_resolving_event to surface the fixed-step-solver fix.
    "We're seeing convergence issues in a Simulink model again, did we fix something like this before?",
    # Ordering -- expect get_ordering to surface the must_precede relationship.
    "What needs to happen before I can commit to the Meridian rollout timeline?",
    # False-premise case -- expect an explicit correction, not a confident wrong "yes".
    "Is the Meridian rollout timeline commitment done yet?",
    # Negative space -- nothing in the seeded data supports this; expect a clean abstention.
    "What did the client say about the Meridian contract renewal terms?",
    # Ambiguous in principle (only one real "budget" on file here, so expect either a direct
    # answer or, if the agent is being cautious, a disambiguation -- worth watching either way).
    "What's the budget?",
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row

    client = get_extraction_client()

    for i, question in enumerate(QUERIES, start=1):
        print("=" * 70)
        print(f"Q{i}: {question}")
        print("-" * 70)
        result = run(conn, client, DEFAULT_EXTRACTION_MODEL, question, headless_mode=False)
        print(f"response_type: {result.response.response_type}")
        if result.response.derived_answer:
            print(f"answer: {result.response.derived_answer}")
        if result.response.abstain_reason:
            print(f"abstain_reason: {result.response.abstain_reason}")
        if result.response.disambiguation_options:
            print(f"disambiguation_options: {result.response.disambiguation_options}")
        if result.response.citations:
            print("citations:")
            for c in result.response.citations:
                print(f"  - {c.source_type}:{c.source_id} -- {c.snippet}")
        print(f"(resolved in {len(result.steps)} step(s))")
        print()
        
        # Paces the queries to avoid hitting the 5 requests per minute API limit
        time.sleep(15)

    conn.close()


if __name__ == "__main__":
    main()