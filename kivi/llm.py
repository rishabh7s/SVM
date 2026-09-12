from __future__ import annotations

import os
from typing import Optional

from google import genai
import instructor
from dotenv import load_dotenv

load_dotenv()  # picks up .env in the working directory if present; harmless if absent


# Read fresh each call. Snapshotting at import means a test's monkeypatch,
# or a .env loaded later, is never seen.
def _get_api_key() -> Optional[str]:
    return os.environ.get("GEMINI_API_KEY")


def _sanitize_model_name(raw: str) -> str:
    """Strips a 'gemini/', 'google/', or 'models/' prefix if the caller
    includes a provider/API-path prefix in the env var, since the
    google-genai SDK expects the bare model id."""
    return raw.replace("gemini/", "").replace("google/", "").replace("models/", "")


# 1.5-flash and 2.0-flash are both shut down; 3.6-flash is the current tier.
DEFAULT_EXTRACTION_MODEL = _sanitize_model_name(os.environ.get("KIVI_EXTRACTION_MODEL", "gemini-3.6-flash"))

# LIGHT_MODEL: condensation only -- wants speed, not depth.
# RETRIEVAL_MODEL: the agent's tool loop.
# Separate env vars even though they default to the same string today.
LIGHT_MODEL = _sanitize_model_name(os.environ.get("KIVI_LIGHT_MODEL", "gemini-3.5-flash-lite"))
RETRIEVAL_MODEL = _sanitize_model_name(os.environ.get("KIVI_RETRIEVAL_MODEL", "gemini-3.6-flash"))


def get_client() -> instructor.Instructor:
    """Returns an instructor-wrapped Gemini client."""
    if not _get_api_key():
        raise RuntimeError(
            "GEMINI_API_KEY is not set. Copy .env.example to .env and set your key, "
            "or export GEMINI_API_KEY in your shell before running ingestion."
        )

    raw_client = genai.Client(api_key=_get_api_key())
    return instructor.from_genai(
        client=raw_client,
        mode=instructor.Mode.JSON,
    )


# extractor.py imports this name. Same function, older label.
get_extraction_client = get_client


if __name__ == "__main__":
    # Manual smoke test -- real network call. python -m kivi.llm
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
