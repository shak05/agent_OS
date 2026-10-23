"""AgentOS kernel: ties scheduling, permissions, recovery and auditing together.

Agents never schedule, check permissions or recover themselves. They are
registered here, tasks are submitted here, and the kernel dispatches them.
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
from runtime.permissions import (
    ConfirmCallback,
    ConfirmationRejectedError,
    PermissionDeniedError,
    PermissionManager,
)
from runtime.recovery import RecoveryAction, RecoveryEngine
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

        # EXTENSION POINT (Phase 2): Memory Manager.
        # A MemoryManager would give each agent scoped, persistent context.
        # Plug it in here and load/save context around agent.run() in _execute().
        self.memory: Any = None

        # EXTENSION POINT (Phase 2): Event Bus.
        # Every kernel event already flows through _emit(); an EventBus would
        # publish those events to subscribers (dashboard, other agents, ...).
        self.event_bus: Any = None

        self._agents: dict[str, Agent] = {}
        self._tasks: list[AgentControlBlock] = []
        self._tasks_lock = threading.Lock()
        self._task_ids = itertools.count(1)
        self._current = threading.local()  # task being executed on this thread

    # ------------------------------------------------------------------ agents
    def register(self, agent: Agent, backup_for: Optional[Union[str, Agent]] = None) -> Agent:
        """Register an agent and grant the capabilities it declares.

        If `backup_for` is given, this agent becomes the backup for that agent.
        """
        if agent.id in self._agents:
            raise ValueError(f"Agent id {agent.id!r} is already registered")
        self._agents[agent.id] = agent
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

    def agent(self, agent_id: str) -> Agent:
        return self._agents[agent_id]

    @property
    def agents(self) -> list[Agent]:
        return list(self._agents.values())

    # ------------------------------------------------------------------- tasks
    def submit(
        self,
        agent: Union[str, Agent],
        task_input: Any = None,
        priority: int = 5,
        required_permissions: Iterable[str] = (),
    ) -> AgentControlBlock:
        """Queue a task for an agent and return its ACB."""
        agent_id = agent.id if isinstance(agent, Agent) else agent
        if agent_id not in self._agents:
            raise KeyError(f"Unknown agent {agent_id!r}; register it first")
        acb = AgentControlBlock(
            task_id=f"T{next(self._task_ids):03d}",
            agent_id=agent_id,
            task_input=task_input,
            priority=priority,
            required_permissions=tuple(required_permissions),
        )
        with self._tasks_lock:
            self._tasks.append(acb)
        target = self._agents[agent_id]
        if target.state != AgentState.RUNNING:
            target.state = AgentState.WAITING
        self.scheduler.submit(acb)
        self._emit("submitted", acb, priority=priority)
        return acb

    @property
    def tasks(self) -> list[AgentControlBlock]:
        with self._tasks_lock:
            return list(self._tasks)

    # --------------------------------------------------------------- dispatch
    def run_next(self) -> Optional[AgentControlBlock]:
        """Dispatch the next task chosen by the scheduler. None if queue is empty."""
        acb = self.scheduler.next()
        if acb is not None:
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
        agent: Union[str, Agent],
        task_input: Any = None,
        priority: int = 5,
        required_permissions: Iterable[str] = (),
    ) -> AgentControlBlock:
        """Submit one task and dispatch until it has finished. Returns its ACB."""
        acb = self.submit(agent, task_input, priority, required_permissions)
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
            while True:
                agent = self._agents[acb.agent_id]
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
                    decision = self.recovery.decide(acb)
                    if decision.action is RecoveryAction.RETRY:
                        self._emit("retried", acb, next_attempt=acb.attempts + 1)
                        if self.recovery.retry_delay:
                            time.sleep(self.recovery.retry_delay)
                        continue
                    if decision.action is RecoveryAction.REASSIGN:
                        new_agent = self._agents[decision.agent_id]
                        self._emit("reassigned", acb, to=new_agent.name)
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
        self.audit.record(event, task_id=acb.task_id, agent=self._agents[acb.agent_id].name, **details)
        # if self.event_bus is not None: self.event_bus.publish(event, acb, details)

    # ---------------------------------------------------------------- reports
    def agents_used(self) -> list[dict[str, Any]]:
        """One row per task: agent(s), task id, state, attempts, duration, outcome."""
        rows = []
        for acb in self.tasks:
            names = [self._agents[a].name for a in acb.agents_tried] or [
                self._agents[acb.agent_id].name
            ]
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

    def format_agents_used(self) -> str:
        return format_agents_table(self.agents_used())

    def print_agents_used(self) -> None:
        print(self.format_agents_used())

    def export_log(self, path: Optional[Union[str, Path]] = None) -> Path:
        """Export the audit log as JSON (default: <sandbox>/audit_log.json)."""
        return self.audit.export_json(path or self.sandbox.root / "audit_log.json")
