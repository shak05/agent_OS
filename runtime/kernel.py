"""AgentOS kernel: coordinates scheduling, dispatch, permissions, execution,
recovery and auditing.

Agents never schedule, check permissions or recover themselves. They are
registered here, tasks are submitted here, and for each task the kernel:
  1. asks the Scheduler which queued task runs next      (which TASK)
  2. asks the Dispatcher which instance should run it    (which AGENT)
  3. has the PermissionManager authorize required actions
  4. runs the agent, and on failure follows the RecoveryEngine's decision
  5. records every step in the AuditLog
"""
from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Optional, Union

from runtime.acb import AgentControlBlock, now
from runtime.agent import Agent, AgentState
from runtime.audit import AuditLog, format_agents_table
from runtime.dispatcher import Dispatcher, NoEligibleAgentError
from runtime.permissions import (
    ConfirmCallback,
    ConfirmationRejectedError,
    PermissionDeniedError,
    PermissionManager,
)
from runtime.recovery import RecoveryAction, RecoveryEngine
from runtime.registry import AgentRegistry
from runtime.sandbox import DEFAULT_OUTPUT_DIR, Sandbox, SandboxViolationError
from runtime.scheduler import Scheduler, SchedulingPolicy


class AgentOS:
    """The runtime layer that manages agents like an OS manages processes."""

    def __init__(
        self,
        policy: Optional[SchedulingPolicy] = None,
        confirm: Union[str, ConfirmCallback] = "auto-deny",
        max_retries: int = 2,
        retry_delay: float = 0.0,
        echo: bool = False,
        sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    ) -> None:
        self.audit = AuditLog(echo=echo)
        self.sandbox = Sandbox(sandbox_root)
        self.scheduler = Scheduler(policy)
        self.permissions = PermissionManager(confirm=confirm, audit=self.audit)
        self.recovery = RecoveryEngine(max_retries=max_retries, retry_delay=retry_delay)
        self.registry = AgentRegistry()
        self.dispatcher = Dispatcher(self.registry, self.permissions)

        # EXTENSION POINT (Phase 2): Memory Manager.
        # A MemoryManager would give each agent scoped, persistent context.
        # Plug it in here and load/save context around agent.run() in _execute().
        self.memory: Any = None

        # EXTENSION POINT (Phase 2): Event Bus.
        # Every kernel event already flows through _emit(); an EventBus would
        # publish those events to subscribers (dashboard, other agents, ...).
        self.event_bus: Any = None

        self._tasks: list[AgentControlBlock] = []
        self._tasks_lock = threading.Lock()
        self._task_ids = itertools.count(1)
        self._current = threading.local()  # task being executed on this thread

    # ------------------------------------------------------------------ agents
    def register(self, agent: Agent, backup_for: Optional[Union[str, Agent]] = None) -> Agent:
        """Register an agent instance and grant the permissions it declares.

        Raises DuplicateAgentError (a ValueError) if the id is taken.
        If `backup_for` is given, this agent becomes the backup for that agent.
        """
        self.registry.register(agent)
        agent._kernel = self
        self.permissions.grant(agent.id, *agent.permissions)
        if backup_for is not None:
            primary_id = backup_for.id if isinstance(backup_for, Agent) else backup_for
            self.recovery.register_backup(primary_id, agent.id)
        return agent

    def set_backup(self, primary: Union[str, Agent], backup: Union[str, Agent]) -> None:
        """Register `backup` as the fallback for `primary`."""
        p = primary.id if isinstance(primary, Agent) else primary
        b = backup.id if isinstance(backup, Agent) else backup
        self.recovery.register_backup(p, b)

    def unregister(self, agent: Union[str, Agent]) -> Agent:
        """Remove an instance: revoke its grants and drop its backup links.
        Queued tasks pinned to it will fail at dispatch with outcome no_agent."""
        agent_id = agent.id if isinstance(agent, Agent) else agent
        removed = self.registry.unregister(agent_id)
        self.permissions.revoke(agent_id, *self.permissions.granted(agent_id))
        self.recovery.forget(agent_id)
        return removed

    def agent(self, agent_id: str) -> Agent:
        return self.registry.get(agent_id)

    @property
    def agents(self) -> list[Agent]:
        return self.registry.list()

    # ------------------------------------------------------------------- tasks
    def submit(
        self,
        agent: Optional[Union[str, Agent]] = None,
        task_input: Any = None,
        priority: int = 5,
        required_permissions: Iterable[str] = (),
        *,
        required_capabilities: Iterable[str] = (),
        label: str = "",
    ) -> AgentControlBlock:
        """Queue a task and return its ACB.

        Either pin the task to `agent`, or pass agent=None with
        `required_capabilities`: the dispatcher then binds a compatible
        instance when the scheduler picks the task.
        """
        caps = tuple(sorted(set(required_capabilities)))
        agent_id = ""
        if agent is None:
            if not caps:
                raise ValueError("submit() needs an agent or required_capabilities")
        else:
            agent_id = agent.id if isinstance(agent, Agent) else agent
            target = self.registry.get(agent_id)  # AgentNotFoundError if unknown
            missing = sorted(set(caps) - target.capabilities)
            if missing:
                raise ValueError(f"Agent {agent_id!r} lacks required capabilities {missing}")
            if target.state != AgentState.RUNNING:
                target.state = AgentState.WAITING
        acb = AgentControlBlock(
            task_id=f"T{next(self._task_ids):03d}",
            agent_id=agent_id,
            task_input=task_input,
            priority=priority,
            required_permissions=tuple(required_permissions),
            required_capabilities=caps,
            label=label,
        )
        with self._tasks_lock:
            self._tasks.append(acb)
        self.scheduler.submit(acb)
        extra = {"capabilities": ",".join(caps)} if caps else {}
        self._emit("submitted", acb, priority=priority, **extra)
        return acb

    @property
    def tasks(self) -> list[AgentControlBlock]:
        with self._tasks_lock:
            return list(self._tasks)

    # --------------------------------------------------------------- dispatch
    def run_next(self) -> Optional[AgentControlBlock]:
        """Run the next task chosen by the scheduler. None if queue is empty."""
        acb = self.scheduler.next()  # scheduler: WHICH TASK runs next
        if acb is not None:
            self._emit("scheduled", acb, policy=self.scheduler.policy.name, priority=acb.priority)
            self._execute(acb)
        return acb

    def run_all(
        self, workers: int = 1, input_closed: Optional[threading.Event] = None
    ) -> list[AgentControlBlock]:
        """Run until the queue is empty, using `workers` concurrent threads.

        If `input_closed` is given, tasks may still be arriving: idle workers
        keep polling the queue until that event is set and the queue is empty.
        """
        finished: list[AgentControlBlock] = []
        lock = threading.Lock()

        def worker() -> None:
            while True:
                acb = self.run_next()
                if acb is not None:
                    with lock:
                        finished.append(acb)
                elif input_closed is None or (input_closed.is_set() and len(self.scheduler) == 0):
                    return
                else:
                    time.sleep(0.001)  # idle: wait for new arrivals

        if workers <= 1:
            worker()
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for f in [pool.submit(worker) for _ in range(workers)]:
                    f.result()
        return finished

    def run_task(
        self,
        agent: Optional[Union[str, Agent]] = None,
        task_input: Any = None,
        priority: int = 5,
        required_permissions: Iterable[str] = (),
        *,
        required_capabilities: Iterable[str] = (),
        label: str = "",
    ) -> AgentControlBlock:
        """Submit one task and dispatch until it has finished. Returns its ACB."""
        acb = self.submit(agent, task_input, priority, required_permissions,
                          required_capabilities=required_capabilities, label=label)
        while not acb.done and self.run_next() is not None:
            pass
        return acb

    # ---------------------------------------------------------------- syscalls
    def syscall_permission(self, agent: Agent, action: str, detail: str = "") -> None:
        """Called by Agent.request_permission(). Raises if not allowed."""
        acb: Optional[AgentControlBlock] = getattr(self._current, "acb", None)
        self.permissions.check(agent, action, detail, task_id=acb.task_id if acb else None)

    def syscall_path(self, agent: Agent, relative: str, action: str) -> Path:
        """Called by Agent.sandbox_path(): check the capability and confine the path."""
        acb: Optional[AgentControlBlock] = getattr(self._current, "acb", None)
        task_id = acb.task_id if acb else None
        try:
            path = self.sandbox.resolve(relative)
        except SandboxViolationError:
            self.audit.record(
                "permission_denied", task_id=task_id, agent=agent.name,
                action=action, detail=f"{relative} (outside sandbox)",
            )
            raise PermissionDeniedError(agent.name, action, f"{relative} is outside the sandbox")
        self.permissions.check(agent, action, str(relative), task_id=task_id)
        return path

    # --------------------------------------------------------------- execution
    def _execute(self, acb: AgentControlBlock) -> None:
        """Run one task to a final outcome, applying recovery on failure."""
        self._current.acb = acb
        acb.start_time = now()
        acb.state = AgentState.RUNNING
        try:
            if not self._bind(acb):
                return
            while True:
                agent = self.registry.get(acb.agent_id)
                acb.attempt_log.append(agent.id)
                agent.state = AgentState.RUNNING
                self._emit("started", acb, attempt=acb.attempts)
                try:
                    for action in acb.required_permissions:
                        self.permissions.check(agent, action, task_id=acb.task_id)
                    result = agent.run(acb.task_input)
                except PermissionDeniedError as exc:
                    # Security decisions are final: no retry, no reassignment.
                    agent.state = AgentState.FAILED
                    outcome = "rejected" if isinstance(exc, ConfirmationRejectedError) else "denied"
                    self._finish(acb, AgentState.FAILED, outcome, error=str(exc))
                    self._emit("failed", acb, error=str(exc), outcome=outcome)
                    return
                except Exception as exc:  # agent crashed -> recovery engine decides
                    agent.state = AgentState.FAILED
                    acb.error = f"{type(exc).__name__}: {exc}"
                    self._emit("failed", acb, attempt=acb.attempts, error=acb.error)
                    decision = self.recovery.decide(
                        acb,
                        is_eligible=lambda aid: self._eligible_for(acb, aid),
                        find_alternative=(
                            (lambda: self.dispatcher.find_alternative(
                                acb.required_capabilities, acb.required_permissions, acb.agents_tried))
                            if acb.required_capabilities else None  # never guess for untyped tasks
                        ),
                    )
                    if decision.action is RecoveryAction.RETRY:
                        self._emit("retried", acb, next_attempt=acb.attempts + 1)
                        if self.recovery.retry_delay:
                            time.sleep(self.recovery.retry_delay)
                        continue
                    if decision.action is RecoveryAction.REASSIGN:
                        new_agent = self.registry.get(decision.agent_id)
                        via = "backup link" if self.recovery.backup_for(acb.agent_id) == new_agent.id else "dispatcher"
                        self._emit("reassigned", acb, to=new_agent.name, to_id=new_agent.id, via=via)
                        acb.agent_id = new_agent.id
                        continue
                    self._finish(acb, AgentState.FAILED, "escalated", error=acb.error)
                    self._emit("escalated", acb, attempts=acb.attempts, error=acb.error)
                    return
                else:
                    agent.state = AgentState.COMPLETED
                    self._finish(acb, AgentState.COMPLETED, "completed", result=result)
                    self._emit("completed", acb, attempts=acb.attempts)
                    return
        finally:
            self._current.acb = None

    def _bind(self, acb: AgentControlBlock) -> bool:
        """Bind an agent instance to the task (dispatcher: WHICH AGENT).
        Returns False, with the task finished as no_agent, if none is eligible."""
        if acb.agent_id:  # pinned at submission
            if acb.agent_id not in self.registry or not self.registry.is_available(acb.agent_id):
                self._fail_dispatch(acb, f"pinned agent {acb.agent_id!r} is not registered or unavailable")
                return False
            self._emit("dispatched", acb, reason="pinned at submission")
            return True
        try:
            decision = self.dispatcher.select(acb.required_capabilities, acb.required_permissions)
        except NoEligibleAgentError as exc:
            self._fail_dispatch(acb, str(exc))
            return False
        acb.agent_id = acb.original_agent_id = decision.agent.id
        self._emit("dispatched", acb, candidates=",".join(decision.candidates), reason=decision.reason)
        return True

    def _fail_dispatch(self, acb: AgentControlBlock, reason: str) -> None:
        self._finish(acb, AgentState.FAILED, "no_agent", error=reason)
        self._emit("dispatch_failed", acb, error=reason)

    def _eligible_for(self, acb: AgentControlBlock, agent_id: str) -> bool:
        """May `agent_id` take over this task? (compatible, available, authorized)"""
        if agent_id not in self.registry:
            return False
        agent = self.registry.get(agent_id)
        return self.dispatcher.check(
            agent, acb.required_capabilities, acb.required_permissions, acb.agents_tried
        ).eligible

    def _name(self, agent_id: str) -> str:
        return self.registry.get(agent_id).name if agent_id in self.registry else agent_id

    def _finish(
        self,
        acb: AgentControlBlock,
        state: AgentState,
        outcome: str,
        result: Any = None,
        error: Optional[str] = None,
    ) -> None:
        acb.end_time = now()
        acb.state = state
        acb.outcome = outcome
        acb.result = result
        acb.error = error

    def _emit(self, event: str, acb: AgentControlBlock, **details: Any) -> None:
        """Single place every kernel event goes through (see Event Bus extension point)."""
        agent = self.registry.get(acb.agent_id) if acb.agent_id in self.registry else None
        self.audit.record(
            event, task_id=acb.task_id,
            agent=agent.name if agent else None,
            agent_id=agent.id if agent else None,  # None while no instance is bound
            agent_type=type(agent).__name__ if agent else None,
            **details,
        )
        # if self.event_bus is not None: self.event_bus.publish(event, acb, details)

    # ---------------------------------------------------------------- reports
    def agents_used(self) -> list[dict[str, Any]]:
        """One row per task: agent(s), task id, state, attempts, duration, outcome."""
        rows = []
        for acb in self.tasks:
            names = [self._name(a) for a in acb.agents_tried] or (
                [self._name(acb.agent_id)] if acb.agent_id else ["(none)"]
            )
            rows.append(
                {
                    "Agent": " -> ".join(names),
                    "Task": acb.task_id,
                    "State": acb.state.value,
                    "Attempts": acb.attempts,
                    "Duration": f"{acb.duration:.3f}s" if acb.duration is not None else "-",
                    "Outcome": acb.outcome,
                }
            )
        return rows

    def instance_usage(self) -> list[dict[str, Any]]:
        """Per registered instance: dispatches, attempts run and tasks completed.
        Supplementary utilization info; not a scheduling metric."""
        tasks = self.tasks
        rows = []
        for rec in self.registry.records():
            aid = rec.agent.id
            rows.append({
                "instance": aid,
                "type": rec.agent_type,
                "available": rec.available,
                "dispatched": rec.dispatch_count,
                "attempts": sum(t.attempts_on(aid) for t in tasks),
                "completed": sum(1 for t in tasks if t.agent_id == aid and t.outcome == "completed"),
            })
        return rows

    def format_agents_used(self) -> str:
        return format_agents_table(self.agents_used())

    def print_agents_used(self) -> None:
        print(self.format_agents_used())

    def export_log(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Export the audit log as JSON (default: <sandbox>/audit_log.json)."""
        return self.audit.export_json(path or self.sandbox.root / "audit_log.json")
