"""Capability-based dispatcher: chooses WHICH agent instance runs a task.

The scheduler decides which queued task runs next; the dispatcher only
answers "which registered instance should execute this task?". It never
sees the queue and never grants permissions.

Eligibility (all must hold):
  1. registered in the AgentRegistry
  2. has every required capability
  3. available (not taken out of service)
  4. already holds a grant for every permission the task declares
     (read-only check; the kernel still performs the real permission
     check, including human confirmation, at execution time)
  5. not excluded (e.g. instances that already failed this task)

Selection policy: least-recently-dispatched. Among eligible instances the
one dispatched longest ago (or never) wins; ties go to registration order.
With several equal instances this rotates work across them deterministically.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Optional

from runtime.agent import Agent
from runtime.registry import AgentRegistry

if TYPE_CHECKING:
    from runtime.permissions import PermissionManager

SELECTION_POLICY = "least-recently-dispatched"


@dataclass(frozen=True)
class Eligibility:
    """Why one instance can or cannot take a task."""

    agent_id: str
    eligible: bool
    reason: str


@dataclass(frozen=True)
class DispatchDecision:
    """The chosen instance plus an explanation for logs and demos."""

    agent: Agent
    agent_type: str
    candidates: tuple[str, ...]  # all eligible instance ids, in registration order
    reason: str


class NoEligibleAgentError(RuntimeError):
    """No registered instance can take the task."""

    def __init__(self, required: frozenset[str], report: list[Eligibility]) -> None:
        caps = ", ".join(sorted(required)) or "(none)"
        incapable = [e for e in report if e.reason.startswith("missing capability")]
        capable = [e for e in report if e not in incapable]
        if not report:
            detail = "registry is empty"
        elif not capable:
            detail = f"none of the {len(report)} registered instances has it"
        else:
            detail = "; ".join(f"{e.agent_id}: {e.reason}" for e in capable)
            if incapable:
                detail += f"; {len(incapable)} other instance(s) lack the capability"
        super().__init__(f"no eligible agent for capabilities [{caps}] ({detail})")
        self.required = required
        self.report = report


class Dispatcher:
    """Selects a compatible, available, authorized agent instance."""

    policy = SELECTION_POLICY

    def __init__(self, registry: AgentRegistry, permissions: "PermissionManager") -> None:
        self.registry = registry
        self.permissions = permissions
        self._lock = threading.Lock()  # select + mark_dispatched is atomic

    def check(
        self,
        agent: Agent,
        required_capabilities: Iterable[str] = (),
        required_permissions: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> Eligibility:
        """Evaluate one instance against the task's requirements."""
        if agent.id in set(exclude):
            return Eligibility(agent.id, False, "already tried for this task")
        if agent.id not in self.registry:
            return Eligibility(agent.id, False, "not registered")
        missing_caps = sorted(set(required_capabilities) - agent.capabilities)
        if missing_caps:
            return Eligibility(agent.id, False, f"missing capability {', '.join(missing_caps)}")
        if not self.registry.is_available(agent.id):
            return Eligibility(agent.id, False, "unavailable")
        missing_perms = sorted(set(required_permissions) - self.permissions.granted(agent.id))
        if missing_perms:
            return Eligibility(agent.id, False, f"no permission grant for {', '.join(missing_perms)}")
        caps = ", ".join(sorted(required_capabilities)) or "no specific capability"
        return Eligibility(agent.id, True, f"available, has {caps}")

    def explain(
        self,
        required_capabilities: Iterable[str],
        required_permissions: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> list[Eligibility]:
        """Eligibility of every registered instance (registration order)."""
        caps, perms, excl = tuple(required_capabilities), tuple(required_permissions), tuple(exclude)
        return [self.check(a, caps, perms, excl) for a in self.registry.list()]

    def select(
        self,
        required_capabilities: Iterable[str],
        required_permissions: Iterable[str] = (),
        exclude: Iterable[str] = (),
    ) -> DispatchDecision:
        """Choose an instance and record the dispatch. Raises NoEligibleAgentError."""
        caps = frozenset(required_capabilities)
        with self._lock:
            report = self.explain(caps, required_permissions, exclude)
            eligible = [e.agent_id for e in report if e.eligible]
            if not eligible:
                raise NoEligibleAgentError(caps, report)
            records = [self.registry.record(i) for i in eligible]
            chosen = min(records, key=lambda r: (r.last_dispatch_seq, r.registered_seq))
            self.registry.mark_dispatched(chosen.agent.id)
        reason = (
            f"{SELECTION_POLICY} of {len(eligible)} eligible"
            if len(eligible) > 1 else "only eligible instance"
        )
        return DispatchDecision(chosen.agent, chosen.agent_type, tuple(eligible), reason)

    def find_alternative(
        self,
        required_capabilities: Iterable[str],
        required_permissions: Iterable[str],
        exclude: Iterable[str],
    ) -> Optional[str]:
        """Instance id for recovery reassignment, or None if no compatible one is left."""
        try:
            return self.select(required_capabilities, required_permissions, exclude).agent.id
        except NoEligibleAgentError:
            return None
