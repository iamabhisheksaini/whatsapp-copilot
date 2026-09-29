"""Agent A — Knowledge, as LangGraph state graphs.

Three compiled graphs, one per endpoint:

  INGEST_GRAPH   Split -> Embed -> Persist
  ASK_GRAPH      Retrieve -> Answer -> (Reflect -> Revise) -> LogIntent
  FOLLOWUP_GRAPH ParseTime -> Validate

Agent A never touches Google or WhatsApp; n8n owns every side effect. The only
external systems here are the LLM provider and the vector store.
"""

import datetime as dt
import os
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

import tools
from shared.llm import get_llm, get_logger, safe_json
from shared.timeparse import correct_weekday

log = get_logger("agentA.graph")

# Below this, the answer is reported as low-confidence and n8n can nudge the
# user to rephrase or upload a document instead of presenting a shaky answer.
CONFIDENCE_FLOOR = float(os.getenv("CONFIDENCE_FLOOR", "0.6"))
REFLECT_ENABLED = os.getenv("SELF_REFLECT", "true").lower() == "true"


# --- ingest ------------------------------------------------------------------

class IngestState(TypedDict, total=False):
    text: str
    filename: str
    driveFileId: Optional[str]
    metadata: Dict[str, Any]
    requestId: str
    docKey: str
    chunks: List[str]
    vectors: List[List[float]]
    result: Dict[str, Any]


def _split_node(state: IngestState) -> IngestState:
    chunks = tools.split(state.get("text", ""))
    if not chunks:
        raise ValueError("document produced no text chunks")
    doc_key = tools.document_key(state["filename"], state.get("driveFileId"))
    log.info(
        f"split into {len(chunks)} chunks",
        extra={"requestId": state.get("requestId"), "node": "Split"},
    )
    return {"chunks": chunks, "docKey": doc_key}


def _embed_node(state: IngestState) -> IngestState:
    return {"vectors": tools.embed_documents(state["chunks"])}


def _persist_node(state: IngestState) -> IngestState:
    doc_key = state["docKey"]
    # Replace-then-write: re-ingesting the same Drive file (nightly re-index, or
    # a user resending it) must not leave stale chunks behind.
    tools.purge_document(doc_key)
    metadata = {
        **(state.get("metadata") or {}),
        "driveFileId": state.get("driveFileId"),
        "ingestedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    tools.persist_chunks(
        doc_key, state["filename"], state["chunks"], state["vectors"], metadata
    )
    result = {
        "chunks": len(state["chunks"]),
        "tokens": tools.estimate_tokens(state["chunks"]),
        "docKey": doc_key,
    }
    log.info(
        "persisted",
        extra={"requestId": state.get("requestId"), "node": "Persist"},
    )
    return {"result": result}


def _build_ingest_graph():
    graph = StateGraph(IngestState)
    graph.add_node("Split", _split_node)
    graph.add_node("Embed", _embed_node)
    graph.add_node("Persist", _persist_node)
    graph.set_entry_point("Split")
    graph.add_edge("Split", "Embed")
    graph.add_edge("Embed", "Persist")
    graph.add_edge("Persist", END)
    return graph.compile()


# --- ask ---------------------------------------------------------------------

class AskState(TypedDict, total=False):
    userId: str
    text: str
    requestId: str
    hits: List[Dict[str, Any]]
    answer: str
    citations: List[Dict[str, Any]]
    confidence: float
    critique: str
    revised: bool
    result: Dict[str, Any]


ANSWER_PROMPT = """You are a knowledge assistant answering from company documents.

Answer the question using ONLY the context below. Cite the exact file names you
used. If the context does not contain the answer, say so plainly and state what
is missing — do not guess.

Context:
{context}

Question: {question}

Return ONLY JSON:
{{
  "answer": string,
  "citations": [{{"title": string, "driveFileId": string|null}}],
  "confidence": number between 0 and 1
}}
"""

REFLECT_PROMPT = """Review this answer against the source context.

Context:
{context}

Question: {question}
Proposed answer: {answer}

Check: is every claim supported by the context? Are the cited files the ones
actually used? Is anything overstated?

Return ONLY JSON:
{{
  "supported": boolean,
  "critique": string,
  "confidence": number between 0 and 1
}}
"""

REVISE_PROMPT = """Your previous answer was reviewed and found unsupported.

Context:
{context}

Question: {question}
Previous answer: {answer}
Critique: {critique}

Write a corrected answer that stays strictly within the context. If the context
genuinely does not answer the question, say that instead.

Return ONLY JSON:
{{
  "answer": string,
  "citations": [{{"title": string, "driveFileId": string|null}}],
  "confidence": number between 0 and 1
}}
"""


def _format_context(hits: List[Dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[Source: {h['metadata'].get('filename', 'unknown')}]\n{h['document']}"
        for h in hits
    )


def _citations_from_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicated citations in retrieval order."""
    seen, citations = set(), []
    for hit in hits:
        meta = hit["metadata"]
        title = meta.get("filename", "unknown")
        if title in seen:
            continue
        seen.add(title)
        citations.append({"title": title, "driveFileId": meta.get("driveFileId")})
    return citations


def _retrieve_node(state: AskState) -> AskState:
    vector = tools.embed_query(state["text"])
    hits = tools.retrieve(vector, k=int(os.getenv("RETRIEVE_K", "4")))
    log.info(
        f"retrieved {len(hits)} chunks",
        extra={"requestId": state.get("requestId"), "node": "Retrieve"},
    )
    return {"hits": hits}


def _answer_node(state: AskState) -> AskState:
    hits = state.get("hits") or []
    if not hits:
        return {
            "answer": "I don't have anything on that in the knowledge base yet. "
            "Send me the document and I'll index it.",
            "citations": [],
            "confidence": 0.0,
        }

    context = _format_context(hits)
    response = get_llm().invoke(
        ANSWER_PROMPT.format(context=context, question=state["text"])
    )
    parsed = safe_json(response.content)

    answer = parsed.get("answer") or parsed.get("_raw") or response.content
    citations = parsed.get("citations")
    if not isinstance(citations, list) or not citations:
        citations = _citations_from_hits(hits)
    try:
        confidence = float(parsed.get("confidence", 0.7))
    except (TypeError, ValueError):
        confidence = 0.7

    return {"answer": answer, "citations": citations, "confidence": confidence}


def _should_reflect(state: AskState) -> str:
    """Only spend a second LLM call when it can change the outcome."""
    if not REFLECT_ENABLED:
        return "skip"
    if not state.get("hits"):
        return "skip"
    if state.get("confidence", 0.0) >= 0.9:
        return "skip"
    return "reflect"


def _reflect_node(state: AskState) -> AskState:
    context = _format_context(state["hits"])
    response = get_llm().invoke(
        REFLECT_PROMPT.format(
            context=context, question=state["text"], answer=state["answer"]
        )
    )
    parsed = safe_json(response.content)
    supported = parsed.get("supported")
    critique = parsed.get("critique", "")

    # Unparseable review: trust the original answer rather than revise blindly.
    if supported is None:
        return {"critique": "", "revised": False}

    try:
        confidence = float(parsed.get("confidence", state.get("confidence", 0.7)))
    except (TypeError, ValueError):
        confidence = state.get("confidence", 0.7)

    return {
        "critique": "" if supported else critique,
        "confidence": confidence,
        "revised": False,
    }


def _needs_revision(state: AskState) -> str:
    return "revise" if state.get("critique") else "done"


def _revise_node(state: AskState) -> AskState:
    context = _format_context(state["hits"])
    response = get_llm().invoke(
        REVISE_PROMPT.format(
            context=context,
            question=state["text"],
            answer=state["answer"],
            critique=state["critique"],
        )
    )
    parsed = safe_json(response.content)
    if "answer" not in parsed:
        return {"revised": False}

    citations = parsed.get("citations")
    if not isinstance(citations, list) or not citations:
        citations = _citations_from_hits(state["hits"])
    try:
        confidence = float(parsed.get("confidence", state.get("confidence", 0.6)))
    except (TypeError, ValueError):
        confidence = state.get("confidence", 0.6)

    log.info(
        "answer revised after self-reflection",
        extra={"requestId": state.get("requestId"), "node": "Revise"},
    )
    return {
        "answer": parsed["answer"],
        "citations": citations,
        "confidence": confidence,
        "revised": True,
    }


def _log_intent_node(state: AskState) -> AskState:
    confidence = state.get("confidence", 0.0)
    result = {
        "answer": state.get("answer", ""),
        "citations": state.get("citations", []),
        "confidence": confidence,
        "lowConfidence": confidence < CONFIDENCE_FLOOR,
        "revised": bool(state.get("revised")),
    }
    log.info(
        f"answered (confidence={confidence:.2f})",
        extra={"requestId": state.get("requestId"), "node": "LogIntent"},
    )
    return {"result": result}


def _build_ask_graph():
    graph = StateGraph(AskState)
    graph.add_node("Retrieve", _retrieve_node)
    graph.add_node("Answer", _answer_node)
    graph.add_node("Reflect", _reflect_node)
    graph.add_node("Revise", _revise_node)
    graph.add_node("LogIntent", _log_intent_node)

    graph.set_entry_point("Retrieve")
    graph.add_edge("Retrieve", "Answer")
    graph.add_conditional_edges(
        "Answer", _should_reflect, {"reflect": "Reflect", "skip": "LogIntent"}
    )
    graph.add_conditional_edges(
        "Reflect", _needs_revision, {"revise": "Revise", "done": "LogIntent"}
    )
    graph.add_edge("Revise", "LogIntent")
    graph.add_edge("LogIntent", END)
    return graph.compile()


# --- follow-up scheduling ----------------------------------------------------

class FollowupState(TypedDict, total=False):
    text: str
    requestId: str
    raw: Dict[str, Any]
    result: Dict[str, Any]


FOLLOWUP_PROMPT = """Extract a calendar event from this message.

Today is {today} ({weekday}). Resolve relative dates ("next Tuesday", "tomorrow")
against that date. Assume a 1 hour duration unless stated. Use the {tz} timezone
and emit ISO 8601 with an offset.

Message: {text}

Return ONLY JSON:
{{
  "title": string,
  "startISO": string,
  "endISO": string,
  "attendees": [string]
}}
"""


def _parse_time_node(state: FollowupState) -> FollowupState:
    today = dt.date.today()
    response = get_llm().invoke(
        FOLLOWUP_PROMPT.format(
            today=today.isoformat(),
            weekday=today.strftime("%A"),
            tz=os.getenv("GENERIC_TIMEZONE", "Asia/Kolkata"),
            text=state["text"],
        )
    )
    return {"raw": safe_json(response.content)}


def _validate_time_node(state: FollowupState) -> FollowupState:
    raw = state.get("raw") or {}
    title, start = raw.get("title"), raw.get("startISO")
    if not title or not start:
        raise ValueError(f"could not extract an event from the message: {raw}")

    try:
        start_dt = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"startISO is not a valid ISO 8601 datetime: {start}") from exc

    # Duration is read before the start moves, so the correction shifts the
    # meeting without resizing it.
    end_dt = None
    raw_end = raw.get("endISO")
    if raw_end:
        try:
            end_dt = dt.datetime.fromisoformat(str(raw_end).replace("Z", "+00:00"))
        except ValueError:
            end_dt = None
    duration = (end_dt - start_dt) if end_dt and end_dt > start_dt else dt.timedelta(hours=1)

    start_dt, corrected = correct_weekday(start_dt, state.get("text", ""))
    if corrected:
        log.info(
            f"weekday corrected to {start_dt.date()}",
            extra={"requestId": state.get("requestId"), "node": "Validate"},
        )

    attendees = raw.get("attendees")
    if not isinstance(attendees, list):
        attendees = []

    return {
        "result": {
            "title": title,
            "startISO": start_dt.isoformat(),
            "endISO": (start_dt + duration).isoformat(),
            "attendees": [a for a in attendees if isinstance(a, str)],
            "weekdayCorrected": corrected,
        }
    }


def _build_followup_graph():
    graph = StateGraph(FollowupState)
    graph.add_node("ParseTime", _parse_time_node)
    graph.add_node("Validate", _validate_time_node)
    graph.set_entry_point("ParseTime")
    graph.add_edge("ParseTime", "Validate")
    graph.add_edge("Validate", END)
    return graph.compile()


INGEST_GRAPH = _build_ingest_graph()
ASK_GRAPH = _build_ask_graph()
FOLLOWUP_GRAPH = _build_followup_graph()


# --- entry points used by app.py --------------------------------------------

def run_ingest(
    text: str,
    filename: str,
    metadata: Optional[Dict[str, Any]],
    requestId: str,
    driveFileId: Optional[str] = None,
) -> Dict[str, Any]:
    final = INGEST_GRAPH.invoke(
        {
            "text": text,
            "filename": filename,
            "driveFileId": driveFileId,
            "metadata": metadata or {},
            "requestId": requestId,
        }
    )
    return final["result"]


def run_ask(user_id: str, text: str, requestId: str) -> Dict[str, Any]:
    final = ASK_GRAPH.invoke(
        {"userId": user_id, "text": text, "requestId": requestId}
    )
    return final["result"]


def run_followup_parse(text: str, requestId: str) -> Dict[str, Any]:
    final = FOLLOWUP_GRAPH.invoke({"text": text, "requestId": requestId})
    return final["result"]
