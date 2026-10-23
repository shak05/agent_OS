"""EmailAgent: drafts an email and "sends" it through a mock transport.

Nothing is really sent. The mock transport appends to sent_emails.json,
which stands in for an SMTP server.
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Union

from runtime.agent import Agent
from runtime.sandbox import DEFAULT_OUTPUT_DIR


class MockMailTransport:
    """Offline stand-in for an SMTP server: appends messages to a JSON file."""

    def __init__(self, outbox: Union[str, Path] = DEFAULT_OUTPUT_DIR / "sent_emails.json") -> None:
        self.outbox = Path(outbox)
        self._lock = threading.Lock()

    def deliver(self, message: dict[str, Any]) -> None:
        with self._lock:
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            sent = json.loads(self.outbox.read_text(encoding="utf-8")) if self.outbox.exists() else []
            sent.append(message)
            self.outbox.write_text(json.dumps(sent, indent=2), encoding="utf-8")

    def sent(self) -> list[dict[str, Any]]:
        if not self.outbox.exists():
            return []
        return json.loads(self.outbox.read_text(encoding="utf-8"))


class EmailAgent(Agent):
    """Input: {"to", "subject", "body", "attachment" (optional)}."""

    def __init__(
        self,
        name: str = "EmailAgent",
        transport: Optional[MockMailTransport] = None,
        agent_id: Optional[str] = None,
    ) -> None:
        super().__init__(name, permissions=["send_email"], agent_id=agent_id)
        self.transport = transport or MockMailTransport()

    def run(self, task_input: dict[str, Any]) -> dict[str, Any]:
        draft = {
            "to": task_input["to"],
            "subject": task_input["subject"],
            "body": task_input["body"],
            "attachment": task_input.get("attachment"),
        }
        # Sensitive action: the kernel asks a human (or auto policy) to confirm.
        self.request_permission(
            "send_email", f"to={draft['to']} subject={draft['subject']!r} attachment={draft['attachment']}"
        )
        message = {**draft, "sent_at": datetime.now().isoformat(timespec="seconds"), "status": "sent (mock)"}
        self.transport.deliver(message)
        return {"status": "sent", "to": draft["to"], "outbox": str(self.transport.outbox)}
