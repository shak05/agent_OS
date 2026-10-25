"""Test-wide guard: automated tests must never call a real Ollama server.

Any OllamaClient HTTP request fails immediately unless the test is marked
`real_http` (talks to the fake server in test_llm.py) or the opt-in live
suite is enabled with AGENTOS_LIVE_OLLAMA=1.
"""
from __future__ import annotations

import os

import pytest

from llm.ollama import OllamaClient, OllamaConnectionError

LIVE = os.environ.get("AGENTOS_LIVE_OLLAMA") == "1"


@pytest.fixture(autouse=True)
def _block_real_ollama(request, monkeypatch):
    if LIVE or request.node.get_closest_marker("real_http") or request.node.get_closest_marker("live_ollama"):
        yield
        return

    def blocked(self, method, path, payload, timeout):
        raise OllamaConnectionError("blocked in tests: inject MockLLMClient instead of calling Ollama")

    monkeypatch.setattr(OllamaClient, "_request", blocked)
    yield
