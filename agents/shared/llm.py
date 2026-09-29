"""Shared LLM / embedding factories, JSON coercion and structured logging.

Both agents import from here so model configuration lives in exactly one place.
Every knob is environment driven; the defaults match the local docker-compose
stack so `docker compose up` works with nothing but an API key set.
"""

import json
import logging
import os
import re
import sys
from typing import Any, Dict, Optional

from langchain_openai import ChatOpenAI, OpenAIEmbeddings

# --- configuration -----------------------------------------------------------

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Chat completions. Defaults to OpenRouter, which is what this project is wired
# to; point OPENAI_BASE_URL at api.openai.com to use OpenAI directly.
CHAT_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://openrouter.ai/api/v1")
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai/gpt-4o-mini")

# Embeddings are configured separately: not every OpenAI-compatible gateway
# exposes /embeddings, so the base URL and key can differ from the chat ones.
EMBEDDINGS_BASE_URL = os.getenv("EMBEDDINGS_BASE_URL", CHAT_BASE_URL)
EMBEDDINGS_API_KEY = os.getenv("EMBEDDINGS_API_KEY") or OPENAI_API_KEY
EMBEDDINGS_MODEL = os.getenv("EMBEDDINGS_MODEL", "text-embedding-3-small")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()


# --- structured logging ------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per line so logs stay greppable by requestId."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("requestId", "node", "agent", "durationMs"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(LOG_LEVEL)
        logger.propagate = False
    return logger


# --- model factories ---------------------------------------------------------

def get_llm(temperature: float = 0.0, model: Optional[str] = None) -> ChatOpenAI:
    """Chat model. Temperature 0 by default: these graphs want determinism."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is not set")
    return ChatOpenAI(
        model=model or CHAT_MODEL,
        temperature=temperature,
        api_key=OPENAI_API_KEY,
        base_url=CHAT_BASE_URL,
    )


def get_embeddings() -> OpenAIEmbeddings:
    if not EMBEDDINGS_API_KEY:
        raise RuntimeError("EMBEDDINGS_API_KEY / OPENAI_API_KEY is not set")
    return OpenAIEmbeddings(
        model=EMBEDDINGS_MODEL,
        api_key=EMBEDDINGS_API_KEY,
        base_url=EMBEDDINGS_BASE_URL,
    )


# --- JSON coercion -----------------------------------------------------------

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def safe_json(content: str) -> Dict[str, Any]:
    """Parse an LLM reply as JSON, tolerating code fences and stray prose.

    Returns `{"_raw": content}` when nothing parseable is found, so callers can
    detect failure without catching exceptions.
    """
    if not isinstance(content, str):
        return {"_raw": str(content)}

    text = _FENCE.sub("", content.strip()).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {"_raw": content}
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost {...} span, which handles "Here is the JSON: {…}".
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else {"_raw": content}
        except json.JSONDecodeError:
            pass
    return {"_raw": content}
