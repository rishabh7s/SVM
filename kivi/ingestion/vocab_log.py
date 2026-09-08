"""
Vocabulary drift logging for the two free-text fields the extraction schema
deliberately left open: event_type and relationship_type. Since nothing
constrains the LLM's wording for these, the same concept can surface as
'resolves' in one capture and 'resolution_of' in another. This module just
counts distinct values seen across a batch run and writes them out sorted by
frequency, so drift is visible for a human to decide whether to normalize at
ingestion or match fuzzily at query time -- it does not make that decision
itself.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


class VocabLogger:
    def __init__(self) -> None:
        self.event_types: Counter[str] = Counter()
        self.relationship_types: Counter[str] = Counter()

    def observe_event_type(self, value: str) -> None:
        self.event_types[value.strip().lower()] += 1

    def observe_relationship_type(self, value: str) -> None:
        self.relationship_types[value.strip().lower()] += 1

    def write(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "event_types": dict(self.event_types.most_common()),
            "relationship_types": dict(self.relationship_types.most_common()),
        }
        path.write_text(json.dumps(payload, indent=2))

    def summary(self) -> str:
        lines = [
            f"  event_type: {len(self.event_types)} distinct values across {sum(self.event_types.values())} occurrences",
            f"  relationship_type: {len(self.relationship_types)} distinct values across {sum(self.relationship_types.values())} occurrences",
        ]
        return "\n".join(lines)
