"""Human approval gate for a web UI.

The CLI confirms sensitive actions with input(), which blocks the terminal.
A web UI cannot do that, so ApprovalBroker is a drop-in ConfirmCallback that
blocks the *agent's* thread until a human clicks Approve/Reject in the UI.

Security properties:
- Fail closed: no answer within `timeout` seconds means "rejected".
- Each request has a unique id; a stale or repeated click cannot approve a
  different request.
- The broker only answers; the PermissionManager still makes and logs the
  decision (permission_confirmed / permission_rejected).
"""
from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ApprovalRequest:
    """One pending or decided request for a sensitive action."""

    request_id: int
    agent_name: str
    action: str
    detail: str
    created_at: float = field(default_factory=time.time)
    timeout: float = 300.0
    approved: Optional[bool] = None  # None = still waiting
    decided_by: Optional[str] = None  # "human" | "timeout" | "cancelled"
    decided_at: Optional[float] = None
    _event: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def is_pending(self) -> bool:
        return self.approved is None

    @property
    def seconds_left(self) -> float:
        return max(0.0, self.created_at + self.timeout - time.time())


class ApprovalBroker:
    """ConfirmCallback that waits for a decision from another thread (the UI)."""

    def __init__(self, timeout: float = 300.0) -> None:
        self.timeout = timeout
        self.history: list[ApprovalRequest] = []
        self._pending: Optional[ApprovalRequest] = None
        self._ids = itertools.count(1)
        self._lock = threading.Lock()

    def __call__(self, agent_name: str, action: str, detail: str) -> bool:
        """Called by PermissionManager on the agent's thread. Blocks until decided."""
        req = ApprovalRequest(next(self._ids), agent_name, action, detail, timeout=self.timeout)
        with self._lock:
            self._pending = req
            self.history.append(req)
        req._event.wait(self.timeout)
        with self._lock:
            if req.approved is None:  # nobody answered: fail closed
                self._decide(req, False, "timeout")
            if self._pending is req:
                self._pending = None
            return bool(req.approved)

    def pending(self) -> Optional[ApprovalRequest]:
        with self._lock:
            return self._pending

    def resolve(self, request_id: int, approved: bool) -> bool:
        """Record the human's decision. Returns False for stale/duplicate clicks."""
        with self._lock:
            req = self._pending
            if req is None or req.request_id != request_id or not req.is_pending:
                return False
            self._decide(req, approved, "human")
            return True

    def cancel(self) -> None:
        """Reject whatever is pending (e.g. on shutdown)."""
        with self._lock:
            if self._pending is not None and self._pending.is_pending:
                self._decide(self._pending, False, "cancelled")

    @staticmethod
    def _decide(req: ApprovalRequest, approved: bool, by: str) -> None:
        req.approved = approved
        req.decided_by = by
        req.decided_at = time.time()
        req._event.set()
