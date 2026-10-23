"""SyntheticAgent: a configurable fake worker for load testing."""
from __future__ import annotations

import random
import time
from typing import Any, Optional

from runtime.agent import Agent


class SyntheticAgent(Agent):
    """Sleeps a random short time and fails with probability `failure_rate`.

    Each agent has its own seeded RNG, so its behaviour is identical across
    runs regardless of thread interleaving.
    """

    def __init__(
        self,
        name: str,
        priority: int = 5,
        required_permission: str = "compute",
        failure_rate: float = 0.1,
        min_time: float = 0.01,
        max_time: float = 0.1,
        seed: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        super().__init__(name, permissions=[required_permission], agent_id=agent_id)
        self.priority = priority
        self.required_permission = required_permission
        self.failure_rate = failure_rate
        self.min_time = min_time
        self.max_time = max_time
        self._rng = random.Random(seed)

    def run(self, task_input: Any) -> dict[str, Any]:
        work = self._rng.uniform(self.min_time, self.max_time)
        fails = self._rng.random() < self.failure_rate
        time.sleep(work)
        if fails:
            raise RuntimeError(f"{self.name}: synthetic failure")
        return {"agent": self.name, "work_s": round(work, 4)}
