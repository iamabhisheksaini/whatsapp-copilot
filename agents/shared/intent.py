"""Shared intent-classifier mini-graph (LangGraph).

n8n calls this once per inbound text-only WhatsApp message and routes on the
result. Keeping it here rather than duplicating the logic inside both agents is
what the assignment asks for, and it means Agent A and Agent B stay focused on
their own domains.

Graph: Normalize -> Classify (LLM) -> Validate (schema + heuristic fallback)
"""

import datetime as dt
from typing import Any, Dict, List, Literal, Optional, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel, Field

from shared.llm import get_llm, get_logger, safe_json

log = get_logger("shared.intent")

Intent = Literal[
    "knowledge_qa",
    "lead_capture",
    "proposal_request",
    "next_step",
    "status_update",
    "smalltalk",
    "unknown",
]

VALID_INTENTS = set(Intent.__args__)  # type: ignore[attr-defined]

# `next_step` is ambiguous on its own — a call about a policy question belongs to
# Agent A, a demo for a lead belongs to Agent B. The classifier picks a lane.
Context = Literal["knowledge", "dealflow"]


class Entities(BaseModel):
    """Loosely-typed entity bag; every field is optional by design."""

    person: Optional[str] = None
    company: Optional[str] = None
    datetimeText: Optional[str] = Field(
        default=None, description="Raw date/time phrase as written by the user"
    )
    budget: Optional[str] = None
    topic: Optional[str] = None
    statusLabel: Optional[Literal["Won", "Lost", "On hold"]] = None


class IntentResult(BaseModel):
    intent: str
    context: Context = "dealflow"
    entities: Entities = Field(default_factory=Entities)
    confidence: float = 0.5


class IntentState(TypedDict, total=False):
    text: str
    requestId: str
    recentContext: Dict[str, Any]
    raw: Dict[str, Any]
    result: Dict[str, Any]


SYSTEM_PROMPT = """You classify inbound WhatsApp messages for a sales copilot.

Given the message and any recent conversation context, identify the intent and \
extract entities. Return ONLY JSON conforming to this schema:

{{
  "intent": "knowledge_qa" | "lead_capture" | "proposal_request" | "next_step" \
| "status_update" | "smalltalk" | "unknown",
  "context": "knowledge" | "dealflow",
  "entities": {{
    "person": string|null,
    "company": string|null,
    "datetimeText": string|null,
    "budget": string|null,
    "topic": string|null,
    "statusLabel": "Won"|"Lost"|"On hold"|null
  }},
  "confidence": number between 0 and 1
}}

Guidance:
- knowledge_qa: a question answerable from company documents ("what's our refund policy?").
- lead_capture: a new prospect with company/budget/timeline details.
- proposal_request: asking to draft or send a proposal or quote.
- next_step: scheduling a call, demo or meeting. Set "context" to "knowledge" when
  the meeting is about a documented topic, "dealflow" when it concerns a deal.
- status_update: a deal moved to Won, Lost or On hold.
- smalltalk: greetings and pleasantries with no task.
- unknown: anything you cannot place.

Recent context (may be empty): {recent}
Today is {today}.

Message: {text}
"""


def _normalize(state: IntentState) -> IntentState:
    return {"text": (state.get("text") or "").strip()}


def _classify(state: IntentState) -> IntentState:
    text = state.get("text", "")
    if not text:
        return {"raw": {"intent": "unknown", "confidence": 0.0}}

    prompt = SYSTEM_PROMPT.format(
        recent=state.get("recentContext") or {},
        today=dt.date.today().isoformat(),
        text=text,
    )
    response = get_llm().invoke(prompt)
    parsed = safe_json(response.content)
    log.info(
        "classified",
        extra={"requestId": state.get("requestId"), "node": "classify"},
    )
    return {"raw": parsed}


# Ordered most-specific first: a message mentioning both "lost" and "budget"
# is a status update, not a new lead.
_HEURISTICS: List[tuple[str, tuple[str, ...]]] = [
    ("status_update", ("we lost", "we won", "on hold", "deal is dead", "closed won", "closed lost")),
    ("proposal_request", ("proposal", "quote", "draft a", "sow", "statement of work")),
    ("next_step", ("schedule", "set up a call", "book a", "demo on", "meeting on", "let's set")),
    ("lead_capture", ("wants a", "interested in", "budget", "poc", "looking for")),
]


def _heuristic_intent(text: str) -> Optional[str]:
    lowered = text.lower()
    for intent, needles in _HEURISTICS:
        if any(n in lowered for n in needles):
            return intent
    if lowered.endswith("?"):
        return "knowledge_qa"
    return None


def _validate(state: IntentState) -> IntentState:
    """Coerce the model output into the contract, falling back to heuristics.

    The LLM is the primary classifier; these rules only catch the cases where it
    returned something unparseable or off-schema, so a bad reply degrades into a
    plausible route instead of a 500.
    """
    raw = state.get("raw") or {}
    text = state.get("text", "")

    intent = raw.get("intent")
    fell_back = intent not in VALID_INTENTS
    if fell_back:
        intent = _heuristic_intent(text) or "unknown"

    entities = raw.get("entities")
    if not isinstance(entities, dict):
        entities = {}

    try:
        result = IntentResult(
            intent=intent,
            context=raw.get("context") if raw.get("context") in ("knowledge", "dealflow") else "dealflow",
            entities=Entities(**{k: v for k, v in entities.items() if k in Entities.model_fields}),
            confidence=float(raw.get("confidence", 0.4 if fell_back else 0.7)),
        )
    except Exception:  # noqa: BLE001 - never let classification hard-fail routing
        log.warning(
            "validation failed, defaulting to unknown",
            extra={"requestId": state.get("requestId"), "node": "validate"},
        )
        result = IntentResult(intent="unknown", confidence=0.0)

    payload = result.model_dump()
    payload["fallbackUsed"] = fell_back
    return {"result": payload}


def _build_graph():
    graph = StateGraph(IntentState)
    graph.add_node("Normalize", _normalize)
    graph.add_node("Classify", _classify)
    graph.add_node("Validate", _validate)
    graph.set_entry_point("Normalize")
    graph.add_edge("Normalize", "Classify")
    graph.add_edge("Classify", "Validate")
    graph.add_edge("Validate", END)
    return graph.compile()


INTENT_GRAPH = _build_graph()


def classify_intent(
    text: str, request_id: str, recent_context: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    final = INTENT_GRAPH.invoke(
        {"text": text, "requestId": request_id, "recentContext": recent_context or {}}
    )
    return final["result"]
