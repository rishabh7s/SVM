"""
Shared text-normalization/similarity helpers, used by both entity resolution
(ingestion) and fuzzy lookup tools (retrieval) so the two don't quietly drift
into two different notions of "similar enough."
"""

from __future__ import annotations

import re

_PREFIX_MATCH_LEN = 5  # two tokens count as "the same word" if they share this many leading chars

# Common function words filtered out before scoring -- meaningful for
# longer free-text matching (event/problem descriptions), where grammatical
# filler otherwise dilutes genuine overlap far more than it does for short
# entity-alias comparisons. Deliberately small and hand-picked, not a
# standard NLP stopword list -- consistent with keeping v1 matching
# rule-based rather than pulling in an NLP dependency.
_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "in", "on", "at", "for", "and", "or", "not", "no",
    "due", "between", "with", "this", "that", "it", "as", "by",
}


def normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9\s]", "", text.lower()).strip()


def _tokens_match(a: str, b: str) -> bool:
    """Exact match, or a shared-prefix heuristic that catches simple word-form
    variation (converge/converging, meeting/meetings) without pulling in a
    real stemmer. Both tokens must be long enough that a shared prefix is
    actually meaningful -- 'a' and 'as' sharing a prefix would be noise."""
    if a == b:
        return True
    if len(a) >= _PREFIX_MATCH_LEN and len(b) >= _PREFIX_MATCH_LEN:
        return a[:_PREFIX_MATCH_LEN] == b[:_PREFIX_MATCH_LEN]
    return False


def token_overlap(a: str, b: str) -> float:
    tokens_a = set(normalize(a).split()) - _STOPWORDS
    tokens_b = set(normalize(b).split()) - _STOPWORDS
    if not tokens_a or not tokens_b:
        return 0.0

    remaining_b = set(tokens_b)
    matched = 0
    for ta in tokens_a:
        hit = next((tb for tb in remaining_b if _tokens_match(ta, tb)), None)
        if hit is not None:
            matched += 1
            remaining_b.discard(hit)

    union_size = len(tokens_a) + len(tokens_b) - matched
    return matched / union_size if union_size else 0.0
