"""
In-memory session state for the conversational engine: rolling turn
history per session_id, plus a pending-clarification slot for the
active-clarification flow (see kivi/retrieval/condensation.py and
kivi/api/app.py's /query handler).

LIMITATION, stated plainly: this is a single-process, in-memory store.
Sessions are lost on restart and are not shared across multiple server
processes/workers. That's an acceptable scope boundary for this build (a
narrow, inspectable demo backend, not a production multi-instance
deployment) -- swapping this module's storage for Redis or a database table
later would not require changing anything in app.py or condensation.py,
since both only interact with the SessionStore interface below, never with
a raw dict.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

MAX_HISTORY_TURNS = 6  # how many past (user, assistant) turns are fed into condensation -- see condensation.py


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


class SessionTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    timestamp: str = Field(default_factory=_now_iso)


class PendingClarification(BaseModel):
    """Set when the agent's search genuinely came up empty and the system
    asked the user for a missing anchor (an entity name, a date, an app)
    instead of just abstaining. Holds enough context that when the user's
    next message arrives, it can be merged with the original question
    rather than treated as a brand-new, unrelated query."""

    original_question: str
    condensed_question: str  # the question AFTER condensation, before the failed search
    missing_info_description: str  # the agent's own explanation of what's missing
    created_at: str = Field(default_factory=_now_iso)


class SessionState(BaseModel):
    session_id: str
    turns: list[SessionTurn] = Field(default_factory=list)
    pending_clarification: Optional[PendingClarification] = None

    def add_turn(self, role: Literal["user", "assistant"], content: str) -> None:
        self.turns.append(SessionTurn(role=role, content=content))

    def recent_history(self, max_turns: int = MAX_HISTORY_TURNS) -> list[SessionTurn]:
        return self.turns[-max_turns:]


class SessionStore:
    """Deliberately the only object in the codebase that touches the raw
    session dict -- everything else goes through get_or_create /
    save methods, so the storage backend can change without touching
    callers."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: Optional[str] = None) -> SessionState:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        new_id = session_id or f"sess_{uuid.uuid4().hex[:12]}"
        state = SessionState(session_id=new_id)
        self._sessions[new_id] = state
        return state

    def save(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state

    def exists(self, session_id: str) -> bool:
        return session_id in self._sessions

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def count(self) -> int:
        return len(self._sessions)


# Module-level singleton -- one store per process, matching the
# single-process limitation stated in the module docstring above.
SESSION_STORE = SessionStore()
