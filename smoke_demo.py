"""Smoke demo: five dummy agents exercising every runtime path.

Run from the agentos/ folder:  python smoke_demo.py
"""
from __future__ import annotations

import time
from typing import Any

from runtime import Agent, AgentOS, PriorityPolicy

WORK_TIME = 0.05  # simulated work per attempt, so durations are visible


class SuccessAgent(Agent):
    """Always succeeds."""

    def run(self, task_input: Any) -> str:
        time.sleep(WORK_TIME)
        return f"processed {task_input}"


class FlakyAgent(Agent):
    """Fails the first `fail_times` calls, then succeeds."""

    def __init__(self, name: str, fail_times: int) -> None:
        super().__init__(name)
        self.fail_times = fail_times
        self.calls = 0

    def run(self, task_input: Any) -> str:
        self.calls += 1
        time.sleep(WORK_TIME)
        if self.calls <= self.fail_times:
            raise RuntimeError(f"transient error #{self.calls}")
        return "recovered"


class BrokenAgent(Agent):
    """Always fails."""

    def run(self, task_input: Any) -> str:
        time.sleep(WORK_TIME)
        raise RuntimeError("service unavailable")


def main() -> None:
    aos = AgentOS(policy=PriorityPolicy(), confirm="auto-deny", max_retries=2)

    ok = aos.register(SuccessAgent("SuccessAgent"))
    flaky = aos.register(FlakyAgent("RetryAgent", fail_times=1))
    primary = aos.register(BrokenAgent("ReassignAgent"))
    aos.register(SuccessAgent("BackupAgent"), backup_for=primary)
    doomed = aos.register(BrokenAgent("EscalateAgent"))
    aos.register(BrokenAgent("EscalateBackup"), backup_for=doomed)
    no_perm = aos.register(SuccessAgent("DeniedAgent", permissions=["read_file"]))

    # Submitted out of priority order on purpose; lower number runs first.
    aos.submit(ok, "report.txt", priority=3)
    aos.submit(flaky, "fetch data", priority=1)
    aos.submit(primary, "summarise", priority=2)
    aos.submit(doomed, "call API", priority=5)
    aos.submit(no_perm, "old.log", priority=4, required_permissions=["delete_file"])

    aos.run_all()

    print("=== Audit log ===")
    aos.audit.print_log()
    print("\n=== Agents used ===")
    aos.print_agents_used()
    print(f"\nAudit log exported to {aos.export_log(aos.sandbox.root / 'smoke_audit_log.json')}")


if __name__ == "__main__":
    main()
