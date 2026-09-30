"""Agent B — Dealflow. FastAPI surface over the LangGraph graphs.

Contract (from the assignment):
  POST /agentB/newlead         {raw}                 -> {name, company, intent, budget, ...}
  POST /agentB/proposal-copy   {lead}                -> {title, summaryBlurb, bulletPoints[]}
  POST /agentB/nextstep-parse  {text}                -> {title, startISO, endISO?}
  POST /agentB/status-classify {label, reasonText?}  -> {label, reasonCategory, reasonSummary}

Agent B also hosts the shared intent classifier at POST /classify, which n8n
calls first to route every text-only inbound message.
"""

import time
import uuid
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from graph import (
    classify_status,
    generate_proposal_copy,
    parse_next_step,
    process_new_lead,
)
from pydantic import BaseModel, Field

from shared import metrics
from shared.intent import classify_intent
from shared.llm import get_logger

log = get_logger("agentB.app")

app = FastAPI(title="Agent B — Dealflow", version="1.0.0")


# --- models ------------------------------------------------------------------

class ClassifyIn(BaseModel):
    text: str = Field(min_length=1)
    recentContext: dict[str, Any] | None = None


class ClassifyOut(BaseModel):
    intent: str
    context: str
    entities: dict[str, Any]
    confidence: float
    fallbackUsed: bool
    requestId: str


class LeadIn(BaseModel):
    raw: str = Field(min_length=1)


class LeadOut(BaseModel):
    name: str | None = None
    company: str | None = None
    intent: str | None = None
    budget: str | None = None
    budgetAmount: float | None = None
    budgetCurrency: str | None = None
    timeline: str | None = None
    normalizedCompanyDomain: str | None = None
    qualityScore: float
    missingFields: list[str]
    notes: str | None = None
    requestId: str


class ProposalIn(BaseModel):
    lead: dict[str, Any]


class ProposalOut(BaseModel):
    title: str
    summaryBlurb: str
    bulletPoints: list[str]
    wordCount: int
    company: str | None = None
    requestId: str


class NextStepIn(BaseModel):
    text: str = Field(min_length=1)


class NextStepOut(BaseModel):
    title: str
    startISO: str
    endISO: str | None = None
    weekdayCorrected: bool = False
    requestId: str


class StatusIn(BaseModel):
    label: Literal["Won", "Lost", "On hold"]
    reasonText: str | None = None


class StatusOut(BaseModel):
    label: str
    reasonCategory: str
    reasonSummary: str
    requestId: str


# --- middleware --------------------------------------------------------------

@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    log.info(
        f"{request.method} {request.url.path} -> {response.status_code}",
        extra={
            "requestId": request_id,
            "agent": "agentB",
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return response


def _fail(request_id: str, where: str, exc: Exception) -> JSONResponse:
    log.error(f"{where} failed: {exc}", extra={"requestId": request_id, "node": where})
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "where": where, "requestId": request_id},
    )


# --- routes ------------------------------------------------------------------

@app.get("/metrics")
def get_metrics() -> dict[str, Any]:
    """Counters for the metrics named in the brief: ingested files, Q&A
    latency, retrieval hit rate and lead funnel counts."""
    return {"agent": "agentB", **metrics.snapshot()}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "agentB"}


@app.post("/classify", response_model=ClassifyOut)
def classify(payload: ClassifyIn, request: Request):
    request_id = request.state.request_id
    try:
        result = classify_intent(
            text=payload.text,
            request_id=request_id,
            recent_context=payload.recentContext,
        )
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "classify", exc)


@app.post("/agentB/newlead", response_model=LeadOut)
def new_lead(payload: LeadIn, request: Request):
    request_id = request.state.request_id
    try:
        result = process_new_lead(payload.raw, requestId=request_id)
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "newlead", exc)


@app.post("/agentB/proposal-copy", response_model=ProposalOut)
def proposal_copy(payload: ProposalIn, request: Request):
    request_id = request.state.request_id
    try:
        result = generate_proposal_copy(payload.lead, requestId=request_id)
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "proposal-copy", exc)


@app.post("/agentB/nextstep-parse", response_model=NextStepOut)
def nextstep_parse(payload: NextStepIn, request: Request):
    request_id = request.state.request_id
    try:
        result = parse_next_step(payload.text, requestId=request_id)
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "nextstep-parse", exc)


@app.post("/agentB/status-classify", response_model=StatusOut)
def status_classify(payload: StatusIn, request: Request):
    request_id = request.state.request_id
    try:
        result = classify_status(
            label=payload.label, reasonText=payload.reasonText, requestId=request_id
        )
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "status-classify", exc)
