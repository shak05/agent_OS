"""ResearchAgent: returns a fake research summary (mock, offline)."""
from __future__ import annotations

import random
import time
from typing import Any, Optional

from runtime.agent import Agent

_POINT_TEMPLATES = [
    "{t} is moving from research prototypes to production systems.",
    "Key challenges for {t} include reliability, cost and evaluation.",
    "Open-source tooling has lowered the barrier to entry for {t}.",
    "Regulation and safety reviews increasingly shape how {t} is deployed.",
    "Teams adopting {t} report the biggest gains from automation of routine work.",
]


class ResearchAgent(Agent):
    """Mock web researcher. Failure behaviour is configurable for recovery demos.

    failure_rate: probability (0..1) that any call fails.
    fail_first:   the first N calls always fail (deterministic, for demos).
    """

    def __init__(
        self,
        name: str = "ResearchAgent",
        delay: float = 0.3,
        failure_rate: float = 0.0,
        fail_first: int = 0,
        seed: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        super().__init__(name, permissions=["web_search"], agent_id=agent_id)
        self.delay = delay
        self.failure_rate = failure_rate
        self.fail_first = fail_first
        self.calls = 0
        self._rng = random.Random(seed)

    def run(self, task_input: Any) -> dict[str, Any]:
        topic = str(task_input.get("topic") if isinstance(task_input, dict) else task_input)
        self.request_permission("web_search", f"query={topic!r}")
        self.calls += 1
        time.sleep(self.delay)
        if self.calls <= self.fail_first or self._rng.random() < self.failure_rate:
            raise ConnectionError("mock search service timed out")

        points = [p.format(t=topic) for p in _POINT_TEMPLATES]
        return {
            "topic": topic,
            "summary": (
                f"{topic} is an active area with rapid adoption. This mock summary was "
                f"produced by {self.name} without internet access."
            ),
            "key_points": points,
            "sources": [
                f"https://example.org/{topic.lower().replace(' ', '-')}/overview",
                "https://example.org/industry-report-2026",
            ],
            "researched_by": self.name,
        }
