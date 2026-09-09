from __future__ import annotations

import os

from google import genai
import instructor
from dotenv import load_dotenv

load_dotenv()  # picks up .env in the working directory if present; harmless if absent


# API key check
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")


def _sanitize_model_name(raw: str) -> str:
    """Strips a 'gemini/', 'google/', or 'models/' prefix if the caller
    includes a provider/API-path prefix in the env var, since the
    google-genai SDK expects the bare model id."""
    return raw.replace("gemini/", "").replace("google/", "").replace("models/", "")


# Default model string for ingestion extraction. gemini-1.5-flash and
# gemini-2.0-flash are both fully shut down as of 2026 (any call to them
# 404s) -- gemini-3.6-flash is the current stable production Flash tier as
# of this writing.
DEFAULT_EXTRACTION_MODEL = _sanitize_model_name(os.environ.get("KIVI_EXTRACTION_MODEL", "gemini-3.6-flash"))

# Retrieval-side model tiering. LIGHT_MODEL is used for cheap, single-shot,
# non-agentic calls -- currently just query condensation
# (kivi/retrieval/condensation.py), which needs speed over deep reasoning:
# rewriting "what was the budget for it?" into a self-contained query given
# recent chat history is a much smaller task than the graph-detective loop
# itself. RETRIEVAL_MODEL is the model driving that full tool-calling loop
# (kivi/retrieval/agent.py). Both default to real, current models --
# gemini-3.5-flash-lite is the fastest/cheapest current Flash-lite tier,
# well suited to condensation; gemini-3.6-flash is the same default used for
# extraction, kept as a SEPARATE env var from KIVI_EXTRACTION_MODEL even
# though they currently default to the same string, since ingestion and
# retrieval are conceptually separate concerns that may want to diverge later.
LIGHT_MODEL = _sanitize_model_name(os.environ.get("KIVI_LIGHT_MODEL", "gemini-3.5-flash-lite"))
RETRIEVAL_MODEL = _sanitize_model_name(os.environ.get("KIVI_RETRIEVAL_MODEL", "gemini-3.6-flash"))


def get_client() -> instructor.Instructor:
    """Returns an instructor-wrapped Gemini client. This ONE client serves
    every model tier (extraction, light/condensation, retrieval/agent) --
    the model string is chosen per-call via the `model=` kwarg passed to
    client.chat.completions.create(...), not by constructing a separate
    client per model. There's no need for multiple genai.Client objects
    while everything sits behind one provider and one API key.

    Raises a clear, actionable error immediately if the API key is missing,
    rather than letting a cryptic auth error surface later from inside a
    batch run."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and set your key, "
            "or export GEMINI_API_KEY in your shell before running ingestion."
        )

    raw_client = genai.Client(api_key=GEMINI_API_KEY)
    return instructor.from_genai(
        client=raw_client,
        mode=instructor.Mode.JSON,
    )


# Kept as an alias -- kivi/ingestion/extractor.py already imports this name.
# get_client() above is the same implementation under a more accurate name
# now that this client is used for retrieval and condensation too, not just
# extraction.
get_extraction_client = get_client


if __name__ == "__main__":
    # Manual smoke test -- NOT part of the automated test suite, since it
    # makes a real network call and costs a token. Run directly with:
    #     python -m kivi.llm
    # after setting GEMINI_API_KEY, to confirm the client actually round-trips
    # a structured extraction correctly against the real API.
    from kivi.models.extraction import ExtractionResult

    client = get_client()
    print(f"[llm smoke test] using model: {DEFAULT_EXTRACTION_MODEL!r}")
    result = client.chat.completions.create(
        model=DEFAULT_EXTRACTION_MODEL,
        response_model=ExtractionResult,
        max_retries=3,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract structured memory from a single dictation capture. "
                    "The capture_id for this record is 'smoketest_001'. "
                    "Set extraction_status='processed' unless the content is clearly "
                    "casual chatter, a secret/credential, or truncated."
                ),
            },
            {
                "role": "user",
                "content": "Meeting with the design team moved to 3pm tomorrow.",
            },
        ],
    )
    print(result.model_dump_json(indent=2))
