from __future__ import annotations

import os

from google import genai
import instructor
from dotenv import load_dotenv

load_dotenv()  # picks up .env in the working directory if present; harmless if absent


# API key check
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Default model string. gemini-1.5-flash and gemini-2.0-flash are both fully
# shut down as of 2026 (any call to them 404s) -- gemini-3.6-flash is the
# current stable production Flash tier as of this writing. Strips a
# 'gemini/', 'google/', or 'models/' prefix if the caller includes a
# provider/API-path prefix in the env var, since the google-genai SDK expects
# the bare model id (e.g. 'gemini-3.6-flash', not 'models/gemini-3.6-flash').
RAW_MODEL_NAME = os.environ.get("KIVI_EXTRACTION_MODEL", "gemini-3.6-flash")
DEFAULT_EXTRACTION_MODEL = (
    RAW_MODEL_NAME.replace("gemini/", "").replace("google/", "").replace("models/", "")
)


def get_extraction_client() -> instructor.Instructor:
    """Returns an instructor-wrapped Gemini client configured for structured
    extraction output. Raises a clear, actionable error immediately if the
    API key is missing, rather than letting a cryptic auth error surface
    later from inside a batch ingestion run."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and set your key, "
            "or export GEMINI_API_KEY in your shell before running ingestion."
        )

    # Initialize native GenAI client with explicit API Key
    raw_client = genai.Client(api_key=GEMINI_API_KEY)

    # Wrap client with instructor using JSON mode for schema resilience
    return instructor.from_genai(
        client=raw_client,
        mode=instructor.Mode.JSON,
    )


if __name__ == "__main__":
    # Manual smoke test -- NOT part of the automated test suite, since it
    # makes a real network call and costs a token. Run directly with:
    #     python -m kivi.llm
    # after setting GEMINI_API_KEY, to confirm the client actually round-trips
    # a structured extraction correctly against the real API.
    from kivi.models.extraction import ExtractionResult

    client = get_extraction_client()
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
