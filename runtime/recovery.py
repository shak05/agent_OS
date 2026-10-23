"""Recovery engine: decides what to do after a task attempt fails.

Policy: retry the current agent up to `max_retries` times, then reassign to
a replacement (which gets the same retry budget), then escalate. Replacement
order: the explicitly registered backup, if it is still eligible; otherwise
another compatible instance found by the dispatcher (capability-based tasks
only). Agents already tried are never chosen again, so recovery always ends.
The engine only decides; the kernel carries out the decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

from runtime.acb import AgentControlBlock


class RecoveryAction(str, Enum):
    RETRY = "retry"
    REASSIGN = "reassign"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class RecoveryDecision:
    action: RecoveryAction
    agent_id: Optional[str] = None  # who should run the next attempt


class RecoveryEngine:
    """Retry -> reassign to backup -> escalate."""

    def __init__(self, max_retries: int = 2, retry_delay: float = 0.0) -> None:
        self.max_retries = max_retries
        self.retry_delay = retry_delay  # seconds to wait before a retry
        self._backups: dict[str, str] = {}

    def register_backup(self, primary_id: str, backup_id: str) -> None:
        if primary_id == backup_id:
            raise ValueError("An agent cannot be its own backup")
        self._backups[primary_id] = backup_id

    def backup_for(self, agent_id: str) -> Optional[str]:
        return self._backups.get(agent_id)

    def forget(self, agent_id: str) -> None:
        """Drop backup links to/from an agent that was unregistered."""
        self._backups = {p: b for p, b in self._backups.items() if agent_id not in (p, b)}

    def decide(
        self,
        acb: AgentControlBlock,
        is_eligible: Callable[[str], bool] = lambda agent_id: True,
        find_alternative: Optional[Callable[[], Optional[str]]] = None,
    ) -> RecoveryDecision:
        """Choose the next step for a task whose latest attempt just failed.

        is_eligible(agent_id): may this backup take the task (compatible,
            available, authorized)? Supplied by the kernel via the dispatcher.
        find_alternative(): another compatible, untried instance or None.
        """
        current = acb.agent_id
        if acb.attempts_on(current) <= self.max_retries:
            return RecoveryDecision(RecoveryAction.RETRY, current)

        backup = self.backup_for(current)
        if backup is not None and backup not in acb.agents_tried and is_eligible(backup):
            return RecoveryDecision(RecoveryAction.REASSIGN, backup)

        alternative = find_alternative() if find_alternative is not None else None
        if alternative is not None and alternative not in acb.agents_tried:
            return RecoveryDecision(RecoveryAction.REASSIGN, alternative)

        return RecoveryDecision(RecoveryAction.ESCALATE)
