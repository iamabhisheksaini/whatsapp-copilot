"""Agent B — Dealflow, as LangGraph state graphs.

  LEAD_GRAPH     Parse -> ValidateEnrich -> Score -> LogIntent
  PROPOSAL_GRAPH ProposalCopy -> ValidateCopy
  NEXTSTEP_GRAPH ScheduleIntent -> ValidateTime
  STATUS_GRAPH   StatusClassify -> ValidateStatus

Agent B returns typed JSON and nothing else — n8n writes to Sheets, Drive and
Calendar.
"""

import datetime as dt
import os
from typing import Any, Literal, TypedDict

import tools
from langgraph.graph import END, StateGraph

from shared import metrics
from shared.llm import get_llm, get_logger, safe_json
from shared.timeparse import correct_weekday

log = get_logger("agentB.graph")

TIMEZONE = os.getenv("GENERIC_TIMEZONE", "Asia/Kolkata")

StatusLabel = Literal["Won", "Lost", "On hold"]

# Fixed vocabulary so the CRM stays reportable — free-text reasons are useless
# for funnel analysis.
REASON_CATEGORIES = [
    "budget",
    "timing",
    "competitor",
    "no_decision",
    "bad_fit",
    "scope",
    "champion_left",
    "other",
]


# --- lead capture ------------------------------------------------------------

class LeadState(TypedDict, total=False):
    raw: str
    requestId: str
    parsed: dict[str, Any]
    result: dict[str, Any]


LEAD_PROMPT = """Extract structured lead information from this sales note.

Note: {raw}

Return ONLY JSON. Use null for anything not stated — never invent details.
{{
  "name": string|null,
  "company": string|null,
  "intent": string|null,
  "budget": string|null,
  "timeline": string|null,
  "notes": string|null
}}
"""


def _parse_lead(state: LeadState) -> LeadState:
    raw = tools.clean_text(state.get("raw")) or ""
    if not raw:
        raise ValueError("empty lead text")
    response = get_llm().invoke(LEAD_PROMPT.format(raw=raw))
    parsed = safe_json(response.content)
    if "_raw" in parsed:
        raise ValueError(f"could not parse lead from model output: {parsed['_raw'][:200]}")
    log.info("lead parsed", extra={"requestId": state.get("requestId"), "node": "Parse"})
    return {"parsed": parsed}


def _enrich_lead(state: LeadState) -> LeadState:
    parsed = dict(state.get("parsed") or {})
    for key in ("name", "company", "intent", "budget", "timeline", "notes"):
        parsed[key] = tools.clean_text(parsed.get(key))

    amount, currency, original = tools.normalize_budget(parsed.get("budget"))
    parsed["normalizedCompanyDomain"] = tools.guess_company_domain(parsed.get("company"))
    parsed["budgetAmount"] = amount
    parsed["budgetCurrency"] = currency
    parsed["budget"] = original
    return {"parsed": parsed}


def _score_lead(state: LeadState) -> LeadState:
    parsed = dict(state["parsed"])
    quality, missing = tools.score_lead(parsed)
    result = {
        "name": parsed.get("name"),
        "company": parsed.get("company"),
        "intent": parsed.get("intent"),
        "budget": parsed.get("budget"),
        "budgetAmount": parsed.get("budgetAmount"),
        "budgetCurrency": parsed.get("budgetCurrency"),
        "timeline": parsed.get("timeline"),
        "normalizedCompanyDomain": parsed.get("normalizedCompanyDomain"),
        "qualityScore": quality,
        "missingFields": missing,
        "notes": parsed.get("notes"),
    }
    log.info(
        f"lead scored {quality}",
        extra={"requestId": state.get("requestId"), "node": "Score"},
    )
    return {"result": result}


def _log_intent(state: LeadState) -> LeadState:
    """Terminal node mirroring Agent A, so both graphs end on a logged outcome."""
    result = state["result"]
    metrics.incr("funnel_captured")
    if not result.get("missingFields"):
        metrics.incr("funnel_complete")
    log.info(
        f"lead captured for {result.get('company') or 'unknown company'}",
        extra={"requestId": state.get("requestId"), "node": "LogIntent"},
    )
    return {}


def _build_lead_graph():
    graph = StateGraph(LeadState)
    graph.add_node("Parse", _parse_lead)
    graph.add_node("ValidateEnrich", _enrich_lead)
    graph.add_node("Score", _score_lead)
    graph.add_node("LogIntent", _log_intent)
    graph.set_entry_point("Parse")
    graph.add_edge("Parse", "ValidateEnrich")
    graph.add_edge("ValidateEnrich", "Score")
    graph.add_edge("Score", "LogIntent")
    graph.add_edge("LogIntent", END)
    return graph.compile()


# --- proposal copy -----------------------------------------------------------

class ProposalState(TypedDict, total=False):
    lead: dict[str, Any]
    requestId: str
    raw: dict[str, Any]
    result: dict[str, Any]


PROPOSAL_PROMPT = """Write proposal copy for this lead.

Lead: {lead}

Business tone. The summary is 120-160 words. Give 3-5 bullets covering scope,
timeline and next step. Only reference what the lead states — make no
commitments about pricing, headcount or delivery dates you cannot support.

Return ONLY JSON:
{{
  "title": string,
  "summaryBlurb": string,
  "bulletPoints": [string]
}}
"""


def _proposal_copy(state: ProposalState) -> ProposalState:
    lead = state.get("lead") or {}
    if not lead:
        raise ValueError("lead is required to generate proposal copy")
    response = get_llm(temperature=0.4).invoke(PROPOSAL_PROMPT.format(lead=lead))
    return {"raw": safe_json(response.content)}


def _validate_copy(state: ProposalState) -> ProposalState:
    raw = state.get("raw") or {}
    title = raw.get("title")
    summary = raw.get("summaryBlurb")
    bullets = raw.get("bulletPoints")

    if not title or not summary:
        raise ValueError(f"proposal copy missing title or summary: {raw}")
    if not isinstance(bullets, list) or not bullets:
        raise ValueError("proposal copy produced no bullet points")

    company = (state.get("lead") or {}).get("company")
    bullets = [tools.clean_text(b, limit=300) for b in bullets if b]
    bullets = [b for b in bullets if b][:5]

    result = {
        "title": tools.clean_text(title, limit=200),
        "summaryBlurb": tools.clean_text(summary, limit=2000),
        "bulletPoints": bullets,
        "wordCount": len(str(summary).split()),
        "company": company,
    }
    metrics.incr("funnel_proposal_sent")
    log.info(
        "proposal copy generated",
        extra={"requestId": state.get("requestId"), "node": "ValidateCopy"},
    )
    return {"result": result}


def _build_proposal_graph():
    graph = StateGraph(ProposalState)
    graph.add_node("ProposalCopy", _proposal_copy)
    graph.add_node("ValidateCopy", _validate_copy)
    graph.set_entry_point("ProposalCopy")
    graph.add_edge("ProposalCopy", "ValidateCopy")
    graph.add_edge("ValidateCopy", END)
    return graph.compile()


# --- next step scheduling ----------------------------------------------------

class NextStepState(TypedDict, total=False):
    text: str
    requestId: str
    raw: dict[str, Any]
    result: dict[str, Any]


NEXTSTEP_PROMPT = """Extract a calendar event from this sales message.

Today is {today} ({weekday}). Resolve relative dates ("next Wed", "tomorrow")
against that date. Assume 1 hour unless stated. Use the {tz} timezone and emit
ISO 8601 with an offset.

Message: {text}

Return ONLY JSON:
{{
  "title": string,
  "startISO": string,
  "endISO": string
}}
"""


def _parse_next_step(state: NextStepState) -> NextStepState:
    today = dt.date.today()
    response = get_llm().invoke(
        NEXTSTEP_PROMPT.format(
            today=today.isoformat(),
            weekday=today.strftime("%A"),
            tz=TIMEZONE,
            text=state["text"],
        )
    )
    return {"raw": safe_json(response.content)}


def _validate_next_step(state: NextStepState) -> NextStepState:
    raw = state.get("raw") or {}
    title, start = raw.get("title"), raw.get("startISO")
    if not title or not start:
        raise ValueError(f"could not extract an event: {raw}")

    try:
        start_dt = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"startISO is not valid ISO 8601: {start}") from exc

    # Read the duration off the model's own pair of timestamps, before the start
    # is moved — otherwise the correction would stretch or invert the meeting.
    end_dt = None
    raw_end = raw.get("endISO")
    if raw_end:
        try:
            end_dt = dt.datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        except ValueError:
            end_dt = None
    duration = (end_dt - start_dt) if end_dt and end_dt > start_dt else dt.timedelta(hours=1)

    # The model is unreliable at calendar arithmetic; if the user named a
    # weekday, that name wins.
    start_dt, corrected = correct_weekday(start_dt, state.get("text", ""))
    if corrected:
        log.info(
            f"weekday corrected to {start_dt.date()}",
            extra={"requestId": state.get("requestId"), "node": "ValidateTime"},
        )

    return {
        "result": {
            "title": title,
            "startISO": start_dt.isoformat(),
            "endISO": (start_dt + duration).isoformat(),
            "weekdayCorrected": corrected,
        }
    }


def _build_nextstep_graph():
    graph = StateGraph(NextStepState)
    graph.add_node("ScheduleIntent", _parse_next_step)
    graph.add_node("ValidateTime", _validate_next_step)
    graph.set_entry_point("ScheduleIntent")
    graph.add_edge("ScheduleIntent", "ValidateTime")
    graph.add_edge("ValidateTime", END)
    return graph.compile()


# --- status classification ---------------------------------------------------

class StatusState(TypedDict, total=False):
    label: str
    reasonText: str | None
    requestId: str
    raw: dict[str, Any]
    result: dict[str, Any]


STATUS_PROMPT = """Categorise why a deal reached this status.

Status: {label}
Reason given: {reason}

Pick exactly one category from: {categories}
Summarise the reason in one sentence, under 20 words.

Return ONLY JSON:
{{
  "reasonCategory": string,
  "reasonSummary": string
}}
"""


def _classify_reason(state: StatusState) -> StatusState:
    reason = tools.clean_text(state.get("reasonText")) or ""
    if not reason:
        # No reason text to categorise — skip the LLM call entirely.
        return {"raw": {"reasonCategory": "other", "reasonSummary": ""}}
    response = get_llm().invoke(
        STATUS_PROMPT.format(
            label=state.get("label"),
            reason=reason,
            categories=", ".join(REASON_CATEGORIES),
        )
    )
    return {"raw": safe_json(response.content)}


def _validate_status(state: StatusState) -> StatusState:
    label = state.get("label")
    if label not in ("Won", "Lost", "On hold"):
        raise ValueError(f"label must be Won, Lost or On hold — got {label!r}")

    raw = state.get("raw") or {}
    category = raw.get("reasonCategory")
    if category not in REASON_CATEGORIES:
        category = "other"

    summary = tools.clean_text(raw.get("reasonSummary"), limit=300) or (
        tools.clean_text(state.get("reasonText"), limit=300) or ""
    )

    metrics.incr(f"funnel_{label.lower().replace(' ', '_')}")
    log.info(
        f"status {label}/{category}",
        extra={"requestId": state.get("requestId"), "node": "ValidateStatus"},
    )
    return {
        "result": {
            "label": label,
            "reasonCategory": category,
            "reasonSummary": summary,
        }
    }


def _build_status_graph():
    graph = StateGraph(StatusState)
    graph.add_node("StatusClassify", _classify_reason)
    graph.add_node("ValidateStatus", _validate_status)
    graph.set_entry_point("StatusClassify")
    graph.add_edge("StatusClassify", "ValidateStatus")
    graph.add_edge("ValidateStatus", END)
    return graph.compile()


LEAD_GRAPH = _build_lead_graph()
PROPOSAL_GRAPH = _build_proposal_graph()
NEXTSTEP_GRAPH = _build_nextstep_graph()
STATUS_GRAPH = _build_status_graph()


# --- entry points used by app.py --------------------------------------------

def process_new_lead(raw: str, requestId: str = "") -> dict[str, Any]:
    return LEAD_GRAPH.invoke({"raw": raw, "requestId": requestId})["result"]


def generate_proposal_copy(lead: dict[str, Any], requestId: str = "") -> dict[str, Any]:
    return PROPOSAL_GRAPH.invoke({"lead": lead, "requestId": requestId})["result"]


def parse_next_step(text: str, requestId: str = "") -> dict[str, Any]:
    return NEXTSTEP_GRAPH.invoke({"text": text, "requestId": requestId})["result"]


def classify_status(
    label: str, reasonText: str | None = None, requestId: str = ""
) -> dict[str, Any]:
    return STATUS_GRAPH.invoke(
        {"label": label, "reasonText": reasonText, "requestId": requestId}
    )["result"]
