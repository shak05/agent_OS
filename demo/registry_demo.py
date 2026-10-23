"""Registry and dispatcher demo: who picks the TASK vs who picks the AGENT.

Run from the agentos/ folder:
    python -m demo.registry_demo        (also menu option 6 in main.py)

Everything printed is read back from the runtime: the registry, the
dispatcher's eligibility report, and the audit log's `scheduled` (scheduler)
and `dispatched` (dispatcher) events.
"""
from __future__ import annotations

from typing import Any, Iterable

from demo.fleet import register_demo_fleet
from runtime import AgentOS, FIFOPolicy, PriorityPolicy, SchedulingPolicy
from runtime.acb import AgentControlBlock

DELAY = 0.02  # mock research time, keeps the demo fast

# (label, capabilities, priority) submitted in this order
TASKS: list[tuple[str, tuple[str, ...], int]] = [
    ("research A", ("web_research",), 5),
    ("research B", ("web_research",), 1),
    ("slides A", ("document_generation",), 3),
    ("research C", ("web_research",), 2),
    ("research D", ("web_research",), 5),
    ("slides B", ("document_generation",), 1),
    ("research E", ("web_research",), 4),
]


def table(rows: Iterable[dict[str, Any]], columns: list[str]) -> str:
    rows = [{c: str(r.get(c, "")) for c in columns} for r in rows]
    widths = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in columns}
    lines = ["  ".join(c.ljust(widths[c]) for c in columns), "  ".join("-" * widths[c] for c in columns)]
    lines += ["  ".join(r[c].ljust(widths[c]) for c in columns) for r in rows]
    return "\n".join("  " + line for line in lines)


def new_os(policy: SchedulingPolicy) -> AgentOS:
    aos = AgentOS(policy=policy, confirm="auto-deny", max_retries=2)
    register_demo_fleet(aos, research_delay=DELAY)
    return aos


def task_input(label: str, caps: tuple[str, ...]) -> Any:
    if "document_generation" in caps:
        research = {"topic": label, "summary": "demo", "key_points": ["demo"], "sources": ["demo"]}
        return {"research": research, "filename": f"registry_demo/{label.replace(' ', '-')}.pptx"}
    return {"topic": label}


def submit_tasks(aos: AgentOS) -> None:
    for label, caps, prio in TASKS:
        aos.submit(None, task_input(label, caps), priority=prio, required_capabilities=caps, label=label)


def timeline(aos: AgentOS) -> list[dict[str, Any]]:
    """One row per task in execution order, from the audit log."""
    by_id = {t.task_id: t for t in aos.tasks}
    dispatched = {e.task_id: e for e in aos.audit.events("dispatched")}
    rows = []
    for n, e in enumerate(aos.audit.events("scheduled"), 1):
        acb: AgentControlBlock = by_id[e.task_id]
        d = dispatched.get(e.task_id)
        rows.append({
            "#": n, "Task": acb.task_id, "Label": acb.label, "Priority": acb.priority,
            "Needs": ",".join(acb.required_capabilities),
            "Dispatched to": d.agent_id if d else "-",
            "Eligible": d.details.get("candidates", "") if d else "",
            "Why": d.details.get("reason", "") if d else acb.error or "",
        })
    return rows


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    # 1. Registry -------------------------------------------------------------
    aos = new_os(PriorityPolicy())
    section(f"1. Agent Registry ({len(aos.registry)} instances)")
    print(table([{
        "Instance": r.agent.id, "Type": r.agent_type,
        "Capabilities": ", ".join(sorted(r.agent.capabilities)),
        "Permissions granted": ", ".join(sorted(aos.permissions.granted(r.agent.id))),
        "Available": "yes" if r.available else "no",
    } for r in aos.registry.records()], ["Instance", "Type", "Capabilities", "Permissions granted", "Available"]))

    # 2. Eligibility -----------------------------------------------------------
    section("2. Dispatcher eligibility check for a task needing [document_generation]")
    aos.registry.set_available("docgen-02", False)
    for e in aos.dispatcher.explain(["document_generation"]):
        print(f"  {e.agent_id:<15} {'ELIGIBLE' if e.eligible else 'rejected':<9} {e.reason}")
    aos.registry.set_available("docgen-02", True)
    print("  (docgen-02 was taken out of service for this check, then restored)")

    # 3. Scheduler vs dispatcher ------------------------------------------------
    section("3. Scheduler picks the TASK (Priority policy), dispatcher picks the INSTANCE")
    submit_tasks(aos)
    aos.run_all()
    print(table(timeline(aos), ["#", "Task", "Label", "Priority", "Needs", "Eligible", "Dispatched to", "Why"]))
    usage = [u for u in aos.instance_usage() if u["dispatched"]]
    print("  Tasks per instance: " + ", ".join(f"{u['instance']}={u['dispatched']}" for u in usage))

    section("4. Same tasks under FIFO: task ORDER changes, matching rules do not")
    fifo = new_os(FIFOPolicy())
    submit_tasks(fifo)
    fifo.run_all()
    print(table(timeline(fifo), ["#", "Task", "Label", "Priority", "Needs", "Dispatched to"]))

    # 5. No suitable instance -------------------------------------------------
    section("5. No suitable instance")
    aos = new_os(PriorityPolicy())
    for iid in ("docgen-01", "docgen-02"):
        aos.registry.set_available(iid, False)
    a = aos.run_task(None, task_input("slides C", ("document_generation",)),
                     required_capabilities=["document_generation"], label="slides C")
    b = aos.run_task(None, {"text": "hola"}, required_capabilities=["translation"], label="translate")
    for acb in (a, b):
        print(f"  {acb.task_id} {acb.label!r}: outcome={acb.outcome}")
        print(f"      {acb.error}")

    # 6. Failover to a compatible instance -----------------------------------
    section("6. research-01 is down: retry, then reassign to a compatible instance")
    aos = new_os(PriorityPolicy())
    aos.agent("research-01").fail_first = 10**6
    acb = aos.run_task(None, {"topic": "failover"}, required_capabilities=["web_research"], label="failover")
    for e in aos.audit.events(task_id=acb.task_id):
        if e.event in ("dispatched", "failed", "retried", "reassigned", "completed", "escalated"):
            extra = {k: v for k, v in e.details.items() if k in ("attempt", "to", "via", "error")}
            print(f"  {e.event:<11} {e.agent_id or '-':<12} {extra}")
    print(f"  => outcome={acb.outcome}, instances tried: {' -> '.join(acb.agents_tried)}")

    section("7. No compatible backup left: escalate")
    aos = new_os(PriorityPolicy())
    for iid in ("research-01", "research-02"):
        aos.registry.set_available(iid, False)
    aos.agent("research-03").fail_first = 10**6
    acb = aos.run_task(None, {"topic": "doomed"}, required_capabilities=["web_research"], label="doomed")
    print(f"  only research-03 is in service and it keeps failing")
    print(f"  => outcome={acb.outcome} after {acb.attempts} attempts on {', '.join(acb.agents_tried)}; "
          f"reassigned events: {aos.audit.count('reassigned')}")


if __name__ == "__main__":
    main()
