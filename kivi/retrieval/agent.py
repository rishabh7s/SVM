"""
The interrogation agent: a manual tool-calling loop (not native
function-calling) implementing the "Atomic Neighborhood Tools" architecture
-- the model acts as a graph detective, exploring the database one atomic
step at a time (search -> inspect -> follow edges -> check history) rather
than receiving a single hardcoded bundle of pre-joined context.

Why a manual loop instead of Gemini's native tool-calling: native
function-calling handling was inconsistent enough to justify using
instructor's JSON mode instead (see kivi/llm.py). The same reasoning
applies here -- the model emits one structured AgentStep per turn (either
"call this tool with these arguments" or "here is my final answer"), Python
executes the tool call deterministically, and the result is fed back as the
next message. Every step is a validated Pydantic object, fully inspectable,
never an opaque function-call blob.

Trade-off this architecture makes deliberately: MORE round trips per
question than the earlier single-macro-tool design, in exchange for the
model being able to decide for itself how far to explore the graph --
checking an asserter, following a resolution's own resolution, or deciding
a superseded value doesn't matter to the question at hand. See
kivi/retrieval/tools.py's module docstring for the full reasoning.

Two behaviors are enforced in Python, not just by prompt instruction:
  1. needs_disambiguation -> abstain collapse in headless_mode.
  2. A false-premise check against any commitment citations, run on every
     'answer' response before it's returned.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Literal, Optional

import instructor
from pydantic import BaseModel, Field

from kivi.models.responses import AgentResponse, Citation
from kivi.retrieval import tools as tool_impls

MAX_ITERATIONS = 10  # higher than the macro-tool design's 6 -- multi-hop graph exploration needs more steps

TOOL_REGISTRY: dict[str, Any] = {
    "search_nodes": tool_impls.search_nodes,
    "get_node_details": tool_impls.get_node_details,
    "get_connected_edges": tool_impls.get_connected_edges,
    "get_node_history": tool_impls.get_node_history,
    "compute": tool_impls.compute,
    "get_events_in_window": tool_impls.get_events_in_window,
}

TOOL_DESCRIPTIONS = """
- search_nodes(query, limit=10) -> YOUR ENTRY POINT. Searches entities, facts, events, AND
    commitments in one call (Porter-stemmed, so word-form variation like "convergence" vs
    "converging" is handled automatically). Returns a lightweight list of {node_id, node_type,
    snippet} -- use this to find WHERE to start investigating, not to get full details.
- get_node_details(node_id) -> full stored details for one specific node, with provenance
    (capture_id, captured_at, foreground_app, formatted_text) already joined in for facts,
    events, and commitments. For a fact, this returns EXACTLY what that row says, including its
    own is_active/superseded_by_id flags -- it does NOT automatically give you the current value
    if the node you asked about is stale. For a commitment, it DOES include the current status
    (joined from the latest commitment_status_events row) since status is part of "what this
    commitment currently says," not a separate version to opt into.
- get_connected_edges(source_node_id, edge_type=null) -> follows the relationships table from a
    node, in EITHER direction. Use this to find a resolution linked to a problem (edge_type=
    "resolve"), what must happen before/after a commitment (edge_type="must_precede"), or any
    other connection. Omit edge_type to see everything connected. Returns each connected node's
    id, type, a short snippet, and which direction the edge runs.
- get_node_history(node_id) -> version history. For a fact, the FULL supersession chain
    (oldest to newest), regardless of which fact_id in that chain you pass in. For a commitment,
    the full status-change history. Events and entities aren't versioned -- you'll get an empty
    history with a note, which is a valid answer, not an error.
- compute(operation, values) -> deterministic arithmetic ('sum'/'difference'/'average'/
    'multiply'/'divide'); ALWAYS use this for any calculation, never compute a number yourself.
- get_events_in_window(start, end, entity_id=null) -> events in an ISO-8601 time range, for
    questions not anchored to a specific node ("what happened last week").
"""

SYSTEM_PROMPT = f"""You are Hey Kivi's interrogation agent, acting as a GRAPH DETECTIVE over \
the user's own history. You answer questions STRICTLY by exploring the database graph with the \
tools below -- you have NO other source of truth, and must never answer a personal-recall \
question from your own general knowledge, even if you think you know the answer. If exploring \
the graph doesn't turn up the answer, you must abstain rather than guess.

Your investigative workflow, most questions follow this shape:
1. Call search_nodes with terms from the question to find candidate starting node(s). Don't
   overthink the query -- it's Porter-stemmed and searches everything at once.
2. Call get_node_details on the most promising candidate(s) to read the full context and
   provenance.
3. If the details raise a further question -- is there a fix for this problem? what must happen
   before this commitment? is this fact still current? -- follow it with get_connected_edges
   (for relationships) or get_node_history (for version history) rather than guessing. A single
   node in isolation is often not enough to answer the question completely; keep exploring until
   you have what you need, or until you're confident it isn't there.

Available tools:
{TOOL_DESCRIPTIONS}

Rules:
- Every 'answer' response MUST include at least one citation, built from a get_node_details
  result's capture_id/formatted_text/foreground_app fields. An answer you cannot cite should be
  an abstention instead.
- If you land on a superseded fact (is_active=0) or a commitment whose status doesn't match what
  the question assumes, say so explicitly and use get_node_history or get_node_details on the
  superseding node to find the current answer -- never present a stale value as current.
- Before answering a question that assumes a specific status or outcome (e.g. "did I agree to X",
  "is Y done"), verify the actual retrieved status against what the question assumes. If they
  don't match, say so explicitly rather than answering as if the premise were true.
- If the question could refer to more than one plausible node and you cannot tell which from
  context, use response_type='needs_disambiguation' with concrete options rather than guessing.
- When calling a tool, set arguments_json to a JSON-encoded STRING of the arguments object, e.g.
  arguments_json='{{"query": "convergence issue"}}' -- not a nested JSON object.
- Respond with exactly one AgentStep JSON object per turn.
"""


class ToolCall(BaseModel):
    tool_name: str
    arguments_json: str = Field(
        default="{}",
        description=(
            "A JSON-encoded object string of the tool's arguments, e.g. "
            '\'{"query": "convergence issue"}\'. '
            "MUST be a valid JSON object string, not a nested object -- "
            "Gemini's structured-output mode does not support open-ended "
            "object schemas, so arguments are passed as a JSON string and "
            "parsed on the Python side."
        ),
    )

    def parsed_arguments(self) -> dict[str, Any]:
        try:
            parsed = json.loads(self.arguments_json)
        except json.JSONDecodeError as e:
            raise ValueError(f"arguments_json is not valid JSON: {e}") from e
        if not isinstance(parsed, dict):
            raise ValueError("arguments_json must decode to a JSON object, not a list/scalar.")
        return parsed


class AgentStep(BaseModel):
    thought: str = Field(..., description="Brief reasoning for this step, shown only for inspection/debugging.")
    action: Literal["call_tool", "final_answer"]
    tool_call: Optional[ToolCall] = None
    final_response: Optional[AgentResponse] = None


class AgentRunResult(BaseModel):
    response: AgentResponse
    steps: list[AgentStep] = Field(default_factory=list)  # full trace, for inspection


def _execute_tool(conn: sqlite3.Connection, tool_call: ToolCall) -> Any:
    fn = TOOL_REGISTRY.get(tool_call.tool_name)
    if fn is None:
        return {"error": f"unknown tool '{tool_call.tool_name}'. Available: {list(TOOL_REGISTRY.keys())}"}
    try:
        arguments = tool_call.parsed_arguments()
    except ValueError as e:
        return {"error": str(e)}
    try:
        # compute() takes no connection; every other tool does
        if tool_call.tool_name == "compute":
            return fn(**arguments)
        return fn(conn, **arguments)
    except Exception as e:  # noqa: BLE001 -- feed the error back to the model so it can retry with corrected args
        return {"error": str(e)}


def check_false_premise(conn: sqlite3.Connection, question: str, citations: list[Citation]) -> Optional[str]:
    """Deterministic, Python-side check: if the question's language assumes
    a commitment is agreed/confirmed/done, and a cited commitment's actual
    current status says otherwise, return a correction string."""
    q_lower = question.lower()
    assumes_positive_outcome = any(
        keyword in q_lower
        for keyword in ("done", "finished", "completed", "confirmed", "agreed", "agree")
    )
    if not assumes_positive_outcome:
        return None

    for c in citations:
        if c.source_type != "commitment":
            continue
        row = conn.execute(
            "SELECT status FROM commitment_status_events WHERE commitment_id = ? AND is_active = 1",
            (c.source_id,),
        ).fetchone()
        if row and row["status"] != "done":
            return (
                f"Correction: the question assumes this was agreed/confirmed, but the record "
                f"for {c.source_id} currently shows status='{row['status']}', not done."
            )
    return None


def run(
    conn: sqlite3.Connection,
    client: instructor.Instructor,
    model: str,
    question: str,
    headless_mode: bool = False,
) -> AgentRunResult:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    steps: list[AgentStep] = []

    for _ in range(MAX_ITERATIONS):
        try:
            step: AgentStep = client.chat.completions.create(
                model=model,
                response_model=AgentStep,
                max_retries=2,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001 -- a transient/network/API failure should not crash a batch run
            return AgentRunResult(
                response=AgentResponse(
                    response_type="abstain",
                    abstain_reason=f"agent call failed: {e}",
                ),
                steps=steps,
            )
        steps.append(step)

        if step.action == "final_answer" and step.final_response is not None:
            response = step.final_response

            if headless_mode and response.response_type == "needs_disambiguation":
                response = AgentResponse(
                    response_type="abstain",
                    abstain_reason="ambiguous_action: cannot present disambiguation options in headless mode",
                )

            if response.response_type == "answer":
                correction = check_false_premise(conn, question, response.citations)
                if correction:
                    response = AgentResponse(
                        response_type="answer",
                        derived_answer=f"{response.derived_answer}\n\n{correction}",
                        citations=response.citations,
                    )

            return AgentRunResult(response=response, steps=steps)

        if step.action == "call_tool" and step.tool_call is not None:
            result = _execute_tool(conn, step.tool_call)
            messages.append({"role": "model", "content": step.model_dump_json()})
            messages.append(
                {"role": "user", "content": f"Tool result for {step.tool_call.tool_name}: {json.dumps(result, default=str)}"}
            )
            continue

        messages.append({"role": "model", "content": step.model_dump_json()})
        messages.append({"role": "user", "content": "That step was malformed -- provide either a valid tool_call or a final_response."})

    return AgentRunResult(
        response=AgentResponse(
            response_type="abstain",
            abstain_reason=f"reached max_iterations ({MAX_ITERATIONS}) without resolving to a final answer",
        ),
        steps=steps,
    )
