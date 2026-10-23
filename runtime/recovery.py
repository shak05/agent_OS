"""Recovery engine: decides what to do after a task attempt fails.

Policy: retry the current agent up to `max_retries` times, then reassign to
its registered backup agent (which gets the same retry budget), then escalate.
The engine only decides; the kernel carries out the decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

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

    def decide(self, acb: AgentControlBlock) -> RecoveryDecision:
        """Choose the next step for a task whose latest attempt just failed."""
        current = acb.agent_id
        if acb.attempts_on(current) <= self.max_retries:
            return RecoveryDecision(RecoveryAction.RETRY, current)

        backup = self.backup_for(current)
        if backup is not None and backup not in acb.agents_tried:
            return RecoveryDecision(RecoveryAction.REASSIGN, backup)

        return RecoveryDecision(RecoveryAction.ESCALATE)
