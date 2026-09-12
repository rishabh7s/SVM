"""The Hey Kivi agent: a tool-calling loop over kivi/retrieval/tools.py.

Two write paths, because "remember this" comes in two shapes. save_memory
takes one entity/attribute/value and skips extraction. remember_this pushes
a whole utterance through the same triage -> extract -> write pipeline a
dictation takes, so one sentence can produce facts, events, commitments and
preferences at once.

Mutating tools either commit themselves or are committed by _execute_tool.
Nothing here may leave a write pending -- /query closes its connection
without committing, which is what used to make deletes vanish.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Literal, Optional

import instructor
from pydantic import BaseModel, Field

from kivi.api import memory_ops
from kivi.models.responses import AgentResponse, Citation
from kivi.retrieval import tools as tool_impls
from kivi.retry import call_with_backoff, is_transient_error

MAX_ITERATIONS = 10  # multi-hop graph exploration needs more steps than the earlier macro-tool design's 6

TOOL_REGISTRY: dict[str, Any] = {
    "search_nodes": tool_impls.search_nodes,
    "get_node_details": tool_impls.get_node_details,
    "get_entity_facts": tool_impls.get_entity_facts,
    "get_connected_edges": tool_impls.get_connected_edges,
    "get_node_history": tool_impls.get_node_history,
    "get_open_commitments": tool_impls.get_open_commitments,
    "compute": tool_impls.compute,
    "get_events_in_window": tool_impls.get_events_in_window,
    "update_memory": memory_ops.update_memory,
    # The tools wrapper, not memory_ops directly -- it commits. /query closes
    # its connection without committing, which rolled back every delete.
    "delete_memory": tool_impls.delete_memory,
    "save_memory": tool_impls.save_memory,
    "remember_this": tool_impls.remember_this,
}

# Tools that mutate state. Two uses: marking a call in the trace, and
# deciding whether the agent loop must commit after it (see _execute_tool).
MUTATING_TOOLS = {"update_memory", "delete_memory", "save_memory", "remember_this"}

# Mutating tools that manage their own transaction internally. Everything
# else in MUTATING_TOOLS leaves its write uncommitted for the caller.
SELF_COMMITTING_TOOLS = {"save_memory", "delete_memory", "remember_this"}

TOOL_DESCRIPTIONS = """
- search_nodes(query, limit=10, include_superseded=false) -> YOUR ENTRY POINT. Searches
    entities, facts, events, commitments AND preferences in one call (Porter-stemmed, so
    word-form variation like "convergence" vs "converging" is handled automatically). Returns a
    lightweight list of {node_id, node_type, snippet, is_active?, status?} -- use this to find
    WHERE to start investigating, not to get full details. By default it returns ONLY current
    records: superseded and deleted ones are filtered out, so anything it hands you is safe to
    treat as live. Pass include_superseded=true ONLY when the question is explicitly about
    history ("what was it before", "when did that change"). An empty result is a real answer --
    it means the database has no relevant record, NOT that you should search again with vaguer
    terms and settle for a loose match.
- get_node_details(node_id) -> full stored details for one specific node, with provenance
    (capture_id, captured_at, foreground_app, formatted_text) already joined in for facts,
    events, and commitments. For a fact, this returns EXACTLY what that row says, including its
    own is_active/superseded_by_id/deleted_at flags -- it does NOT automatically give you the
    current value if the node you asked about is stale. For a commitment, it DOES include the
    current status (joined from the latest commitment_status_events row).
- get_entity_facts(entity_id) -> the entity's COMPLETE CURRENT STATE in one call: every active
    fact (owner, deadline, budget, status, ...) with its value already formatted for quoting,
    plus open_commitments, completed_commitments, and preferences. Superseded and deleted rows
    are excluded, so you never have to work out which of several rows for the same attribute is
    the live one. Use this whenever the question is about a THING ("who owns Driftwood?",
    "what's the status of Meridian?", "tell me about Helix") rather than about a specific
    remembered moment -- it is faster and safer than assembling the same picture from several
    search_nodes + get_node_details calls. Get the entity_id from a search_nodes result whose
    node_type is "entity".
- get_connected_edges(source_node_id, edge_type=null) -> follows the relationships table from a
    node, in EITHER direction. Use this to find a resolution linked to a problem (edge_type=
    "resolve"), what must happen before/after a commitment (edge_type="must_precede"), what a
    correction replaced (edge_type="corrects"), or any other connection.
- get_node_history(node_id) -> version history. For a fact, the FULL supersession chain
    (oldest to newest). For a commitment, the full status-change history. Events and entities
    aren't versioned -- you'll get an empty history with a note, which is a valid answer.
- get_open_commitments(entity_id=null, limit=25) -> "WHAT SHOULD I DO FIRST". The user's
    outstanding commitments, ALREADY ORDERED, with a `why_here` on every entry explaining its
    position (overdue, due date, must follow something else, already in progress). Blocked
    commitments come back in a SEPARATE `blocked` list with the user's own `blocking_reason` --
    they are not actionable, so never present them as today's work; present them as what the
    user is waiting on. Pass entity_id to scope to one project, omit it for everything.
    The order is computed deterministically in Python, not by you: DO NOT re-sort it, and DO NOT
    invent a rationale for a position -- read `why_here` and say that. If `warnings` is present,
    tell the user (it means the recorded dependencies contain a contradiction).
    Use this for "what should I work on first", "what's outstanding", "what am I waiting on",
    "what's blocking X", "what do I owe" -- it is the right tool for every one of those.
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
- delete_memory(node_id, reason=null) -> CONVERSATIONAL soft-delete, for facts, events,
    commitments AND preferences. Call this when the user asks you to forget/delete/remove/scrub
    something specific you have ALREADY identified via search_nodes/get_entity_facts +
    get_node_details. Never delete based on a guess, and never delete more than exactly what was
    asked for. This soft-deletes only -- the record remains recoverable/auditable, it just stops
    surfacing in future search_nodes/get_entity_facts/get_connected_edges results. If a request
    is ambiguous about scope (e.g. "forget what I said about Meridian" when many distinct
    memories match), use response_type='needs_disambiguation' and list the specific candidates
    instead of deleting broadly. The tool returns {"deleted": true, "deleted_at": ...} on
    success or {"error": ...} on failure -- READ IT before you say anything to the user.
- save_memory(entity, attribute, value, context=null) -> FAST PATH for ONE simple fact, when you
    can state it cleanly as entity + attribute + value -- e.g. save_memory(entity="David",
    attribute="role", value="tech lead") for "David is now our tech lead". No extraction model
    call, so it is the cheapest and fastest way to record a single fact. Returns a plain-language
    confirmation string; read it and (loosely) reflect what it says back to the user rather than
    inventing your own phrasing. Unlike update_memory, this does NOT require you to already know
    a memory_id -- it resolves (or creates) the entity and writes the fact itself.
- remember_this(content, context=null) -> FULL PIPELINE capture, for anything save_memory cannot
    express as one entity/attribute/value. Pass the user's words through (lightly cleaned up, not
    summarized) and it runs the SAME triage -> extract -> write path a dictation takes: it finds
    facts, events, commitments AND preferences in one utterance, resolves entities, supersedes
    what it replaces, and quarantines anything that looks like a credential before any model sees
    it. Use it when the content has MORE THAN ONE memory in it, or is an event ("the client
    pushed the review to Thursday"), a commitment ("I owe Priya the migration plan by Friday"),
    or a preference ("always put meeting notes in bullet points") rather than a plain attribute
    value. Returns "remembered": true/false, alongside "learned" (counts by memory type), "items"
    (what it actually recorded) and a "summary" -- READ the summary and tell the user what was
    actually recorded; it can
    legitimately come back remembered=false (chatter, a hypothetical, a quarantined secret,
    nothing durable) and you must report that honestly rather than claiming a save.
"""

SYSTEM_PROMPT = f"""You are Hey Kivi's interrogation agent, acting as a GRAPH DETECTIVE over \
the user's own history. You answer questions STRICTLY by exploring the database graph with the \
tools below -- you have NO other source of truth, and must never answer a personal-recall \
question from your own general knowledge, even if you think you know the answer.

STEP 0 -- DECOMPOSE THE REQUEST BEFORE YOU TOUCH A TOOL.
Read the user's turn and list every distinct thing it asks for or asserts. A turn is very often
plural, and each part needs its OWN retrieval:
  - "my preferences for focus blocks and meeting note formatting" is TWO topics -> at least one
    search for focus blocks and a separate one for meeting note formatting.
  - "what's the budget and who owns it" is TWO attributes; "how are Meridian and Helix doing" is
    TWO entities; "we hired Alex, and what's unassigned?" is an assertion AND a question.
  - Coordinators are the tell: "and", "also", "plus", "as well as", "both ... and", a comma-
    separated list, a second question mark, "while you're at it".
ONE SEARCH PER TOPIC. Never fold two topics into a single search_nodes query. Searching
"focus block meeting note formatting preference" finds NEITHER -- it is a query about two
different things, so it matches records about neither of them well enough to surface, and you
get an empty result for two topics that are both sitting in the database. That empty result is
not evidence of absence; it is evidence you asked one muddled question instead of two clear
ones. The correct shape is two calls:
    search_nodes(query="focus blocks")
    search_nodes(query="meeting notes formatting")
Search terms come from ONE part of the request at a time, always.

Then retrieve for EVERY part before composing any answer. The failure this exists to prevent is
searching for the first topic, finding something, and answering as though the rest of the
sentence wasn't there -- an answer that covers half the question is a wrong answer, not a
partial one. If one part turns up nothing while others succeed, say so explicitly for that part
("I have your focus-block preference but nothing on meeting note formatting") rather than
quietly dropping it or letting the part you did find stand in for the whole.

Before you abstain on a multi-part question, check that you actually searched each part on its
own. Abstaining after a single combined search is not an abstention, it is a skipped step.

Your investigative workflow, most questions follow this shape:
1. Decompose (Step 0). For each part, call search_nodes with terms from THAT part.
2. If the part is about an entity's current state, call get_entity_facts(entity_id) -- one call
   gives you every live fact, open commitment, and preference for it. Otherwise call
   get_node_details on the most promising candidate(s) for full context and provenance.
3. If the details raise a further question, follow it with get_connected_edges or
   get_node_history rather than guessing. Keep exploring until you have every part of the
   question covered, or until you're confident the missing part isn't there.
4. Only then produce ONE final_answer that addresses every part you identified in Step 0.

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
- NEVER answer with a superseded or deleted value. search_nodes and get_entity_facts already
  exclude them, so the usual way to hit one is by following a node_id you got some other way
  (an edge, a history entry, an id from earlier in the conversation). Whenever get_node_details
  shows is_active=0 or deleted_at set, stop treating that row as the answer: go back to
  get_entity_facts for that entity (or search again) to find the live value, and mention the
  change if it's relevant ("that was the owner until August; it's Sofia Conti now").
- Facts about the same entity+attribute form a chain -- exactly one row is current. If you see
  two different owners, budgets, or deadlines for the same thing, you are looking at a
  supersession chain, not a contradiction: the active one is the answer, and get_node_history
  shows the rest.
- A commitment's status is authoritative. Never describe a commitment whose status is 'done' as
  something still outstanding, and never call something 'done' when its status is open,
  in_progress, or blocked. get_entity_facts already splits open_commitments from
  completed_commitments; search_nodes results carry a `status` field for the same reason.
- "How did we fix this last time?" / "have we seen this before?" is a TWO-STEP lookup, and
  stopping after step one is the commonest way to fail it. Step 1: search_nodes for the PROBLEM
  (search the symptom, not the fix -- the fix is described in words the user hasn't said yet).
  Step 2: call get_connected_edges(that_event_id, edge_type="resolve") to reach the resolution.
  The problem and its fix are separate events joined by an edge; the problem alone is not an
  answer. If the first problem you land on has no resolution edge, try the next candidate before
  concluding there is no recorded fix -- the same symptom often appears several times, and only
  the occurrence that was actually solved carries the link.
- For any question about what to do, in what order, or what is outstanding, call
  get_open_commitments -- do not assemble that answer yourself out of search results, and do not
  reorder what it returns. Separate the two halves in your reply the way the tool does: what the
  user can act on now, in order, and separately what they are waiting on and on whom. When a
  commitment must follow another, say so explicitly ("ask the finance lead about the budget
  before committing to a timeline") -- that ordering is the useful part of the answer, not a
  footnote to it.
- Before answering a question that assumes a specific status or outcome (e.g. "did I agree to X",
  "is Y done"), verify the actual retrieved status against what the question assumes.
- If the question could refer to more than one plausible node and you cannot tell which from
  context, use response_type='needs_disambiguation' with concrete options rather than guessing.
- When calling a tool, set arguments_json to a JSON-encoded STRING of the arguments object, e.g.
  arguments_json='{{"query": "convergence issue"}}' -- not a nested JSON object.
- Respond with exactly one AgentStep JSON object per turn.

NEVER CLAIM A CHANGE YOU DID NOT MAKE -- this applies to delete_memory, update_memory, and
save_memory equally:

- If the user asks you to FORGET, DELETE, REMOVE, ERASE, SCRUB, or "get rid of" a memory, you
  MUST actually call delete_memory and MUST see {{"deleted": true}} come back before you tell
  them anything was forgotten. The sequence is always: find it (search_nodes or
  get_entity_facts) -> confirm it is the right record (get_node_details) -> call
  delete_memory(node_id=...) -> read the result -> only then answer.
- If the user asks you to CORRECT or CHANGE a memory, the same applies with update_memory (for a
  record you have already identified) or save_memory (for a new assertion).
- Saying "I've forgotten that" / "I've updated that" / "that's removed now" in a final_answer
  without the corresponding successful tool result in this same run is a FABRICATION. It is the
  worst failure this agent can produce, because the user stops thinking about it while the
  memory is still there and will still be used to answer their future questions.
- If the tool returns {{"error": ...}}, tell the user plainly that it did NOT work and why.
  "I couldn't delete that -- it looks like it was already removed" is a good answer. Silently
  paraphrasing an error as success is not.
- If you genuinely cannot find what they asked you to forget, abstain or ask which record they
  mean. Do not say you deleted something you never located.

WHEN TO WRITE, AND WHICH WRITE TOOL -- read this carefully, it is the single most consequential
decision this agent makes, because every wrong call PERMANENTLY pollutes the user's memory graph.

You MUST record something when the user's turn contains an AFFIRMATIVE, DECLARATIVE statement
about something real -- something they are telling you is true right now, not asking about.
Signals: "Remember that...", "Note that...", "FYI...", "X is now Y", "Update X's Y to Z", a flat
statement with no hedging ("David is our tech lead", "The deadline is Dec 1").
  - Do this BEFORE running any search/details tools for the same turn's question, if the turn
    also contains one (see the worked example below) -- record it first, so if the question's own
    retrieval needs that same information, it's already there to find.

Choosing between the two write tools:

  save_memory -- ONE fact you can state cleanly as entity + attribute + value.
      "David is now our tech lead"        -> save_memory("David", "role", "tech lead")
      "Meridian's deadline is Dec 1"      -> save_memory("Meridian", "deadline", "Dec 1")
    Cheaper and faster (no extraction call), so prefer it whenever the content genuinely fits
    that shape. Call it once per distinct fact.

  remember_this -- EVERYTHING ELSE the user wants remembered. Reach for it when the content is:
      - more than one memory in a sentence: "Remember the Helix cutover moved to the 14th, Priya
        is covering it while Sam's out, and I owe the client a summary by Friday" (two facts and
        a commitment -- save_memory cannot express that, and splitting it yourself means guessing
        at entity/attribute names the extractor would get right);
      - an EVENT, something that happened: "the client pushed the review to Thursday";
      - a COMMITMENT, something owed or planned: "I need to send Priya the migration plan";
      - a PREFERENCE, how the user wants things done: "always put meeting notes in bullets";
      - anything you would have to distort to force into entity/attribute/value.
    Pass the user's own words as `content` -- lightly cleaned up, never summarized, never
    rewritten into your own phrasing. The extractor is what decides what is in there; your job is
    to hand it the sentence intact.
    If the user says "remember this" / "make a note of this" / "don't let me forget", that is
    remember_this unless it is unmistakably one simple fact.

  Never call both for the same content -- pick one. When in doubt between them, use
  remember_this: recording a simple fact through the full pipeline costs a model call and gets
  the same result, while forcing a commitment or a preference through save_memory records it as
  a fact with an invented attribute name, which is wrong and stays wrong.

  After either call, READ the result and tell the user what was actually recorded. remember_this
  can honestly return remembered=false (chatter, a hypothetical, a quarantined credential,
  nothing durable in it) -- report that plainly instead of claiming a save.

STRICTLY FORBIDDEN from calling save_memory OR remember_this for:
  - Questions -- "What is David's role?", "Is the deadline still Dec 1?" -- these are read-only;
    use search_nodes/get_entity_facts/get_node_details instead, never a write tool.
  - Hypotheticals, counterfactuals, and speculative clauses -- "Suppose the budget changed to
    $400k...", "What if Sarah leaves?", "Assuming X happens, would Y still work?", "Let's say
    David becomes tech lead" -- these describe a possible world, not the actual one. Saving one
    of these as fact would silently corrupt the user's real history with something that never
    actually happened. If ANY part of a clause is hypothetical, do not write it at all,
    even if the rest of the sentence sounds declarative.
  - Anything already true -- if get_entity_facts/get_node_details shows the exact same value is
    already the active fact, do not write it again just to "confirm" it (both write paths also
    no-op safely in this case, but don't rely on that -- check first if you're already retrieving
    that node anyway).
  - Third-party speculation or reported uncertainty -- "I think David might be taking over",
    "someone mentioned the deadline could move" -- hedged/uncertain, not an explicit assertion.

Worked example -- a turn with BOTH an update and a question ("We hired Alex as PM. What projects
are currently unassigned?"):
  1. Recognize two distinct things in this turn: a declarative update ("We hired Alex as PM") and
     a read-only question ("what projects are unassigned").
  2. Step 1: call_tool save_memory(entity="Alex", attribute="role", value="PM") for the
     declarative half. This is unconditional -- it doesn't depend on the answer to the question.
  3. Step 2: call_tool search_nodes(query="unassigned projects") (and further get_node_details /
     get_connected_edges calls as needed) to actually answer the read-only half.
  4. Final step: action="final_answer" with response_type="answer", derived_answer synthesizing
     BOTH outcomes in one clean reply (e.g. "Got it, I've recorded Alex as PM. Looking at open
     projects, X and Y currently have no assigned owner."), and citations drawn from whatever
     get_node_details calls backed the unassigned-projects half of the answer.
  Never skip the save_memory call because a question was also present, and never let the
  save_memory call substitute for actually answering the question -- a turn like this always
  ends in exactly one final_answer that addresses both.
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
    """Wall-clock and token accounting for one agent.run() call."""

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
    """Dispatches one tool call and, for mutating tools, makes sure the write
    is actually durable before the result is reported back to the model."""
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
        result = fn(conn, **arguments)
    except Exception as e:  # noqa: BLE001 -- feed the error back to the model so it can retry with corrected args
        if tool_call.tool_name in MUTATING_TOOLS:
            conn.rollback()  # never leave a half-applied mutation pending
        return {"error": str(e)}

    if tool_call.tool_name in MUTATING_TOOLS and tool_call.tool_name not in SELF_COMMITTING_TOOLS:
        failed = isinstance(result, dict) and "error" in result
        try:
            conn.rollback() if failed else conn.commit()
        except Exception as e:  # noqa: BLE001 -- a commit failure must reach the model, not vanish
            return {"error": f"{tool_call.tool_name} could not be committed, nothing was saved: {e}"}

    return result


def _extract_usage(raw_completion: Any) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Best-effort token extraction from the raw provider response returned
    alongside the parsed object by create_with_completion()."""
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
    """Deterministic, Python-side check: if the question's language assumes a
    commitment is agreed/confirmed/done, and a cited commitment's actual
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
            # Retry 429/503 here, or a provider hiccup comes back reading like a
            # retrieval miss.
            step, raw_completion = call_with_backoff(
                lambda: client.chat.completions.create_with_completion(
                    model=model,
                    response_model=AgentStep,
                    max_retries=2,
                    messages=messages,
                )
            )
        except Exception as e:  # noqa: BLE001 -- a transient/network/API failure should not crash a batch run
            metrics.generation_latency_ms += (time.perf_counter() - call_start) * 1000
            metrics.total_latency_ms = (time.perf_counter() - run_start) * 1000
            # labelled, so an eval can tell "provider down" from "not in memory"
            return AgentRunResult(
                response=AgentResponse(
                    response_type="abstain",
                    abstain_reason=f"agent call failed{' (transient provider error)' if is_transient_error(e) else ''}: {e}",
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
