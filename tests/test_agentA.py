"""Agent A: ingestion, retrieval, answering and the self-reflection branch."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import FakeCollection, fake_llm_factory, load_agent

A = load_agent("agentA_knowledge")
tools, graph, app_module = A["tools"], A["graph"], A["app"]

client = TestClient(app_module.app)


@pytest.fixture(autouse=True)
def isolate_store():
    """Every test gets a fresh fake collection and deterministic embeddings."""
    collection = FakeCollection()
    tools.set_collection(collection)
    yield collection
    tools.set_collection(None)


@pytest.fixture
def fake_embeddings(monkeypatch):
    monkeypatch.setattr(tools, "embed_documents", lambda chunks: [[0.1] * 8 for _ in chunks])
    monkeypatch.setattr(tools, "embed_query", lambda text: [0.1] * 8)


# --- health ------------------------------------------------------------------

def test_health():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "agent": "agentA"}


def test_request_id_header_is_echoed():
    response = client.get("/health", headers={"X-Request-Id": "trace-me"})
    assert response.headers["X-Request-Id"] == "trace-me"


# --- splitting and document identity ----------------------------------------

def test_split_drops_empty_chunks():
    assert tools.split("   ") == []
    assert len(tools.split("word " * 2000)) > 1


def test_document_key_is_stable_and_prefers_drive_id():
    # Same Drive file under a new name keeps its identity...
    assert tools.document_key("a.pdf", "drive-1") == tools.document_key("b.pdf", "drive-1")
    # ...and different files stay distinct.
    assert tools.document_key("a.pdf", "drive-1") != tools.document_key("a.pdf", "drive-2")
    # Falls back to filename when there is no Drive id.
    assert tools.document_key("a.pdf") == tools.document_key("a.pdf")


# --- ingest ------------------------------------------------------------------

def test_ingest_persists_chunks_with_metadata(isolate_store, fake_embeddings):
    response = client.post(
        "/agentA/ingest",
        json={
            "filename": "Refunds_2025.pdf",
            "driveFileId": "drive-abc",
            "text": "Refunds are issued within 30 days of purchase. " * 40,
            "metadata": {"uploader": "+91999"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chunks"] >= 1
    assert body["tokens"] > 0
    assert body["requestId"]

    upsert = isolate_store.upserts[0]
    assert len(upsert["ids"]) == body["chunks"]
    meta = upsert["metadatas"][0]
    assert meta["filename"] == "Refunds_2025.pdf"
    assert meta["driveFileId"] == "drive-abc"
    assert meta["docKey"] == body["docKey"]


def test_ingest_is_idempotent_purges_old_chunks(isolate_store, fake_embeddings):
    """Re-ingesting the same Drive file must replace, not duplicate.

    The nightly re-index runs over every file, so without the purge the store
    would grow a new copy of every document each night.
    """
    payload = {
        "filename": "Refunds_2025.pdf",
        "driveFileId": "drive-abc",
        "text": "Refunds are issued within 30 days. " * 40,
    }
    first = client.post("/agentA/ingest", json=payload).json()
    second = client.post("/agentA/ingest", json=payload).json()

    assert first["docKey"] == second["docKey"]
    assert isolate_store.deletes == [{"where": {"docKey": first["docKey"]}}] * 2
    # Same ids both times, so the upsert overwrites rather than appends.
    assert isolate_store.upserts[0]["ids"] == isolate_store.upserts[1]["ids"]


def test_ingest_rejects_empty_text():
    response = client.post("/agentA/ingest", json={"filename": "x.pdf", "text": ""})
    assert response.status_code == 422


def test_ingest_requires_text():
    response = client.post("/agentA/ingest", json={"filename": "x.pdf"})
    assert response.status_code == 422


# --- ask ---------------------------------------------------------------------

def _hits(*filenames: str):
    return {
        "documents": [[f"Content of {f}" for f in filenames]],
        "metadatas": [[{"filename": f, "driveFileId": f"id-{f}"} for f in filenames]],
        "distances": [[0.1] * len(filenames)],
    }


def test_ask_with_empty_store_reports_no_knowledge(fake_embeddings):
    response = client.post("/agentA/ask", json={"userId": "u1", "text": "refund policy?"})
    assert response.status_code == 200
    body = response.json()
    assert body["confidence"] == 0.0
    assert body["lowConfidence"] is True
    assert body["citations"] == []


def test_ask_returns_grounded_answer_with_citations(monkeypatch, fake_embeddings):
    tools.set_collection(FakeCollection(_hits("Refunds_2025.pdf")))
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "answer": "Refunds are issued within 30 days.",
                    "citations": [{"title": "Refunds_2025.pdf", "driveFileId": "id-1"}],
                    "confidence": 0.95,  # >= 0.9 so reflection is skipped
                }
            )
        ),
    )
    response = client.post("/agentA/ask", json={"userId": "u1", "text": "refund policy?"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Refunds are issued within 30 days."
    assert body["citations"][0]["title"] == "Refunds_2025.pdf"
    assert body["lowConfidence"] is False
    assert body["revised"] is False


def test_ask_falls_back_to_retrieved_filenames_when_model_omits_citations(
    monkeypatch, fake_embeddings
):
    """An answer must never reach the user uncited when sources were retrieved."""
    tools.set_collection(FakeCollection(_hits("A.pdf", "B.pdf", "A.pdf")))
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(json.dumps({"answer": "Yes.", "confidence": 0.95})),
    )
    body = client.post("/agentA/ask", json={"userId": "u", "text": "q"}).json()
    titles = [c["title"] for c in body["citations"]]
    assert titles == ["A.pdf", "B.pdf"]  # deduplicated, retrieval order preserved


def test_ask_revises_answer_when_reflection_finds_it_unsupported(
    monkeypatch, fake_embeddings
):
    tools.set_collection(FakeCollection(_hits("Refunds_2025.pdf")))
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps({"answer": "Refunds take 90 days.", "confidence": 0.7}),
            json.dumps(
                {"supported": False, "critique": "90 days is not in the source.", "confidence": 0.5}
            ),
            json.dumps(
                {
                    "answer": "Refunds are issued within 30 days.",
                    "citations": [{"title": "Refunds_2025.pdf"}],
                    "confidence": 0.9,
                }
            ),
        ),
    )
    body = client.post("/agentA/ask", json={"userId": "u", "text": "q"}).json()
    assert body["revised"] is True
    assert "30 days" in body["answer"]


def test_ask_keeps_answer_when_reflection_approves(monkeypatch, fake_embeddings):
    tools.set_collection(FakeCollection(_hits("Refunds_2025.pdf")))
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps({"answer": "Within 30 days.", "confidence": 0.7}),
            json.dumps({"supported": True, "critique": "", "confidence": 0.88}),
        ),
    )
    body = client.post("/agentA/ask", json={"userId": "u", "text": "q"}).json()
    assert body["revised"] is False
    assert body["answer"] == "Within 30 days."
    assert body["confidence"] == pytest.approx(0.88)


def test_ask_survives_unparseable_model_output(monkeypatch, fake_embeddings):
    """A non-JSON reply degrades to the raw text, not a 500."""
    tools.set_collection(FakeCollection(_hits("A.pdf")))
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory("Sorry, I can't format that.", "also not json"),
    )
    response = client.post("/agentA/ask", json={"userId": "u", "text": "q"})
    assert response.status_code == 200
    assert response.json()["answer"] == "Sorry, I can't format that."


# --- follow-up parsing -------------------------------------------------------

def test_followup_parse_fills_missing_end_time(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "title": "Call with Dana about refunds",
                    "startISO": "2026-10-06T10:00:00+05:30",
                    "attendees": ["Dana"],
                }
            )
        ),
    )
    body = client.post(
        "/agentA/followup-parse",
        json={"text": "Let's set a call next Tue at 10:00 with Dana about refunds."},
    ).json()
    assert body["title"] == "Call with Dana about refunds"
    assert body["endISO"] == "2026-10-06T11:00:00+05:30"
    assert body["attendees"] == ["Dana"]


def test_followup_parse_rejects_invalid_start(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(json.dumps({"title": "Call", "startISO": "next tuesday"})),
    )
    response = client.post("/agentA/followup-parse", json={"text": "call next tue"})
    assert response.status_code == 500
    assert "ISO 8601" in response.json()["error"]


def test_followup_parse_errors_when_no_event_found(monkeypatch):
    monkeypatch.setattr(graph, "get_llm", fake_llm_factory(json.dumps({})))
    response = client.post("/agentA/followup-parse", json={"text": "hello"})
    assert response.status_code == 500
    assert response.json()["where"] == "followup-parse"
    assert response.json()["requestId"]
