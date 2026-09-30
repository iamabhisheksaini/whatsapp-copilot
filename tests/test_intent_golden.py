"""Golden tests for intent classification and entity extraction (§7).

Two layers, deliberately separated:

* The **heuristic** layer is asserted exactly. It runs with no LLM at all, so
  these cases are deterministic and will fail loudly if the keyword ordering
  regresses — which matters, because the heuristic is what catches a model that
  returns something off-schema.
* The **schema** layer asserts the shape a caller can rely on, for every
  intent, without pinning the model's wording.

Nothing here reaches the network: the LLM is replaced by a fake that returns
queued JSON.
"""

import json

import pytest
from conftest import fake_llm_factory

from shared import intent as intent_module
from shared.intent import VALID_INTENTS, Entities, classify_intent

# --- heuristic fallback: exact, deterministic, no LLM ------------------------

GOLDEN_HEURISTIC = [
    # (message, expected intent)
    ("We lost the Acme deal",                       "status_update"),
    ("we won the Globex contract",                  "status_update"),
    ("Put it on hold for now",                      "status_update"),
    ("Draft a proposal for Acme",                   "proposal_request"),
    ("can you send a quote",                        "proposal_request"),
    ("Schedule a demo next Wed at 11",              "next_step"),
    ("let's set up a call tomorrow",                "next_step"),
    ("John from Acme wants a PoC, budget 10k",      "lead_capture"),
    ("They're interested in the enterprise tier",   "lead_capture"),
    ("What is our refund policy?",                  "knowledge_qa"),
    ("Do digital goods qualify?",                   "knowledge_qa"),
    ("asdfghjkl",                                   "unknown"),
]


@pytest.mark.parametrize("message,expected", GOLDEN_HEURISTIC)
def test_heuristic_fallback_routes_correctly(monkeypatch, message, expected):
    """With the model returning junk, the keyword layer must still route."""
    monkeypatch.setattr(intent_module, "get_llm", fake_llm_factory("not json at all"))
    result = classify_intent(message, request_id="golden")
    assert result["intent"] == expected, f"{message!r} routed to {result['intent']}"
    assert result["fallbackUsed"] is True


def test_status_beats_lead_when_both_signals_present():
    """Ordering matters: 'we lost ... budget cut' is a status, not a new lead."""
    from shared.intent import _heuristic_intent

    assert _heuristic_intent("We lost Acme - budget cut") == "status_update"
    assert _heuristic_intent("They have budget for a PoC") == "lead_capture"


def test_proposal_beats_scheduling_when_both_present():
    from shared.intent import _heuristic_intent

    assert _heuristic_intent("draft a proposal and schedule a call") == "proposal_request"


# --- full graph: schema contract across every intent -------------------------

GOLDEN_SCHEMA = [
    ("knowledge_qa",     "knowledge", {"topic": "refunds"}),
    ("lead_capture",     "dealflow",  {"person": "John", "company": "Acme", "budget": "10k"}),
    ("proposal_request", "dealflow",  {"company": "Acme"}),
    ("next_step",        "dealflow",  {"datetimeText": "next Wednesday at 11"}),
    ("status_update",    "dealflow",  {"company": "Acme", "statusLabel": "Lost"}),
    ("smalltalk",        "dealflow",  {}),
    ("unknown",          "dealflow",  {}),
]


@pytest.mark.parametrize("intent,context,entities", GOLDEN_SCHEMA)
def test_every_intent_returns_the_documented_shape(monkeypatch, intent, context, entities):
    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(json.dumps({
            "intent": intent, "context": context,
            "entities": entities, "confidence": 0.9,
        })),
    )
    result = classify_intent("some message", request_id="golden")

    assert result["intent"] == intent
    assert result["intent"] in VALID_INTENTS
    assert result["context"] in ("knowledge", "dealflow")
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["fallbackUsed"] is False
    # Every declared entity key is present, defaulting to None.
    assert set(result["entities"]) == set(Entities.model_fields)
    for key, value in entities.items():
        assert result["entities"][key] == value


def test_unexpected_entity_keys_are_dropped(monkeypatch):
    """A model that invents fields must not widen the contract."""
    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(json.dumps({
            "intent": "lead_capture",
            "entities": {"company": "Acme", "favouriteColour": "blue"},
            "confidence": 0.8,
        })),
    )
    result = classify_intent("John at Acme", request_id="golden")
    assert "favouriteColour" not in result["entities"]
    assert result["entities"]["company"] == "Acme"


def test_next_step_context_selects_the_right_agent(monkeypatch):
    """`context` is what routes scheduling to Agent A rather than Agent B."""
    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(json.dumps({
            "intent": "next_step", "context": "knowledge",
            "entities": {"topic": "refunds"}, "confidence": 0.9,
        })),
    )
    result = classify_intent("call next Tue about refunds", request_id="golden")
    assert result["intent"] == "next_step"
    assert result["context"] == "knowledge"


def test_invalid_status_label_is_rejected(monkeypatch):
    """Only the CRM's three stages are accepted."""
    monkeypatch.setattr(
        intent_module,
        "get_llm",
        fake_llm_factory(json.dumps({
            "intent": "status_update",
            "entities": {"statusLabel": "Maybe"},
            "confidence": 0.9,
        })),
    )
    result = classify_intent("we might lose Acme", request_id="golden")
    # Validation falls back rather than passing an unusable label downstream.
    assert result["entities"]["statusLabel"] is None or result["fallbackUsed"]


def test_empty_message_is_unknown_not_an_error(monkeypatch):
    monkeypatch.setattr(intent_module, "get_llm", fake_llm_factory())
    result = classify_intent("   ", request_id="golden")
    assert result["intent"] == "unknown"
    assert result["confidence"] == 0.0
