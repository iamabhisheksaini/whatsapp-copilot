"""In-process counters for the metrics the assignment asks for (§7).

Deliberately small: a process-local registry exposed as JSON on /metrics, with
no Prometheus dependency. It answers the four questions the brief names —
ingested files, Q&A latency, retrieval hit rate, lead funnel counts — and
resets when the container restarts, which is the honest scope for a demo.

For a production deployment this would be a Prometheus client and the same call
sites would not change.
"""

import threading
import time
from collections import Counter, deque
from contextlib import contextmanager
from typing import Any

_lock = threading.Lock()
_counters: Counter = Counter()
# Bounded so a long-running process cannot grow this without limit.
_latencies: dict[str, deque[float]] = {}
_MAX_SAMPLES = 500


def incr(name: str, amount: int = 1) -> None:
    with _lock:
        _counters[name] += amount


def observe(name: str, millis: float) -> None:
    with _lock:
        samples = _latencies.setdefault(name, deque(maxlen=_MAX_SAMPLES))
        samples.append(millis)


@contextmanager
def timed(name: str):
    """Record wall-clock duration of a block, whether or not it raises."""
    started = time.perf_counter()
    try:
        yield
    finally:
        observe(name, (time.perf_counter() - started) * 1000)


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(int(round((pct / 100) * (len(ordered) - 1))), len(ordered) - 1)
    return round(ordered[idx], 1)


def snapshot() -> dict[str, Any]:
    with _lock:
        counters = dict(_counters)
        latencies = {k: list(v) for k, v in _latencies.items()}

    timings = {
        name: {
            "count": len(vals),
            "p50Ms": _percentile(vals, 50),
            "p95Ms": _percentile(vals, 95),
            "maxMs": round(max(vals), 1) if vals else 0.0,
        }
        for name, vals in latencies.items()
    }

    # Retrieval hit rate: the share of questions that found any context at all.
    asks = counters.get("ask_total", 0)
    hits = counters.get("ask_with_hits", 0)
    derived = {}
    if asks:
        derived["retrievalHitRate"] = round(hits / asks, 3)
        derived["lowConfidenceRate"] = round(counters.get("ask_low_confidence", 0) / asks, 3)
        derived["revisionRate"] = round(counters.get("ask_revised", 0) / asks, 3)

    # Lead funnel: how far captured leads progressed.
    funnel = {
        k.split("funnel_", 1)[1]: v for k, v in counters.items() if k.startswith("funnel_")
    }
    if funnel:
        derived["leadFunnel"] = funnel

    return {"counters": counters, "latency": timings, "derived": derived}
