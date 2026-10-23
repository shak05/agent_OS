"""Stage 1 tests: scheduling, permissions, recovery and auditing."""
from __future__ import annotations

import json
from typing import Any

from runtime import (
    Agent,
    AgentOS,
    AgentState,
    AgingPolicy,
    FIFOPolicy,
    PriorityPolicy,
)
from runtime.acb import now


# ---------------------------------------------------------------- test agents
class RecorderAgent(Agent):
    """Records the order in which it receives task inputs."""

    def __init__(self, name: str = "Recorder", **kw: Any) -> None:
        super().__init__(name, **kw)
        self.order: list[Any] = []

    def run(self, task_input: Any) -> Any:
        self.order.append(task_input)
        return task_input


class FlakyAgent(Agent):
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, name: str, fail_times: int) -> None:
        super().__init__(name)
        self.fail_times = fail_times
        self.calls = 0

    def run(self, task_input: Any) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError(f"simulated failure #{self.calls}")
        return "ok"


class AlwaysFailAgent(Agent):
    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.calls = 0

    def run(self, task_input: Any) -> str:
        self.calls += 1
        raise RuntimeError("always fails")


class MailerAgent(Agent):
    """Asks the kernel before sending (sensitive action)."""

    def __init__(self, permissions: tuple[str, ...] = ("send_email",)) -> None:
        super().__init__("Mailer", permissions=permissions)
        self.sent: list[str] = []

    def run(self, task_input: str) -> str:
        self.request_permission("send_email", f"to={task_input}")
        self.sent.append(task_input)
        return "sent"


def events(os_: AgentOS, kind: str) -> list:
    return os_.audit.events(kind)


# ------------------------------------------------------------------ scheduling
def test_fifo_order() -> None:
    os_ = AgentOS(policy=FIFOPolicy())
    rec = os_.register(RecorderAgent())
    for name, prio in [("a", 5), ("b", 1), ("c", 3)]:
        os_.submit(rec, name, priority=prio)
    os_.run_all()
    assert rec.order == ["a", "b", "c"]


def test_priority_order_with_fifo_tie_break() -> None:
    os_ = AgentOS(policy=PriorityPolicy())
    rec = os_.register(RecorderAgent())
    for name, prio in [("low", 9), ("high1", 1), ("mid", 5), ("high2", 1)]:
        os_.submit(rec, name, priority=prio)
    os_.run_all()
    assert rec.order == ["high1", "high2", "mid", "low"]


def test_aging_lets_old_low_priority_task_win() -> None:
    os_ = AgentOS(policy=AgingPolicy(rate=1.0))
    rec = os_.register(RecorderAgent())
    old = os_.submit(rec, "old-low", priority=9)
    os_.submit(rec, "new-high", priority=1)
    old.submit_time = now() - 20  # pretend it has waited 20 s -> effective -11
    os_.run_all()
    assert rec.order == ["old-low", "new-high"]


def test_parallel_workers_complete_everything() -> None:
    os_ = AgentOS(policy=PriorityPolicy())
    rec = os_.register(RecorderAgent())
    for i in range(50):
        os_.submit(rec, i, priority=i % 3)
    finished = os_.run_all(workers=4)
    assert len(finished) == 50
    assert all(a.outcome == "completed" for a in os_.tasks)


# ----------------------------------------------------------------- permissions
def test_permission_denied_by_default() -> None:
    os_ = AgentOS()
    rec = os_.register(RecorderAgent())  # declares no permissions
    acb = os_.run_task(rec, "x", required_permissions=["delete_file"])
    assert acb.outcome == "denied"
    assert acb.state == AgentState.FAILED
    assert rec.order == []  # run() never executed
    assert acb.attempts == 1  # no retry on a security decision
    assert len(events(os_, "permission_denied")) == 1
    assert events(os_, "retried") == []


def test_permission_denied_on_syscall() -> None:
    os_ = AgentOS(confirm="auto-approve")
    mailer = os_.register(MailerAgent(permissions=()))  # did not declare send_email
    acb = os_.run_task(mailer, "bob@example.com")
    assert acb.outcome == "denied"
    assert mailer.sent == []


def test_confirmation_rejected() -> None:
    os_ = AgentOS(confirm="auto-deny")
    mailer = os_.register(MailerAgent())
    acb = os_.run_task(mailer, "bob@example.com")
    assert acb.outcome == "rejected"
    assert mailer.sent == []
    assert len(events(os_, "permission_rejected")) == 1


def test_confirmation_approved() -> None:
    asked: list[tuple[str, str, str]] = []

    def confirm(agent: str, action: str, detail: str) -> bool:
        asked.append((agent, action, detail))
        return True

    os_ = AgentOS(confirm=confirm)
    mailer = os_.register(MailerAgent())
    acb = os_.run_task(mailer, "bob@example.com")
    assert acb.outcome == "completed"
    assert mailer.sent == ["bob@example.com"]
    assert asked == [("Mailer", "send_email", "to=bob@example.com")]
    assert len(events(os_, "permission_confirmed")) == 1


def test_non_sensitive_permission_allowed() -> None:
    os_ = AgentOS()
    rec = os_.register(RecorderAgent(permissions=["web_search"]))
    acb = os_.run_task(rec, "q", required_permissions=["web_search"])
    assert acb.outcome == "completed"
    assert len(events(os_, "permission_allowed")) == 1


# -------------------------------------------------------------------- recovery
def test_retry_then_success() -> None:
    os_ = AgentOS(max_retries=2)
    flaky = os_.register(FlakyAgent("Flaky", fail_times=2))
    acb = os_.run_task(flaky, None)
    assert acb.outcome == "completed"
    assert acb.result == "ok"
    assert acb.attempts == 3
    assert len(events(os_, "retried")) == 2
    assert events(os_, "reassigned") == []


def test_retry_then_reassign() -> None:
    os_ = AgentOS(max_retries=2)
    primary = os_.register(AlwaysFailAgent("Primary"))
    backup = os_.register(FlakyAgent("Backup", fail_times=0), backup_for=primary)
    acb = os_.run_task(primary, None)
    assert acb.outcome == "completed"
    assert primary.calls == 3  # 1 try + 2 retries
    assert backup.calls == 1
    assert acb.agent_id == backup.id
    assert acb.agents_tried == [primary.id, backup.id]
    assert len(events(os_, "reassigned")) == 1


def test_retry_then_escalate() -> None:
    os_ = AgentOS(max_retries=1)
    primary = os_.register(AlwaysFailAgent("Primary"))
    backup = os_.register(AlwaysFailAgent("Backup"), backup_for=primary)
    acb = os_.run_task(primary, None)
    assert acb.outcome == "escalated"
    assert acb.state == AgentState.FAILED
    assert primary.calls == 2 and backup.calls == 2
    assert len(events(os_, "escalated")) == 1


def test_escalate_without_backup() -> None:
    os_ = AgentOS(max_retries=2)
    bad = os_.register(AlwaysFailAgent("Lonely"))
    acb = os_.run_task(bad, None)
    assert acb.outcome == "escalated"
    assert acb.attempts == 3


# ----------------------------------------------------------------------- audit
def test_audit_table_and_json_export(tmp_path) -> None:
    os_ = AgentOS(max_retries=1)
    primary = os_.register(AlwaysFailAgent("Primary"))
    os_.register(FlakyAgent("Backup", fail_times=0), backup_for=primary)
    os_.run_task(primary, None)

    table = os_.format_agents_used()
    assert "Primary -> Backup" in table and "completed" in table

    path = os_.export_log(tmp_path / "log.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    kinds = [e["event"] for e in data]
    for expected in ["submitted", "started", "failed", "retried", "reassigned", "completed"]:
        assert expected in kinds
    assert all("timestamp" in e for e in data)
