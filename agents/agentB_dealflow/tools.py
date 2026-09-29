"""Deterministic enrichment helpers for Agent B.

These are pure functions on purpose: domain guessing, budget normalisation and
lead scoring are rules, not judgement calls, so they run without an LLM and are
covered by fast unit tests.
"""

import re
from typing import Any, Dict, Optional, Tuple

# Suffixes to strip before turning a company name into a domain.
_LEGAL_SUFFIXES = (
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "corp", "corp.",
    "corporation", "co", "co.", "gmbh", "bv", "plc", "pvt", "private",
    "technologies", "technology", "solutions", "systems", "labs", "group",
)

_BUDGET_RE = re.compile(
    r"""(?P<currency>[$€£₹]|usd|eur|gbp|inr|rs\.?)?\s*
        (?P<amount>\d[\d,]*(?:\.\d+)?)\s*
        (?P<scale>k|m|thousand|million|lakh|lakhs|cr|crore)?""",
    re.IGNORECASE | re.VERBOSE,
)

_SCALES = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "lakh": 100_000, "lakhs": 100_000,
    "cr": 10_000_000, "crore": 10_000_000,
}

_CURRENCY_SYMBOLS = {
    "$": "USD", "€": "EUR", "£": "GBP", "₹": "INR",
    "usd": "USD", "eur": "EUR", "gbp": "GBP", "inr": "INR",
    "rs": "INR", "rs.": "INR",
}


def guess_company_domain(company: Optional[str]) -> Optional[str]:
    """Best-effort domain from a company name.

    A guess, not a lookup — Agent B does no network calls. n8n or a human can
    correct it; the CRM column is advisory.
    """
    if not company or not company.strip():
        return None

    # Already a domain or an email? Use it directly.
    cleaned = company.strip().lower()
    if "@" in cleaned:
        return cleaned.split("@", 1)[1] or None
    if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", cleaned):
        return cleaned

    words = [w for w in re.split(r"[^a-z0-9]+", cleaned) if w]
    words = [w for w in words if w not in _LEGAL_SUFFIXES]
    if not words:
        return None
    return "".join(words) + ".com"


def normalize_budget(budget: Any) -> Tuple[Optional[float], Optional[str], Optional[str]]:
    """Return (amount, currency, originalText) from free text like "~10k" or "$50,000"."""
    if budget is None:
        return None, None, None
    if isinstance(budget, (int, float)):
        return float(budget), None, str(budget)

    text = str(budget).strip()
    if not text:
        return None, None, None

    match = _BUDGET_RE.search(text)
    if not match:
        return None, None, text

    try:
        amount = float(match.group("amount").replace(",", ""))
    except ValueError:
        return None, None, text

    scale = (match.group("scale") or "").lower()
    if scale in _SCALES:
        amount *= _SCALES[scale]

    currency_token = (match.group("currency") or "").lower().rstrip(".")
    currency = _CURRENCY_SYMBOLS.get(currency_token) or _CURRENCY_SYMBOLS.get(
        match.group("currency") or ""
    )
    return amount, currency, text


# Fields that make a lead actionable, and what they contribute to the score.
_SCORE_WEIGHTS = {
    "name": 0.2,
    "company": 0.25,
    "intent": 0.2,
    "budget": 0.2,
    "timeline": 0.15,
}


def score_lead(lead: Dict[str, Any]) -> Tuple[float, list]:
    """Score 0..1 on completeness, plus the list of fields still missing.

    n8n uses `missingFields` to ask a targeted follow-up question ("what's the
    timeline?") instead of a generic one.
    """
    score, missing = 0.0, []
    for field, weight in _SCORE_WEIGHTS.items():
        value = lead.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            missing.append(field)
        else:
            score += weight
    return round(score, 2), missing


def clean_text(value: Any, limit: int = 2000) -> Optional[str]:
    """Strip HTML tags and clamp length — basic input sanitisation."""
    if value is None:
        return None
    text = re.sub(r"<[^>]+>", "", str(value)).strip()
    return text[:limit] or None
