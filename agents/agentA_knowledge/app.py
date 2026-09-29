"""Agent A — Knowledge. FastAPI surface over the LangGraph graphs.

Contract (from the assignment):
  POST /agentA/ingest         {driveFileId, filename, text} -> {chunks, tokens}
  POST /agentA/ask            {userId, text}                -> {answer, citations[], confidence}
  POST /agentA/followup-parse {text}                        -> {title, startISO, endISO?, attendees?}

Every response carries a requestId for tracing across n8n and the agent logs.
"""

import time
import uuid
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from graph import run_ask, run_followup_parse, run_ingest
from shared.llm import get_logger

log = get_logger("agentA.app")

app = FastAPI(title="Agent A — Knowledge", version="1.0.0")


# --- models ------------------------------------------------------------------

class IngestIn(BaseModel):
    filename: str
    text: str = Field(min_length=1, description="Plain text extracted by n8n")
    driveFileId: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


class IngestOut(BaseModel):
    chunks: int
    tokens: int
    docKey: str
    requestId: str


class AskIn(BaseModel):
    userId: str
    text: str = Field(min_length=1)


class Citation(BaseModel):
    title: str
    driveFileId: Optional[str] = None
    pageRanges: Optional[str] = None


class AskOut(BaseModel):
    answer: str
    citations: List[Citation]
    confidence: float
    lowConfidence: bool
    revised: bool
    requestId: str


class FollowupIn(BaseModel):
    text: str = Field(min_length=1)


class FollowupOut(BaseModel):
    title: str
    startISO: str
    endISO: Optional[str] = None
    attendees: List[str] = []
    weekdayCorrected: bool = False
    requestId: str


# --- middleware --------------------------------------------------------------

@app.middleware("http")
async def attach_request_id(request: Request, call_next):
    """One id per request, echoed in the response header and body.

    n8n forwards its own X-Request-Id so a WhatsApp message can be traced from
    the webhook through both agents.
    """
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    log.info(
        f"{request.method} {request.url.path} -> {response.status_code}",
        extra={
            "requestId": request_id,
            "agent": "agentA",
            "durationMs": round((time.perf_counter() - started) * 1000, 1),
        },
    )
    return response


def _fail(request_id: str, where: str, exc: Exception) -> JSONResponse:
    """Uniform error envelope so n8n's error branch has something to route on."""
    log.error(f"{where} failed: {exc}", extra={"requestId": request_id, "node": where})
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "where": where, "requestId": request_id},
    )


# --- routes ------------------------------------------------------------------

@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "agentA"}


@app.post("/agentA/ingest", response_model=IngestOut)
def ingest(payload: IngestIn, request: Request):
    request_id = request.state.request_id
    try:
        result = run_ingest(
            text=payload.text,
            filename=payload.filename,
            metadata=payload.metadata,
            requestId=request_id,
            driveFileId=payload.driveFileId,
        )
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "ingest", exc)


@app.post("/agentA/ask", response_model=AskOut)
def ask(payload: AskIn, request: Request):
    request_id = request.state.request_id
    try:
        result = run_ask(user_id=payload.userId, text=payload.text, requestId=request_id)
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "ask", exc)


@app.post("/agentA/followup-parse", response_model=FollowupOut)
def followup_parse(payload: FollowupIn, request: Request):
    request_id = request.state.request_id
    try:
        result = run_followup_parse(text=payload.text, requestId=request_id)
        return {**result, "requestId": request_id}
    except Exception as exc:  # noqa: BLE001
        return _fail(request_id, "followup-parse", exc)
