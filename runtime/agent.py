"""Agent base class.

Agents are simple workers that do one job. They contain NO scheduling,
permission or recovery logic: that all lives in the AgentOS kernel.
When an agent needs a capability at runtime it makes a "system call"
(request_permission) and the kernel decides.
"""
from __future__ import annotations

import re
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Optional

if TYPE_CHECKING:
    from runtime.kernel import AgentOS


class AgentState(str, Enum):
    """Lifecycle states, used for both agents and tasks (ACBs)."""

    IDLE = "IDLE"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"


class Agent:
    """Base class for all agents. Subclasses override run()."""

    def __init__(
        self,
        name: str,
        permissions: Iterable[str] = (),
        agent_id: Optional[str] = None,
        capabilities: Iterable[str] = (),
    ) -> None:
        self.name = name
        self.id = agent_id or _slug(name)
        # Permissions this agent declares it needs (like an app manifest).
        # The kernel's PermissionManager decides what is actually granted.
        self.permissions: list[str] = list(permissions)
        # What this agent CAN do (e.g. "web_research"). Used by the dispatcher
        # to match tasks to agents. A capability is never an authorization.
        self.capabilities: frozenset[str] = frozenset(capabilities)
        # Reflects the most recent task; per-task truth is in the ACB.
        self.state = AgentState.IDLE
        self._kernel: Optional["AgentOS"] = None

    def run(self, task_input: Any) -> Any:
        """Do the agent's one job and return a result. Raise on failure."""
        raise NotImplementedError(f"{type(self).__name__} must implement run()")

    def request_permission(self, action: str, detail: str = "") -> None:
        """System call: ask AgentOS for permission to perform `action`.

        Returns normally if allowed; raises PermissionDeniedError otherwise.
        """
        if self._kernel is None:
            raise RuntimeError(f"Agent '{self.name}' is not registered with AgentOS")
        self._kernel.syscall_permission(self, action, detail)

    def annotate(self, **info: Any) -> None:
        """System call: attach facts about the current task (e.g. the model used)
        so the kernel can include them in the task's audit events."""
        if self._kernel is not None:
            self._kernel.syscall_annotate(self, info)

    def sandbox_path(self, relative: str, action: str) -> "Path":
        """System call: get a file path inside the sandbox for `action`
        ('read_file' or 'write_file'). Raises PermissionDeniedError if the
        agent lacks the capability or the path escapes the sandbox.
        """
        if self._kernel is None:
            raise RuntimeError(f"Agent '{self.name}' is not registered with AgentOS")
        return self._kernel.syscall_path(self, relative, action)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id!r} state={self.state.value}>"
