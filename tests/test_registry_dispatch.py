"""Stage 6 tests: agent registry, capability-based dispatcher, and their
separation from the scheduler inside the kernel."""
from __future__ import annotations

import ast
import inspect
from typing import Any

import pytest

import runtime.dispatcher
import runtime.scheduler
from demo.fleet import DEMO_FLEET, register_demo_fleet
from runtime import Agent, AgentOS, AgingPolicy, FIFOPolicy, PermissionManager, PriorityPolicy
from runtime.acb import AgentControlBlock, now
from runtime.dispatcher import Dispatcher, NoEligibleAgentError
from runtime.recovery import RecoveryAction, RecoveryEngine
from runtime.registry import AgentNotFoundError, AgentRegistry, DuplicateAgentError
from runtime.scheduler import Scheduler


# ---------------------------------------------------------------- helpers
class Worker(Agent):
    """Records which task inputs it ran; optionally always fails."""

    def __init__(self, agent_id: str, capabilities=("work",), permissions=(), broken: bool = False) -> None:
        super().__init__(agent_id, permissions=permissions, agent_id=agent_id, capabilities=capabilities)
        self.broken = broken
        self.ran: list[Any] = []

    def run(self, task_input: Any) -> Any:
        self.ran.append(task_input)
        if self.broken:
            raise RuntimeError(f"{self.id} is broken")
        return f"{self.id}:{task_input}"


class Mailer(Agent):
    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id, permissions=["send_email"], agent_id=agent_id, capabilities=["email_send"])
        self.sent: list[Any] = []

    def run(self, task_input: Any) -> str:
        self.request_permission("send_email", f"to={task_input}")
        self.sent.append(task_input)
        return "sent"


def make_dispatcher(*agents: Agent) -> tuple[AgentRegistry, PermissionManager, Dispatcher]:
    registry, perms = AgentRegistry(), PermissionManager()
    for a in agents:
        registry.register(a)
        perms.grant(a.id, *a.permissions)
    return registry, perms, Dispatcher(registry, perms)


# ----------------------------------------------------------------- registry
def test_registry_register_lookup_list() -> None:
    reg = AgentRegistry()
    a, b = Worker("w-01"), Worker("w-02")
    reg.register(a)
    reg.register(b)
    assert reg.get("w-01") is a
    assert reg.list() == [a, b]  # registration order
    assert len(reg) == 2 and "w-02" in reg
    assert reg.agent_type("w-01") == "Worker"


def test_registry_rejects_duplicate_ids() -> None:
    reg = AgentRegistry()
    reg.register(Worker("w-01"))
    with pytest.raises(DuplicateAgentError, match="already registered"):
        reg.register(Worker("w-01"))
    assert len(reg) == 1


def test_registry_find_by_capabilities() -> None:
    reg = AgentRegistry()
    r = reg.register(Worker("r", capabilities=["web_research"]))
    f = reg.register(Worker("f", capabilities=["file_read", "file_write"]))
    assert reg.find(["web_research"]) == [r]
    assert reg.find(["file_read", "file_write"]) == [f]
    assert reg.find(["file_read", "web_research"]) == []  # must have ALL
    assert reg.find([]) == [r, f]


def test_registry_empty() -> None:
    reg = AgentRegistry()
    assert reg.list() == [] and len(reg) == 0
    assert reg.find(["anything"]) == []
    with pytest.raises(AgentNotFoundError):
        reg.get("nobody")


def test_registry_unregister() -> None:
    reg = AgentRegistry()
    a = reg.register(Worker("w-01"))
    assert reg.unregister("w-01") is a
    assert "w-01" not in reg
    with pytest.raises(AgentNotFoundError):
        reg.unregister("w-01")


def test_registry_tracks_availability_and_dispatches() -> None:
    reg = AgentRegistry()
    reg.register(Worker("w-01"))
    assert reg.is_available("w-01")
    reg.set_available("w-01", False)
    assert not reg.is_available("w-01")
    reg.mark_dispatched("w-01")
    assert reg.record("w-01").dispatch_count == 1


def test_demo_fleet_registers_nine_real_instances() -> None:
    aos = AgentOS()
    fleet = register_demo_fleet(aos, research_delay=0)
    assert {k: len(v) for k, v in fleet.items()} == DEMO_FLEET == {
        "research": 3, "docgen": 2, "filemanager": 2, "email": 2}
    assert [a.id for a in aos.agents] == [
        "research-01", "research-02", "research-03", "docgen-01", "docgen-02",
        "filemanager-01", "filemanager-02", "email-01", "email-02"]
    assert {type(a).__name__ for a in fleet["research"]} == {"ResearchAgent"}


# --------------------------------------------------------------- dispatcher
def test_dispatcher_selects_agent_with_all_capabilities() -> None:
    _, _, d = make_dispatcher(Worker("a", capabilities=["x"]), Worker("b", capabilities=["x", "y"]))
    assert d.select(["x", "y"]).agent.id == "b"


def test_dispatcher_rejects_missing_capabilities() -> None:
    _, _, d = make_dispatcher(Worker("a", capabilities=["x"]))
    report = d.explain(["y"])
    assert [(e.agent_id, e.eligible, e.reason) for e in report] == [("a", False, "missing capability y")]
    with pytest.raises(NoEligibleAgentError, match="none of the 1 registered instances"):
        d.select(["y"])


def test_dispatcher_rotates_least_recently_dispatched() -> None:
    _, _, d = make_dispatcher(Worker("a"), Worker("b"), Worker("c"))
    picks = [d.select(["work"]).agent.id for _ in range(6)]
    assert picks == ["a", "b", "c", "a", "b", "c"]  # not always the first one


def test_dispatcher_is_deterministic() -> None:
    def picks() -> list[str]:
        _, _, d = make_dispatcher(Worker("a"), Worker("b"))
        return [d.select(["work"]).agent.id for _ in range(5)]

    assert picks() == picks() == ["a", "b", "a", "b", "a"]


def test_dispatcher_single_eligible_instance() -> None:
    _, _, d = make_dispatcher(Worker("a"), Worker("z", capabilities=["other"]))
    decision = d.select(["work"])
    assert decision.agent.id == "a" and decision.reason == "only eligible instance"
    assert d.select(["work"]).agent.id == "a"


def test_dispatcher_skips_unavailable_instances() -> None:
    reg, _, d = make_dispatcher(Worker("a"), Worker("b"))
    reg.set_available("a", False)
    assert {d.select(["work"]).agent.id for _ in range(3)} == {"b"}
    reg.set_available("b", False)
    with pytest.raises(NoEligibleAgentError, match="a: unavailable; b: unavailable"):
        d.select(["work"])


def test_dispatcher_empty_registry() -> None:
    _, _, d = make_dispatcher()
    with pytest.raises(NoEligibleAgentError, match="registry is empty"):
        d.select(["work"])


def test_dispatcher_honours_exclusions() -> None:
    _, _, d = make_dispatcher(Worker("a"), Worker("b"))
    assert d.select(["work"], exclude=["a"]).agent.id == "b"
    assert d.find_alternative(["work"], (), ["a", "b"]) is None


def test_capability_does_not_grant_permission() -> None:
    reg, perms, d = make_dispatcher(Worker("m", capabilities=["email_send"]))  # declares no permissions
    assert perms.granted("m") == frozenset()  # capability != grant
    with pytest.raises(NoEligibleAgentError, match="no permission grant for send_email"):
        d.select(["email_send"], required_permissions=["send_email"])
    d.select(["email_send"])
    assert perms.granted("m") == frozenset()  # dispatching granted nothing


# -------------------------------------------------- scheduler separation
def _module_imports(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
    return names


def test_scheduler_knows_nothing_about_agents_or_dispatch() -> None:
    imports = _module_imports(runtime.scheduler)
    assert not imports & {"runtime.registry", "runtime.dispatcher", "runtime.agent", "runtime.kernel"}
    # The scheduler orders ACBs purely by policy, capabilities are ignored.
    sched = Scheduler(PriorityPolicy())
    for i, caps in enumerate([("nobody_has_this",), ("x",), ()]):
        sched.submit(AgentControlBlock(f"T{i}", "", priority=3 - i, required_capabilities=caps))
    assert [sched.next().task_id for _ in range(3)] == ["T2", "T1", "T0"]


def test_dispatcher_never_sees_the_queue() -> None:
    imports = _module_imports(runtime.dispatcher)
    assert "runtime.scheduler" not in imports and "runtime.kernel" not in imports
    params = set(inspect.signature(Dispatcher.select).parameters)
    assert params == {"self", "required_capabilities", "required_permissions", "exclude"}


def _run(policy, register_extra: bool = False) -> tuple[list[str], list[str]]:
    """Returns (task execution order, instance per task in that order)."""
    aos = AgentOS(policy=policy)
    aos.register(Worker("w-01"))
    aos.register(Worker("w-02"))
    if register_extra:
        aos.register(Worker("w-03"))
    for label, prio in [("a", 5), ("b", 1), ("c", 3), ("d", 2)]:
        aos.submit(None, label, priority=prio, required_capabilities=["work"], label=label)
    aos.run_all()
    order = [e.task_id for e in aos.audit.events("scheduled")]
    by_id = {t.task_id: t for t in aos.tasks}
    return [by_id[t].label for t in order], [by_id[t].agent_id for t in order]


def test_policy_changes_order_not_matching_rules() -> None:
    fifo_order, fifo_agents = _run(FIFOPolicy())
    prio_order, prio_agents = _run(PriorityPolicy())
    assert fifo_order == ["a", "b", "c", "d"]
    assert prio_order == ["b", "d", "c", "a"]
    # Same rotation rule in both: the n-th dispatched task goes to the same instance.
    assert fifo_agents == prio_agents == ["w-01", "w-02", "w-01", "w-02"]


def test_registry_changes_instance_not_task_order() -> None:
    order2, agents2 = _run(PriorityPolicy())
    order3, agents3 = _run(PriorityPolicy(), register_extra=True)
    assert order2 == order3 == ["b", "d", "c", "a"]
    assert agents3 == ["w-01", "w-02", "w-03", "w-01"] != agents2


def test_aging_still_orders_by_effective_priority() -> None:
    aos = AgentOS(policy=AgingPolicy(rate=1.0))
    aos.register(Worker("w-01"))
    old = aos.submit(None, "old", priority=9, required_capabilities=["work"])
    aos.submit(None, "new", priority=1, required_capabilities=["work"])
    old.submit_time = now() - 20
    aos.run_all()
    assert aos.agent("w-01").ran == ["old", "new"]


# ------------------------------------------------------------ kernel integration
def test_kernel_dispatches_and_audits_selected_instance() -> None:
    aos = AgentOS(policy=PriorityPolicy())
    aos.register(Worker("w-01", permissions=["read_file"]))
    acb = aos.run_task(None, "job", required_capabilities=["work"],
                       required_permissions=["read_file"], label="job")
    assert acb.outcome == "completed" and acb.result == "w-01:job"
    sched = aos.audit.events("scheduled")[0]
    disp = aos.audit.events("dispatched")[0]
    assert sched.details["policy"] == "Priority" and sched.agent_id is None  # not bound yet
    assert (disp.agent_id, disp.agent_type) == ("w-01", "Worker")
    assert aos.audit.events("permission_allowed")[0].agent_id == "w-01"
    kinds = [e.event for e in aos.audit.events(task_id=acb.task_id)]
    assert kinds.index("scheduled") < kinds.index("dispatched") < kinds.index("started")


def test_kernel_handles_no_compatible_agent() -> None:
    aos = AgentOS()
    w = aos.register(Worker("w-01"))
    acb = aos.run_task(None, "x", required_capabilities=["translation"])
    assert acb.outcome == "no_agent" and acb.attempts == 0
    assert w.ran == []  # never executed on an incompatible agent
    assert aos.audit.count("dispatch_failed") == 1
    assert aos.format_agents_used()  # report still renders with no instance


def test_pinned_task_rejects_incompatible_agent() -> None:
    aos = AgentOS()
    aos.register(Worker("w-01"))
    with pytest.raises(ValueError, match="lacks required capabilities"):
        aos.submit("w-01", "x", required_capabilities=["translation"])
    with pytest.raises(ValueError, match="needs an agent or required_capabilities"):
        aos.submit(None, "x")


def test_dispatch_respects_human_confirmation() -> None:
    for confirm, outcome in [("auto-deny", "rejected"), ("auto-approve", "completed")]:
        aos = AgentOS(confirm=confirm)
        m = aos.register(Mailer("email-01"))
        acb = aos.run_task(None, "bob", required_capabilities=["email_send"])
        assert acb.outcome == outcome
        assert m.sent == ([] if outcome == "rejected" else ["bob"])


def test_revoked_permission_still_denied_at_runtime() -> None:
    aos = AgentOS(confirm="auto-approve")
    m = aos.register(Mailer("email-01"))
    aos.permissions.revoke("email-01", "send_email")  # capability stays, grant is gone
    acb = aos.run_task(None, "bob", required_capabilities=["email_send"])
    assert acb.outcome == "denied" and m.sent == []


def test_unregister_removes_instance_from_dispatch() -> None:
    aos = AgentOS()
    aos.register(Worker("w-01", permissions=["read_file"]))
    aos.register(Worker("w-02"))
    aos.unregister("w-01")
    assert aos.permissions.granted("w-01") == frozenset()
    acb = aos.run_task(None, "x", required_capabilities=["work"])
    assert acb.agent_id == "w-02"


# ------------------------------------------------------------------ recovery
def test_retry_then_success_on_same_instance() -> None:
    aos = AgentOS(max_retries=2)

    class Flaky(Worker):
        def run(self, task_input):
            if len(self.ran) < 2:
                self.ran.append(task_input)
                raise RuntimeError("transient")
            return super().run(task_input)

    aos.register(Flaky("w-01"))
    aos.register(Worker("w-02"))
    acb = aos.run_task(None, "x", required_capabilities=["work"])
    assert acb.outcome == "completed" and acb.agents_tried == ["w-01"] and acb.attempts == 3


def test_failed_task_reassigned_only_to_compatible_authorized_instance() -> None:
    aos = AgentOS(max_retries=1)
    aos.register(Worker("w-01", permissions=["read_file"], broken=True))
    aos.register(Worker("other", capabilities=["unrelated"], permissions=["read_file"]))  # incompatible
    aos.register(Worker("w-02"))  # compatible but NOT authorized for read_file
    good = aos.register(Worker("w-03", permissions=["read_file"]))
    acb = aos.run_task(None, "x", required_capabilities=["work"], required_permissions=["read_file"])
    assert acb.outcome == "completed"
    assert acb.agents_tried == ["w-01", "w-03"] and good.ran == ["x"]
    assert aos.audit.events("reassigned")[0].details["via"] == "dispatcher"


def test_no_compatible_backup_escalates() -> None:
    aos = AgentOS(max_retries=2)
    aos.register(Worker("w-01", broken=True))
    bystander = aos.register(Worker("other", capabilities=["unrelated"]))
    acb = aos.run_task(None, "x", required_capabilities=["work"])
    assert acb.outcome == "escalated" and acb.attempts == 3
    assert aos.audit.count("reassigned") == 0 and bystander.ran == []


def test_reassignment_stops_when_all_compatible_instances_fail() -> None:
    aos = AgentOS(max_retries=0)
    for i in range(3):
        aos.register(Worker(f"w-0{i + 1}", broken=True))
    acb = aos.run_task(None, "x", required_capabilities=["work"])
    assert acb.outcome == "escalated"
    assert acb.agents_tried == ["w-01", "w-02", "w-03"]  # each tried once, no loop


def test_explicit_backup_must_be_eligible() -> None:
    aos = AgentOS(max_retries=0)
    primary = aos.register(Worker("w-01", broken=True))
    wrong = aos.register(Worker("bad-backup", capabilities=["unrelated"]), backup_for=primary)
    right = aos.register(Worker("w-02"))
    acb = aos.run_task(None, "x", required_capabilities=["work"])
    assert acb.agents_tried == ["w-01", "w-02"] and wrong.ran == [] and right.ran == ["x"]


def test_pinned_task_without_capabilities_is_never_guessed_onto_other_agents() -> None:
    aos = AgentOS(max_retries=0)
    aos.register(Worker("w-01", broken=True))
    other = aos.register(Worker("w-02"))
    acb = aos.run_task("w-01", "x")  # pinned, no capabilities declared, no backup link
    assert acb.outcome == "escalated" and other.ran == []


def test_recovery_engine_decision_order() -> None:
    engine = RecoveryEngine(max_retries=0)
    engine.register_backup("a", "b")
    acb = AgentControlBlock("T1", "a", attempt_log=["a"])
    assert engine.decide(acb).action is RecoveryAction.REASSIGN  # backup, default eligible
    d = engine.decide(acb, is_eligible=lambda aid: False, find_alternative=lambda: "c")
    assert (d.action, d.agent_id) == (RecoveryAction.REASSIGN, "c")
    d = engine.decide(acb, is_eligible=lambda aid: False, find_alternative=lambda: None)
    assert d.action is RecoveryAction.ESCALATE


# --------------------------------------------------------------- utilization
def test_instance_usage_counts() -> None:
    aos = AgentOS(max_retries=0)
    aos.register(Worker("w-01", broken=True))
    aos.register(Worker("w-02"))
    for _ in range(3):
        aos.submit(None, "x", required_capabilities=["work"])
    aos.run_all()
    usage = {u["instance"]: u for u in aos.instance_usage()}
    # Each task is first dispatched to w-01 (least recently dispatched, since
    # w-02 was just used as the replacement), fails, then moves to w-02.
    assert sum(u["completed"] for u in usage.values()) == 3
    assert usage["w-01"]["completed"] == 0
    assert usage["w-01"]["attempts"] == usage["w-01"]["dispatched"]
    assert usage["w-02"]["attempts"] == 3
