"""Test harness for loading the two agents side by side.

In production each agent is its own container with its own /app on PYTHONPATH,
so `import tools` is unambiguous. In the test process both agents are importable
at once and their `tools` / `graph` / `app` modules would collide, so each agent
is loaded explicitly by file path and its modules are swapped into sys.modules
just long enough for the dependent module to bind them.
"""

import importlib.util
import sys
import types
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "agents"

# `shared` is a real package with no twin, so it can live on sys.path normally.
if str(AGENTS) not in sys.path:
    sys.path.insert(0, str(AGENTS))

# The agents read configuration at import time; give them something harmless so
# importing a module never requires a real key.
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("SELF_REFLECT", "true")


def _load_module(path: Path, name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    # Register before exec so intra-module circular imports resolve.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_agent(package_dir: str) -> dict[str, types.ModuleType]:
    """Load one agent's tools/graph/app in dependency order.

    Returns the three modules. Each keeps a direct reference to the ones it
    imported, so loading the *other* agent afterwards cannot rebind them.
    """
    base = AGENTS / package_dir
    tools = _load_module(base / "tools.py", "tools")
    graph = _load_module(base / "graph.py", "graph")
    app = _load_module(base / "app.py", "app")
    return {"tools": tools, "graph": graph, "app": app}


class FakeLLM:
    """Stand-in for ChatOpenAI: returns queued replies in order.

    Graph nodes call `get_llm().invoke(prompt)`, so a fake that records prompts
    lets tests assert on routing without any network access.
    """

    def __init__(self, *responses: str):
        self._responses = list(responses)
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if not self._responses:
            raise AssertionError("FakeLLM ran out of queued responses")
        content = self._responses.pop(0)
        return types.SimpleNamespace(content=content)


def fake_llm_factory(*responses: str):
    """Build a `get_llm` replacement that always hands back the same FakeLLM."""
    llm = FakeLLM(*responses)

    def _factory(*_args: Any, **_kwargs: Any) -> FakeLLM:
        return llm

    _factory.llm = llm  # type: ignore[attr-defined]
    return _factory


class FakeCollection:
    """In-memory stand-in for a Chroma collection."""

    def __init__(self, hits: dict[str, Any] | None = None):
        self.upserts: list[dict[str, Any]] = []
        self.deletes: list[dict[str, Any]] = []
        self._hits = hits or {"documents": [[]], "metadatas": [[]], "distances": [[]]}

    def upsert(self, **kwargs: Any) -> None:
        self.upserts.append(kwargs)

    def delete(self, **kwargs: Any) -> None:
        self.deletes.append(kwargs)

    def query(self, **_kwargs: Any) -> dict[str, Any]:
        return self._hits
