"""Scheduler with pluggable policies.

A policy only supplies a sort key; the task with the smallest key runs next.
Keys are computed at dispatch time, so dynamic policies (like Aging) work
without re-sorting a heap. O(n) per dispatch is fine for hundreds of tasks.
"""
from __future__ import annotations

import itertools
import threading
from abc import ABC, abstractmethod
from typing import Optional

from runtime.acb import AgentControlBlock, now


class SchedulingPolicy(ABC):
    """Interface for scheduling policies."""

    name: str = "base"

    @abstractmethod
    def sort_key(self, acb: AgentControlBlock, current_time: float) -> tuple:
        """Return a key; the queued task with the lowest key is dispatched first."""


class FIFOPolicy(SchedulingPolicy):
    """First in, first out."""

    name = "FIFO"

    def sort_key(self, acb: AgentControlBlock, current_time: float) -> tuple:
        return (acb.seq,)


class PriorityPolicy(SchedulingPolicy):
    """Lower priority number runs first; FIFO among equal priorities."""

    name = "Priority"

    def sort_key(self, acb: AgentControlBlock, current_time: float) -> tuple:
        return (acb.priority, acb.seq)


class AgingPolicy(SchedulingPolicy):
    """Priority that improves the longer a task waits (prevents starvation).

    effective_priority = priority - rate * seconds_waited
    """

    name = "Aging"

    def __init__(self, rate: float = 1.0) -> None:
        self.rate = rate  # priority levels gained per second of waiting

    def effective_priority(self, acb: AgentControlBlock, current_time: float) -> float:
        return acb.priority - self.rate * (current_time - acb.submit_time)

    def sort_key(self, acb: AgentControlBlock, current_time: float) -> tuple:
        return (self.effective_priority(acb, current_time), acb.seq)


class Scheduler:
    """Thread-safe ready queue that dispatches according to a policy."""

    def __init__(self, policy: Optional[SchedulingPolicy] = None) -> None:
        self.policy = policy or FIFOPolicy()
        self._queue: list[AgentControlBlock] = []
        self._lock = threading.Lock()
        self._seq = itertools.count()

    def submit(self, acb: AgentControlBlock) -> None:
        with self._lock:
            acb.seq = next(self._seq)
            self._queue.append(acb)

    def next(self) -> Optional[AgentControlBlock]:
        """Remove and return the next task to run, or None if the queue is empty."""
        with self._lock:
            if not self._queue:
                return None
            t = now()
            best = min(self._queue, key=lambda a: self.policy.sort_key(a, t))
            self._queue.remove(best)
            return best

    def set_policy(self, policy: SchedulingPolicy) -> None:
        with self._lock:
            self.policy = policy

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)
