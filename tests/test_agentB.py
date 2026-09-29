"""Agent B: lead parsing, enrichment, proposal copy, scheduling and status."""

import json

import pytest
from fastapi.testclient import TestClient

from conftest import fake_llm_factory, load_agent

B = load_agent("agentB_dealflow")
tools, graph, app_module = B["tools"], B["graph"], B["app"]

client = TestClient(app_module.app)


# --- health ------------------------------------------------------------------

def test_health():
    assert client.get("/health").json() == {"status": "ok", "agent": "agentB"}


# --- pure enrichment helpers -------------------------------------------------

@pytest.mark.parametrize(
    "company,expected",
    [
        ("Acme", "acme.com"),
        ("Acme Inc.", "acme.com"),
        ("Acme Technologies Pvt Ltd", "acme.com"),
        ("Blue Ridge Systems", "blueridge.com"),
        ("acme.io", "acme.io"),               # already a domain
        ("john@acme.co.uk", "acme.co.uk"),    # pulled from an email
        ("", None),
        (None, None),
        ("Inc.", None),                       # nothing left after stripping
    ],
)
def test_guess_company_domain(company, expected):
    assert tools.guess_company_domain(company) == expected


@pytest.mark.parametrize(
    "raw,amount,currency",
    [
        ("10k", 10_000, None),
        ("~10k", 10_000, None),
        ("$50,000", 50_000, "USD"),
        ("around 2.5M", 2_500_000, None),
        ("₹5 lakh", 500_000, "INR"),
        ("USD 250k", 250_000, "USD"),
        (10000, 10_000, None),
        ("no budget discussed", None, None),
        (None, None, None),
    ],
)
def test_normalize_budget(raw, amount, currency):
    got_amount, got_currency, _ = tools.normalize_budget(raw)
    assert got_amount == amount
    assert got_currency == currency


def test_score_lead_reports_missing_fields():
    score, missing = tools.score_lead(
        {"name": "John", "company": "Acme", "intent": "PoC", "budget": None, "timeline": None}
    )
    assert score == pytest.approx(0.65)
    assert set(missing) == {"budget", "timeline"}


def test_score_lead_full_marks():
    score, missing = tools.score_lead(
        {
            "name": "John",
            "company": "Acme",
            "intent": "PoC",
            "budget": "10k",
            "timeline": "September",
        }
    )
    assert score == 1.0
    assert missing == []


def test_clean_text_strips_html_and_clamps():
    assert tools.clean_text("<b>hi</b> there") == "hi there"
    assert len(tools.clean_text("x" * 5000, limit=100)) == 100
    assert tools.clean_text("   ") is None


# --- lead capture ------------------------------------------------------------

def test_newlead_parses_enriches_and_scores(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "name": "John",
                    "company": "Acme Inc.",
                    "intent": "PoC",
                    "budget": "around 10k",
                    "timeline": "September",
                    "notes": None,
                }
            )
        ),
    )
    body = client.post(
        "/agentB/newlead",
        json={"raw": "John from Acme wants a PoC in September, budget around 10k."},
    ).json()

    assert body["name"] == "John"
    assert body["company"] == "Acme Inc."
    assert body["normalizedCompanyDomain"] == "acme.com"
    assert body["budgetAmount"] == 10_000
    assert body["qualityScore"] == 1.0
    assert body["missingFields"] == []
    assert body["requestId"]


def test_newlead_flags_missing_fields_for_followup(monkeypatch):
    """A thin lead should tell n8n exactly what to ask for next."""
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(json.dumps({"name": "Dana", "company": "Globex"})),
    )
    body = client.post("/agentB/newlead", json={"raw": "Dana at Globex reached out"}).json()
    assert set(body["missingFields"]) == {"intent", "budget", "timeline"}
    assert body["qualityScore"] < 0.6


def test_newlead_errors_on_unparseable_model_output(monkeypatch):
    monkeypatch.setattr(graph, "get_llm", fake_llm_factory("I couldn't parse that"))
    response = client.post("/agentB/newlead", json={"raw": "something"})
    assert response.status_code == 500
    assert response.json()["where"] == "newlead"


def test_newlead_rejects_empty_input():
    assert client.post("/agentB/newlead", json={"raw": ""}).status_code == 422
    assert client.post("/agentB/newlead", json={}).status_code == 422


# --- proposal copy -----------------------------------------------------------

def test_proposal_copy_returns_contract_shape(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "title": "PoC Proposal for Acme",
                    "summaryBlurb": "A four week proof of concept. " * 10,
                    "bulletPoints": ["Scope", "Timeline", "Next step"],
                }
            )
        ),
    )
    body = client.post(
        "/agentB/proposal-copy",
        json={"lead": {"name": "John", "company": "Acme", "budget": "10k"}},
    ).json()

    assert body["title"] == "PoC Proposal for Acme"
    assert len(body["bulletPoints"]) == 3
    assert body["company"] == "Acme"
    assert body["wordCount"] > 0


def test_proposal_copy_caps_bullets_at_five(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "title": "T",
                    "summaryBlurb": "S",
                    "bulletPoints": [f"point {i}" for i in range(9)],
                }
            )
        ),
    )
    body = client.post("/agentB/proposal-copy", json={"lead": {"company": "Acme"}}).json()
    assert len(body["bulletPoints"]) == 5


def test_proposal_copy_errors_without_bullets(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(json.dumps({"title": "T", "summaryBlurb": "S", "bulletPoints": []})),
    )
    response = client.post("/agentB/proposal-copy", json={"lead": {"company": "Acme"}})
    assert response.status_code == 500
    assert "bullet" in response.json()["error"]


def test_proposal_copy_errors_on_empty_lead(monkeypatch):
    response = client.post("/agentB/proposal-copy", json={"lead": {}})
    assert response.status_code == 500
    assert "lead is required" in response.json()["error"]


# --- next step ---------------------------------------------------------------

def test_nextstep_parse_defaults_end_to_one_hour(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps({"title": "Demo", "startISO": "2026-10-07T11:00:00+05:30"})
        ),
    )
    body = client.post(
        "/agentB/nextstep-parse", json={"text": "Schedule a demo next Wed at 11."}
    ).json()
    assert body["title"] == "Demo"
    assert body["endISO"] == "2026-10-07T12:00:00+05:30"


def test_nextstep_parse_replaces_invalid_end(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {"title": "Demo", "startISO": "2026-10-07T11:00:00+05:30", "endISO": "later"}
            )
        ),
    )
    body = client.post("/agentB/nextstep-parse", json={"text": "demo wed 11"}).json()
    assert body["endISO"] == "2026-10-07T12:00:00+05:30"


def test_nextstep_parse_errors_when_no_time_found(monkeypatch):
    monkeypatch.setattr(graph, "get_llm", fake_llm_factory(json.dumps({"title": "Demo"})))
    response = client.post("/agentB/nextstep-parse", json={"text": "let's meet sometime"})
    assert response.status_code == 500


# --- status classification ---------------------------------------------------

def test_status_classify_categorises_reason(monkeypatch):
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps({"reasonCategory": "budget", "reasonSummary": "Budget was cut."})
        ),
    )
    body = client.post(
        "/agentB/status-classify", json={"label": "Lost", "reasonText": "budget cut"}
    ).json()
    assert body == {
        "label": "Lost",
        "reasonCategory": "budget",
        "reasonSummary": "Budget was cut.",
        "requestId": body["requestId"],
    }


def test_status_classify_coerces_unknown_category(monkeypatch):
    """The CRM only accepts the fixed vocabulary, so anything else becomes 'other'."""
    monkeypatch.setattr(
        graph,
        "get_llm",
        fake_llm_factory(
            json.dumps({"reasonCategory": "vibes", "reasonSummary": "Went cold."})
        ),
    )
    body = client.post(
        "/agentB/status-classify", json={"label": "Lost", "reasonText": "went cold"}
    ).json()
    assert body["reasonCategory"] == "other"


def test_status_classify_skips_llm_without_reason(monkeypatch):
    factory = fake_llm_factory()  # no queued replies: calling it would raise
    monkeypatch.setattr(graph, "get_llm", factory)
    body = client.post("/agentB/status-classify", json={"label": "Won"}).json()
    assert body["reasonCategory"] == "other"
    assert factory.llm.prompts == []


def test_status_classify_rejects_unknown_label():
    response = client.post("/agentB/status-classify", json={"label": "Maybe"})
    assert response.status_code == 422


# --- shared intent classifier ------------------------------------------------

def test_classify_returns_intent_and_entities(monkeypatch):
    from shared import intent as intent_module

    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(
            json.dumps(
                {
                    "intent": "lead_capture",
                    "context": "dealflow",
                    "entities": {"person": "John", "company": "Acme", "budget": "10k"},
                    "confidence": 0.9,
                }
            )
        ),
    )
    body = client.post(
        "/classify", json={"text": "John from Acme wants a PoC, budget 10k"}
    ).json()
    assert body["intent"] == "lead_capture"
    assert body["entities"]["company"] == "Acme"
    assert body["fallbackUsed"] is False


def test_classify_falls_back_to_heuristics_on_bad_output(monkeypatch):
    """An unparseable classification must still route somewhere sensible."""
    from shared import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", fake_llm_factory("not json at all"))
    body = client.post("/classify", json={"text": "We lost the Acme deal"}).json()
    assert body["intent"] == "status_update"
    assert body["fallbackUsed"] is True


def test_classify_heuristic_treats_question_as_knowledge_qa(monkeypatch):
    from shared import intent as intent_module

    monkeypatch.setattr(intent_module, "get_llm", fake_llm_factory("garbage"))
    body = client.post("/classify", json={"text": "What is our refund policy?"}).json()
    assert body["intent"] == "knowledge_qa"


def test_classify_rejects_invalid_intent_value(monkeypatch):
    from shared import intent as intent_module

    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(json.dumps({"intent": "make_coffee", "confidence": 0.9})),
    )
    body = client.post("/classify", json={"text": "hello there"}).json()
    assert body["intent"] in ("unknown", "smalltalk")
    assert body["fallbackUsed"] is True
