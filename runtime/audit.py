"""Audit log: timestamped record of everything the kernel does."""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

EVENT_TYPES = (
    "submitted",
    "started",
    "completed",
    "failed",
    "retried",
    "reassigned",
    "escalated",
    "permission_allowed",
    "permission_denied",
    "permission_confirmed",
    "permission_rejected",
)


@dataclass
class AuditEvent:
    timestamp: str
    event: str
    task_id: Optional[str] = None
    agent: Optional[str] = None
    details: dict[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        time_part = self.timestamp[11:23]  # HH:MM:SS.mmm
        extra = " ".join(f"{k}={v}" for k, v in self.details.items())
        return f"{time_part}  {self.event:<21} {self.task_id or '-':<6} {self.agent or '-':<20} {extra}"


class AuditLog:
    """Thread-safe, append-only event log."""

    def __init__(self, echo: bool = False) -> None:
        self.echo = echo  # print each event as it happens (live status)
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    def record(
        self, event: str, task_id: Optional[str] = None, agent: Optional[str] = None, **details: Any
    ) -> AuditEvent:
        if event not in EVENT_TYPES:
            raise ValueError(f"Unknown audit event type: {event!r}")
        entry = AuditEvent(
            timestamp=datetime.now().isoformat(timespec="milliseconds"),
            event=event,
            task_id=task_id,
            agent=agent,
            details=details,
        )
        with self._lock:
            self._events.append(entry)
        if self.echo:
            print(entry.format(), flush=True)
        return entry

    def events(self, event: Optional[str] = None, task_id: Optional[str] = None) -> list[AuditEvent]:
        """Return events, optionally filtered by type and/or task id."""
        with self._lock:
            items = list(self._events)
        return [
            e
            for e in items
            if (event is None or e.event == event) and (task_id is None or e.task_id == task_id)
        ]

    def count(self, event: str) -> int:
        return len(self.events(event))

    def print_log(self) -> None:
        for e in self.events():
            print(e.format())

    def export_json(self, path: Union[str, Path]) -> Path:
        """Write all events to a JSON file and return its path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = [asdict(e) for e in self.events()]
        path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        return path

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._events)


AGENT_TABLE_COLUMNS = ("Agent", "Task", "State", "Attempts", "Duration", "Outcome")


def format_agents_table(rows: Iterable[dict[str, Any]]) -> str:
    """Render rows (keys = AGENT_TABLE_COLUMNS) as a plain-text table."""
    rows = [{c: str(r.get(c, "")) for c in AGENT_TABLE_COLUMNS} for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in AGENT_TABLE_COLUMNS}
    line = "+".join("-" * (widths[c] + 2) for c in AGENT_TABLE_COLUMNS)
    line = f"+{line}+"

    def fmt(r: dict[str, str]) -> str:
        return "| " + " | ".join(r[c].ljust(widths[c]) for c in AGENT_TABLE_COLUMNS) + " |"

    out = [line, fmt({c: c for c in AGENT_TABLE_COLUMNS}), line]
    out += [fmt(r) for r in rows] or ["| (no tasks) |"]
    out.append(line)
    return "\n".join(out)


def print_agents_table(rows: Iterable[dict[str, Any]]) -> None:
    print(format_agents_table(rows))
