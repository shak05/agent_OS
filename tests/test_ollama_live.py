"""Optional live test against a REAL local Ollama server.

Skipped by default. Enable explicitly (it takes one to a few minutes):

    set AGENTOS_LIVE_OLLAMA=1            (Windows cmd)
    $env:AGENTOS_LIVE_OLLAMA = "1"       (PowerShell)
    export AGENTOS_LIVE_OLLAMA=1         (macOS / Linux / Git Bash)
    python -m pytest tests/test_ollama_live.py -v -s

Uses OLLAMA_BASE_URL / OLLAMA_MODEL (default llama3:latest).
"""
from __future__ import annotations

import os

import pytest

from agents import ResearchAgent
from llm import OllamaClient
from runtime import AgentOS

pytestmark = [
    pytest.mark.live_ollama,
    pytest.mark.skipif(os.environ.get("AGENTOS_LIVE_OLLAMA") != "1",
                       reason="live Ollama test disabled; set AGENTOS_LIVE_OLLAMA=1 to run it"),
]


def test_live_ollama_research_task() -> None:
    client = OllamaClient()
    ok, message = client.health()
    assert ok, message
    aos = AgentOS()
    aos.register(ResearchAgent("research-01", agent_id="research-01", llm=client))
    acb = aos.run_task(None, {"topic": "Benefits of process scheduling in operating systems"},
                       required_capabilities=["web_research"])
    assert acb.outcome == "completed", acb.error
    result = acb.result
    print(f"\n[{result['model']} via {result['backend']}, {result['duration_s']} s]\n{result['raw_text']}")
    assert result["backend"] == "ollama" and result["model"] == client.model
    assert result["summary"] and result["key_points"]
    assert aos.audit.events("completed")[0].details["llm_backend"] == "ollama"
