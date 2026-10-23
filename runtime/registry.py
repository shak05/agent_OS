"""Agent Registry: the kernel's table of registered agent instances.

Like an OS process table, it knows every instance, its type, whether it is
available, and simple dispatch statistics. It does not schedule tasks,
select agents or grant permissions.
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from runtime.agent import Agent


class DuplicateAgentError(ValueError):
    """An agent with the same instance id is already registered."""


class AgentNotFoundError(KeyError):
    """No agent with this instance id is registered."""

    def __str__(self) -> str:  # KeyError would otherwise show quotes only
        return str(self.args[0])


@dataclass
class AgentRecord:
    """Registry metadata for one instance (kept separate from task/ACB data)."""

    agent: Agent
    agent_type: str
    registered_seq: int
    available: bool = True
    dispatch_count: int = 0
    last_dispatch_seq: int = -1  # -1 = never dispatched


class AgentRegistry:
    """Thread-safe store of agent instances keyed by unique instance id."""

    def __init__(self) -> None:
        self._records: dict[str, AgentRecord] = {}
        self._lock = threading.Lock()
        self._registered = itertools.count()
        self._dispatches = itertools.count()

    # ------------------------------------------------------------ membership
    def register(self, agent: Agent) -> Agent:
        with self._lock:
            if agent.id in self._records:
                raise DuplicateAgentError(f"Agent id {agent.id!r} is already registered")
            self._records[agent.id] = AgentRecord(agent, type(agent).__name__, next(self._registered))
        return agent

    def unregister(self, agent_id: str) -> Agent:
        with self._lock:
            record = self._records.pop(agent_id, None)
        if record is None:
            raise AgentNotFoundError(f"Unknown agent {agent_id!r}")
        return record.agent

    def get(self, agent_id: str) -> Agent:
        return self.record(agent_id).agent

    def record(self, agent_id: str) -> AgentRecord:
        with self._lock:
            record = self._records.get(agent_id)
        if record is None:
            raise AgentNotFoundError(f"Unknown agent {agent_id!r}; register it first")
        return record

    def list(self) -> list[Agent]:
        """All instances in registration order."""
        return [r.agent for r in self.records()]

    def records(self) -> list[AgentRecord]:
        with self._lock:
            return sorted(self._records.values(), key=lambda r: r.registered_seq)

    def __contains__(self, agent_id: object) -> bool:
        with self._lock:
            return agent_id in self._records

    def __len__(self) -> int:
        with self._lock:
            return len(self._records)

    # ---------------------------------------------------------- capabilities
    def find(self, required_capabilities: Iterable[str]) -> list[Agent]:
        """Instances whose capabilities include all of `required_capabilities`
        (registration order). Availability is NOT considered here."""
        required = frozenset(required_capabilities)
        return [r.agent for r in self.records() if required <= r.agent.capabilities]

    def agent_type(self, agent_id: str) -> str:
        return self.record(agent_id).agent_type

    # ---------------------------------------------------------- availability
    def set_available(self, agent_id: str, available: bool) -> None:
        """Take an instance out of (or back into) service."""
        record = self.record(agent_id)
        with self._lock:
            record.available = available

    def is_available(self, agent_id: str) -> bool:
        return self.record(agent_id).available

    # ------------------------------------------------------------ statistics
    def mark_dispatched(self, agent_id: str) -> None:
        """Called by the dispatcher when it assigns a task to this instance."""
        record = self.record(agent_id)
        with self._lock:
            record.dispatch_count += 1
            record.last_dispatch_seq = next(self._dispatches)
