"""Counts the distinct values seen for the two open-vocabulary fields
(event_type, relationship_type) across a run.

Makes drift visible -- 'resolves' in one capture, 'resolution_of' in the
next -- without deciding what to do about it.
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
