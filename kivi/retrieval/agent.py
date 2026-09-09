"""
The interrogation agent: a manual tool-calling loop implementing the
"Atomic Neighborhood Tools" architecture -- the model acts as a graph
detective, exploring the database one atomic step at a time. See
kivi/retrieval/tools.py's module docstring for the full architecture
rationale.

This version adds three things beyond graph traversal:

1. Conversational memory management -- update_memory / delete_memory tools,
   backed by kivi/api/memory_ops.py, the SAME deterministic implementation
   the REST API's direct UI actions use. This is what makes the dual-path
   guarantee real: whether a memory changes via a button click or a spoken
   instruction, the exact same Python function does it.

2. Timing/token instrumentation -- every model call goes through
   client.chat.completions.create_with_completion() instead of plain
   create(), so the raw provider response (with usage metadata, when the
   provider/SDK version exposes it) is available alongside the parsed
   Pydantic object. generation_latency_ms accumulates wall-clock time spent
   inside model calls; retrieval_latency_ms accumulates time spent inside
   tool execution (_execute_tool). These are two genuinely different costs
   and reported separately rather than as one blended number.

3. A structured abstain_reason convention for genuine retrieval misses --
   the model is instructed to prefix abstain_reason with 'no_matching_nodes:'
   specifically when it searched and found nothing, as opposed to any other
   reason to abstain (ambiguous target, false premise, a failed model call,
   hitting max_iterations). This lets the session layer
   (kivi/api/session_store.py + kivi/api/app.py) distinguish "ask the user
   for a missing anchor" from "just report the abstention" without the
   session layer needing to re-interpret free-text reasoning itself.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Literal, Optional

import instructor
from pydantic import BaseModel, Field

from kivi.retrieval import memory_ops
from kivi.models.responses import AgentResponse, Citation
from kivi.retrieval import tools as tool_impls

MAX_ITERATIONS = 10  # multi-hop graph exploration needs more steps than the earlier macro-tool design's 6

TOOL_REGISTRY: dict[str, Any] = {
    "search_nodes": tool_impls.search_nodes,
    "get_node_details": tool_impls.get_node_details,
    "get_connected_edges": tool_impls.get_connected_edges,
    "get_node_history": tool_impls.get_node_history,
    "compute": tool_impls.compute,
    "get_events_in_window": tool_impls.get_events_in_window,
    "update_memory": memory_ops.update_memory,
    "delete_memory": memory_ops.delete_memory,
}

# Tools that mutate state -- used only to decide whether a tool CALL is
# worth logging distinctly in the trace; not a separate registry, since
# dispatch is identical either way.
MUTATING_TOOLS = {"update_memory", "delete_memory"}

TOOL_DESCRIPTIONS = """
- search_nodes(query, limit=10) -> YOUR ENTRY POINT. Searches entities, facts, events, AND
    commitments in one call (Porter-stemmed, so word-form variation like "convergence" vs
    "converging" is handled automatically). Returns a lightweight list of {node_id, node_type,
    snippet} -- use this to find WHERE to start investigating, not to get full details.
- get_node_details(node_id) -> full stored details for one specific node, with provenance
    (capture_id, captured_at, foreground_app, formatted_text) already joined in for facts,
    events, and commitments. For a fact, this returns EXACTLY what that row says, including its
    own is_active/superseded_by_id/deleted_at flags -- it does NOT automatically give you the
    current value if the node you asked about is stale. For a commitment, it DOES include the
    current status (joined from the latest commitment_status_events row).
- get_connected_edges(source_node_id, edge_type=null) -> follows the relationships table from a
    node, in EITHER direction. Use this to find a resolution linked to a problem (edge_type=
    "resolve"), what must happen before/after a commitment (edge_type="must_precede"), what a
    correction replaced (edge_type="corrects"), or any other connection.
- get_node_history(node_id) -> version history. For a fact, the FULL supersession chain
    (oldest to newest). For a commitment, the full status-change history. Events and entities
    aren't versioned -- you'll get an empty history with a note, which is a valid answer.
- compute(operation, values) -> deterministic arithmetic; ALWAYS use this for any calculation.
- get_events_in_window(start, end, entity_id=null) -> events in an ISO-8601 time range, for
    questions not anchored to a specific node ("what happened last week").
- update_memory(memory_id, updates, note=null) -> CONVERSATIONAL memory correction. Only call
    this when the user has clearly and explicitly asked you to correct or change something you
    have ALREADY found and confirmed via get_node_details -- never guess a memory_id blind. For
    a fact: updates may set value_text/value_numeric/unit. For a commitment: status/
    status_confirmed_by_user/blocking_reason/due_date_*. For an event: description (this creates
    a new corrected event and links it back to the original via a 'corrects' edge -- the
    original is never destroyed). This performs the exact same operation as the app's own Edit
    button; nothing you can do here is possible through voice/chat alone that a person couldn't
    also do by clicking a button.
- delete_memory(memory_id, reason=null) -> CONVERSATIONAL soft-delete. Only call this when the
    user has clearly and explicitly asked to forget/remove something specific you have ALREADY
    identified via search_nodes + get_node_details. Never delete based on a guess, and never
    delete more than exactly what was asked for. This soft-deletes only -- the record remains
    recoverable/auditable, it just stops surfacing in future search_nodes/get_connected_edges
    results. If a request is ambiguous about scope (e.g. "forget what I said about Meridian" when
    many distinct memories match), use response_type='needs_disambiguation' and list the specific
    candidates instead of deleting broadly.
"""

SYSTEM_PROMPT = f"""You are Hey Kivi's interrogation agent, acting as a GRAPH DETECTIVE over \
the user's own history. You answer questions STRICTLY by exploring the database graph with the \
tools below -- you have NO other source of truth, and must never answer a personal-recall \
question from your own general knowledge, even if you think you know the answer.

Your investigative workflow, most questions follow this shape:
1. Call search_nodes with terms from the question to find candidate starting node(s).
2. Call get_node_details on the most promising candidate(s) to read the full context and
   provenance.
3. If the details raise a further question, follow it with get_connected_edges or
   get_node_history rather than guessing. Keep exploring until you have what you need, or until
   you're confident it isn't there.

Available tools:
{TOOL_DESCRIPTIONS}

Rules:
- Every 'answer' response MUST include at least one citation, built from a get_node_details
  result's capture_id/formatted_text/foreground_app fields. An answer you cannot cite should be
  an abstention instead.
- If exploring the graph genuinely turns up nothing relevant (you searched and found no matching
  nodes), abstain with abstain_reason starting EXACTLY with the prefix 'no_matching_nodes:'
  followed by a short note on what you searched for and, if you can tell, what specific piece of
  information (an entity name, a date, an app) would let a retry succeed. Example:
  abstain_reason="no_matching_nodes: no record of a 'Q3 budget' -- which project or client is this about?"
  Use this EXACT prefix only for a genuine empty search result -- for any other reason to
  abstain (an ambiguous target, a failed tool call, anything else), do not use this prefix.
- If you land on a superseded fact (is_active=0), a deleted memory (deleted_at set), or a
  commitment whose status doesn't match what the question assumes, say so explicitly and find
  the current answer -- never present a stale or deleted value as current.
- Before answering a question that assumes a specific status or outcome (e.g. "did I agree to X",
  "is Y done"), verify the actual retrieved status against what the question assumes.
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


class RunMetrics(BaseModel):
    """Wall-clock and token accounting for one agent.run() call. Token
    fields are best-effort: they're populated only when the underlying
    provider SDK response exposes usage metadata, and left null otherwise
    rather than guessed -- see _extract_usage()'s docstring."""

    model_calls: int = 0
    tool_calls: int = 0
    generation_latency_ms: float = 0.0
    retrieval_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    total_tokens: Optional[int] = None


class AgentRunResult(BaseModel):
    response: AgentResponse
    steps: list[AgentStep] = Field(default_factory=list)  # full trace, for inspection
    metrics: RunMetrics = Field(default_factory=RunMetrics)


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


def _extract_usage(raw_completion: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Best-effort token extraction from the raw provider response returned
    alongside the parsed object by create_with_completion(). Different
    provider SDKs expose usage differently (and it can change between SDK
    versions), so this tries the google-genai shape first and degrades to
    (None, None, None) rather than raising or fabricating a number --
    reporting 'unknown' is honest; reporting 0 or a guessed figure is not."""
    try:
        usage = getattr(raw_completion, "usage_metadata", None)
        if usage is not None:
            return (
                getattr(usage, "prompt_token_count", None),
                getattr(usage, "candidates_token_count", None),
                getattr(usage, "total_token_count", None),
            )
    except Exception:  # noqa: BLE001 -- token accounting must never break the actual response
        pass
    return None, None, None


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
    run_start = time.perf_counter()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    steps: list[AgentStep] = []
    metrics = RunMetrics()

    for _ in range(MAX_ITERATIONS):
        call_start = time.perf_counter()
        try:
            step, raw_completion = client.chat.completions.create_with_completion(
                model=model,
                response_model=AgentStep,
                max_retries=2,
                messages=messages,
            )
        except Exception as e:  # noqa: BLE001 -- a transient/network/API failure should not crash a batch run
            metrics.generation_latency_ms += (time.perf_counter() - call_start) * 1000
            metrics.total_latency_ms = (time.perf_counter() - run_start) * 1000
            return AgentRunResult(
                response=AgentResponse(
                    response_type="abstain",
                    abstain_reason=f"agent call failed: {e}",
                ),
                steps=steps,
                metrics=metrics,
            )
        metrics.generation_latency_ms += (time.perf_counter() - call_start) * 1000
        metrics.model_calls += 1
        p, c, t = _extract_usage(raw_completion)
        if p is not None:
            metrics.prompt_tokens = (metrics.prompt_tokens or 0) + p
        if c is not None:
            metrics.completion_tokens = (metrics.completion_tokens or 0) + c
        if t is not None:
            metrics.total_tokens = (metrics.total_tokens or 0) + t
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

            metrics.total_latency_ms = (time.perf_counter() - run_start) * 1000
            return AgentRunResult(response=response, steps=steps, metrics=metrics)

        if step.action == "call_tool" and step.tool_call is not None:
            tool_start = time.perf_counter()
            result = _execute_tool(conn, step.tool_call)
            metrics.retrieval_latency_ms += (time.perf_counter() - tool_start) * 1000
            metrics.tool_calls += 1
            messages.append({"role": "model", "content": step.model_dump_json()})
            messages.append(
                {"role": "user", "content": f"Tool result for {step.tool_call.tool_name}: {json.dumps(result, default=str)}"}
            )
            continue

        messages.append({"role": "model", "content": step.model_dump_json()})
        messages.append({"role": "user", "content": "That step was malformed -- provide either a valid tool_call or a final_response."})

    metrics.total_latency_ms = (time.perf_counter() - run_start) * 1000
    return AgentRunResult(
        response=AgentResponse(
            response_type="abstain",
            abstain_reason=f"reached max_iterations ({MAX_ITERATIONS}) without resolving to a final answer",
        ),
        steps=steps,
        metrics=metrics,
    )
