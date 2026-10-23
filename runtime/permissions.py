"""Capability-based permission manager (deny by default).

An agent may only perform an action if it holds that capability. Sensitive
actions additionally need a human (or automated) confirmation.
"""
from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Callable, Iterable, Optional, Union

if TYPE_CHECKING:
    from runtime.agent import Agent
    from runtime.audit import AuditLog

SENSITIVE_ACTIONS: frozenset[str] = frozenset({"send_email", "delete_file"})

# confirm(agent_name, action, detail) -> True to approve
ConfirmCallback = Callable[[str, str, str], bool]


class PermissionDeniedError(Exception):
    """Agent does not hold the capability for an action."""

    def __init__(self, agent_name: str, action: str, reason: str = "not granted") -> None:
        super().__init__(f"'{agent_name}' denied '{action}': {reason}")
        self.agent_name = agent_name
        self.action = action


class ConfirmationRejectedError(PermissionDeniedError):
    """Agent holds the capability, but the human/automated confirmer said no."""

    def __init__(self, agent_name: str, action: str) -> None:
        super().__init__(agent_name, action, "confirmation rejected")


def interactive_confirm(agent_name: str, action: str, detail: str) -> bool:
    """Ask the human at the terminal."""
    answer = input(f"\n[AgentOS] '{agent_name}' wants to {action}: {detail}\n  Allow? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def auto_approve(agent_name: str, action: str, detail: str) -> bool:
    """Unattended mode: approve everything (for demos/experiments)."""
    return True


def auto_deny(agent_name: str, action: str, detail: str) -> bool:
    """Unattended mode: reject everything (safe default)."""
    return False


CONFIRM_MODES: dict[str, ConfirmCallback] = {
    "interactive": interactive_confirm,
    "auto-approve": auto_approve,
    "auto-deny": auto_deny,
}


def resolve_confirm(confirm: Union[str, ConfirmCallback]) -> ConfirmCallback:
    """Accept a mode name ('interactive', 'auto-approve', 'auto-deny') or a callable."""
    if callable(confirm):
        return confirm
    try:
        return CONFIRM_MODES[confirm]
    except KeyError:
        raise ValueError(f"Unknown confirm mode {confirm!r}; use one of {list(CONFIRM_MODES)}")


class PermissionManager:
    """Holds granted capabilities per agent and checks every request."""

    def __init__(
        self,
        confirm: Union[str, ConfirmCallback] = "auto-deny",
        sensitive_actions: Iterable[str] = SENSITIVE_ACTIONS,
        audit: Optional["AuditLog"] = None,
    ) -> None:
        self.confirm = resolve_confirm(confirm)
        self.sensitive_actions = frozenset(sensitive_actions)
        self.audit = audit
        self._grants: dict[str, set[str]] = {}
        self._lock = threading.Lock()
        self._confirm_lock = threading.Lock()  # one confirmation prompt at a time

    def grant(self, agent_id: str, *actions: str) -> None:
        with self._lock:
            self._grants.setdefault(agent_id, set()).update(actions)

    def revoke(self, agent_id: str, *actions: str) -> None:
        with self._lock:
            self._grants.get(agent_id, set()).difference_update(actions)

    def capabilities(self, agent_id: str) -> frozenset[str]:
        with self._lock:
            return frozenset(self._grants.get(agent_id, set()))

    def check(
        self, agent: "Agent", action: str, detail: str = "", task_id: Optional[str] = None
    ) -> None:
        """Allow, or raise PermissionDeniedError / ConfirmationRejectedError."""
        if action not in self.capabilities(agent.id):
            self._log("permission_denied", task_id, agent, action, detail)
            raise PermissionDeniedError(agent.name, action)

        if action in self.sensitive_actions:
            with self._confirm_lock:
                approved = bool(self.confirm(agent.name, action, detail))
            if not approved:
                self._log("permission_rejected", task_id, agent, action, detail)
                raise ConfirmationRejectedError(agent.name, action)
            self._log("permission_confirmed", task_id, agent, action, detail)
            return

        self._log("permission_allowed", task_id, agent, action, detail)

    def _log(
        self, event: str, task_id: Optional[str], agent: "Agent", action: str, detail: str
    ) -> None:
        if self.audit is not None:
            extra = {"detail": detail} if detail else {}
            self.audit.record(event, task_id=task_id, agent=agent.name, action=action, **extra)
