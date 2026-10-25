"""Agent Control Block: the kernel's record of one task (like a PCB for a process)."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from runtime.agent import AgentState


def now() -> float:
    """Monotonic clock used for all task timings."""
    return time.perf_counter()


@dataclass
class AgentControlBlock:
    """Everything the kernel knows about one submitted task."""

    task_id: str
    # Agent currently assigned (changes on reassignment). "" = not yet bound:
    # the dispatcher binds an instance when the task is dispatched.
    agent_id: str
    task_input: Any = None
    priority: int = 5  # lower number = higher priority
    required_permissions: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()  # used by the dispatcher
    label: str = ""  # free-form tag, e.g. the workflow step name
    metadata: dict[str, Any] = field(default_factory=dict)  # facts agents report via annotate()
    state: AgentState = AgentState.WAITING
    submit_time: float = field(default_factory=now)
    start_time: Optional[float] = None
    end_time: Optional[float] = None
    attempt_log: list[str] = field(default_factory=list)  # agent id per attempt
    result: Any = None
    error: Optional[str] = None
    # pending | completed | escalated | denied | rejected | no_agent
    outcome: str = "pending"
    seq: int = 0  # submission order, set by the scheduler (FIFO tie-break)
    original_agent_id: str = ""

    def __post_init__(self) -> None:
        if not self.original_agent_id:
            self.original_agent_id = self.agent_id

    @property
    def attempts(self) -> int:
        return len(self.attempt_log)

    def attempts_on(self, agent_id: str) -> int:
        """Number of attempts made by one specific agent."""
        return self.attempt_log.count(agent_id)

    @property
    def agents_tried(self) -> list[str]:
        """Unique agent ids in the order they were tried."""
        return list(dict.fromkeys(self.attempt_log))

    @property
    def done(self) -> bool:
        return self.end_time is not None

    @property
    def wait_time(self) -> Optional[float]:
        """Time spent queued before first dispatch."""
        return None if self.start_time is None else self.start_time - self.submit_time

    @property
    def duration(self) -> Optional[float]:
        """Time from first dispatch to finish (includes retries)."""
        if self.start_time is None or self.end_time is None:
            return None
        return self.end_time - self.start_time

    @property
    def turnaround(self) -> Optional[float]:
        """Time from submission to finish."""
        return None if self.end_time is None else self.end_time - self.submit_time

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "original_agent_id": self.original_agent_id,
            "label": self.label,
            "required_capabilities": list(self.required_capabilities),
            "priority": self.priority,
            "state": self.state.value,
            "outcome": self.outcome,
            "attempts": self.attempts,
            "agents_tried": self.agents_tried,
            "wait_time": self.wait_time,
            "duration": self.duration,
            "turnaround": self.turnaround,
            "error": self.error,
            "result": repr(self.result) if self.result is not None else None,
        }
