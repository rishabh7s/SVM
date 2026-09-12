"""Secret and PII scan, run on raw text before any model call.

The ordering is the point: a flagged capture is quarantined locally and the
extraction call never happens, so the secret doesn't leave the machine.
Screening the model's output instead would be strictly weaker.

Regex and entropy, not a classifier.
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

# Most false-positive-prone of the three, so it needs a nearby keyword.
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
    """Screens raw + formatted capture text for secrets before any LLM call."""
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
