"""Demo fleet: several registered instances of each existing agent class.

This is configuration, not kernel logic: the kernel only sees agent
instances in its registry. Add or remove instances here (or register more
at runtime) without touching the kernel.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from agents import DocGenAgent, EmailAgent, FileManagerAgent, MockMailTransport, ResearchAgent
from runtime.agent import Agent
from runtime.kernel import AgentOS

if TYPE_CHECKING:
    from llm import LLMClient

# instance-id prefix -> number of instances
DEMO_FLEET: dict[str, int] = {"research": 3, "docgen": 2, "filemanager": 2, "email": 2}

# Capabilities each workflow step needs (what the dispatcher matches on).
STEP_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "research": ("web_research",),
    "docgen": ("document_generation",),
    "file": ("file_read", "file_write"),
    "email": ("email_send",),
}


def register_demo_fleet(
    aos: AgentOS,
    research_delay: float = 0.0,
    outbox: Optional[Path] = None,
    fleet: Optional[dict[str, int]] = None,
    llm: Optional["LLMClient"] = None,
) -> dict[str, list[Agent]]:
    """Register the demo instances (ids like research-01) and return them by prefix.

    All research instances share one LLM client: by default a real OllamaClient
    configured from OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_TIMEOUT.
    """
    if llm is None:
        from llm import OllamaClient

        llm = OllamaClient()
    transport = MockMailTransport(outbox or aos.sandbox.root / "sent_emails.json")
    factories: dict[str, Callable[[str], Agent]] = {
        "research": lambda iid: ResearchAgent(iid, delay=research_delay, agent_id=iid, llm=llm),
        "docgen": lambda iid: DocGenAgent(iid, agent_id=iid),
        "filemanager": lambda iid: FileManagerAgent(iid, agent_id=iid),
        "email": lambda iid: EmailAgent(iid, transport=transport, agent_id=iid),
    }
    created: dict[str, list[Agent]] = {}
    for prefix, count in (fleet or DEMO_FLEET).items():
        created[prefix] = [aos.register(factories[prefix](f"{prefix}-{n:02d}")) for n in range(1, count + 1)]
    return created
