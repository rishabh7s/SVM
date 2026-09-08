"""
Pre-persistence, pre-LLM secret/PII scrub.

Runs on raw capture text BEFORE any LLM call is made, not just before
writing to the database. This is a stronger privacy property than screening
the LLM's *output* -- if a capture is flagged here, the raw text containing
the secret never leaves the local machine at all, since we skip the
extraction call entirely and mark the capture quarantined directly.

This is deliberately a fast, cheap regex + entropy pass, not a learned
classifier -- consistent with the project's "don't over-engineer v1"
approach to fuzzy matching elsewhere.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

# --- Patterns -----------------------------------------------------------

_OTP_PATTERN = re.compile(
    r"\b(?:otp|one[\s-]?time[\s-]?(?:code|pass(?:word|code)?)|verification code)\b",
    re.IGNORECASE,
)

# 'pin' alone is too common a word (e.g. "pin this to the top") to flag on
# its own -- require it to look like a credential mention specifically.
_PIN_PATTERN = re.compile(r"\bpin\b[^.\n]{0,10}?\b(is|:|number)\b", re.IGNORECASE)

_PASSWORD_PATTERN = re.compile(
    r"\bpassword\b[^.\n]{0,20}?\b(is|:)\s*([^\s.,]{4,})",
    re.IGNORECASE,
)

_CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]?){13,16}\b")

# A high-entropy token near a credential-suggestive keyword. This is the
# most likely to false-positive of the three checks, so it's the last one
# tried and requires a nearby keyword, not entropy alone.
_CREDENTIAL_KEYWORD_PATTERN = re.compile(
    r"\b(api[\s-]?key|secret|token|credential)\b[^.\n]{0,20}?([A-Za-z0-9_\-]{8,})",
    re.IGNORECASE,
)


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    length = len(s)
    return -sum((count / length) * math.log2(count / length) for count in freq.values())


@dataclass
class TriageResult:
    flagged: bool
    reason: Optional[str] = None


def triage(raw_text: str, formatted_text: str = "") -> TriageResult:
    """Screens raw + formatted capture text for secrets before any LLM call.
    Returns flagged=True with a human-readable reason the moment any pattern
    matches -- deliberately stops at the first hit rather than exhaustively
    reporting all matches, since one is already enough to quarantine the
    whole capture."""
    combined = f"{raw_text}\n{formatted_text}"

    if match := _OTP_PATTERN.search(combined):
        return TriageResult(True, f"OTP/verification-code keyword detected near '{match.group(0)[:40]}'")

    if match := _PIN_PATTERN.search(combined):
        return TriageResult(True, f"PIN keyword detected near '{match.group(0)[:40]}'")

    if match := _PASSWORD_PATTERN.search(combined):
        return TriageResult(True, "password-like pattern detected ('password is/: <value>')")

    if _CREDIT_CARD_PATTERN.search(combined):
        return TriageResult(True, "credit-card-number-like digit sequence detected")

    if match := _CREDENTIAL_KEYWORD_PATTERN.search(combined):
        candidate = match.group(2)
        if _shannon_entropy(candidate) >= 3.0:  # empirically: random-looking tokens score higher
            return TriageResult(
                True, f"high-entropy token near credential keyword detected ('{match.group(1)}')"
            )

    return TriageResult(False, None)
