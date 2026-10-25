"""Workflow runner: Research -> DocGen -> FileManager -> Email, all through AgentOS.

Run from the agentos/ folder:
    python -m demo.workflow                       # normal run
    python -m demo.workflow --fail reassign       # research-01 is down; dispatcher reassigns
    python -m demo.workflow --confirm interactive # you approve the email
    python -m demo.workflow --llm mock            # explicit mock LLM (no Ollama needed)
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Union

from agents.docgen import slugify
from demo.fleet import STEP_CAPABILITIES, register_demo_fleet
from runtime import AgentOS, PriorityPolicy
from runtime.acb import AgentControlBlock
from runtime.permissions import ConfirmCallback
from runtime.sandbox import DEFAULT_OUTPUT_DIR

if TYPE_CHECKING:
    from llm import LLMClient


@dataclass
class WorkflowStep:
    """One node in the workflow graph.

    Set `capabilities` to let the dispatcher pick a compatible instance
    (agent_id ""), or set `agent_id` to pin the step to one instance.
    """

    name: str
    agent_id: str
    make_input: Callable[[dict[str, Any]], Any]  # gets results of earlier steps by name
    depends_on: tuple[str, ...] = ()
    priority: int = 5
    capabilities: tuple[str, ...] = ()


@dataclass
class WorkflowRun:
    """Outcome of a workflow: one ACB per step that ran, plus skipped steps."""

    acbs: dict[str, AgentControlBlock] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return not self.skipped and all(a.outcome == "completed" for a in self.acbs.values())


def order_steps(steps: list[WorkflowStep]) -> list[WorkflowStep]:
    """Topological sort (Kahn's algorithm); keeps declaration order among ready steps."""
    by_name = {s.name: s for s in steps}
    for s in steps:
        missing = [d for d in s.depends_on if d not in by_name]
        if missing:
            raise ValueError(f"Step {s.name!r} depends on unknown step(s) {missing}")
    indegree = {s.name: len(s.depends_on) for s in steps}
    ready = deque(s for s in steps if indegree[s.name] == 0)
    ordered: list[WorkflowStep] = []
    while ready:
        step = ready.popleft()
        ordered.append(step)
        for s in steps:
            if step.name in s.depends_on:
                indegree[s.name] -= 1
                if indegree[s.name] == 0:
                    ready.append(s)
    if len(ordered) != len(steps):
        raise ValueError("Workflow has a dependency cycle")
    return ordered


def run_workflow(aos: AgentOS, steps: list[WorkflowStep], verbose: bool = True) -> WorkflowRun:
    """Run steps in dependency order, passing results forward. A step whose
    dependency did not complete is skipped (not submitted)."""
    run = WorkflowRun()
    results: dict[str, Any] = {}
    ordered = order_steps(steps)
    for i, step in enumerate(ordered, 1):
        blocked = [d for d in step.depends_on if d not in results]
        target = aos.agent(step.agent_id).name if step.agent_id else f"dispatch by capability [{', '.join(step.capabilities)}]"
        if blocked:
            run.skipped.append(step.name)
            if verbose:
                print(f"\n[{i}/{len(ordered)}] {step.name}: SKIPPED (waiting on failed {blocked})")
            continue
        if verbose:
            print(f"\n[{i}/{len(ordered)}] {step.name} -> {target}")
        acb = aos.run_task(step.agent_id or None, step.make_input(results), priority=step.priority,
                           required_capabilities=step.capabilities, label=step.name)
        run.acbs[step.name] = acb
        if acb.outcome == "completed":
            results[step.name] = acb.result
        if verbose:
            ran_on = f" on {aos.agent(acb.agent_id).name}" if acb.agent_id in aos.registry else ""
            print(f"      => {acb.outcome.upper()}{ran_on}" + (f": {acb.error}" if acb.error and acb.outcome != "completed" else ""))
    return run


def build_report_workflow(
    aos: AgentOS,
    topic: str = "AI Agents in Healthcare",
    recipient: str = "supervisor@university.example",
    fail: str = "none",
    research_delay: float = 0.0,
    llm: Optional["LLMClient"] = None,
) -> list[WorkflowStep]:
    """Register the demo fleet (9 instances) and return capability-based steps.

    Every step is dispatched by capability, so the dispatcher picks the
    instance. In a fresh registry it picks research-01 first, so the forced
    failure is injected there:
    fail: "none" | "retry" (research-01 fails once) | "reassign" (research-01
    is down, so AgentOS retries it, then the dispatcher reassigns the task to
    another compatible research instance).
    """
    fleet = register_demo_fleet(aos, research_delay=research_delay, llm=llm)
    fleet["research"][0].fail_first = {"none": 0, "retry": 1, "reassign": 10**6}[fail]

    slug = slugify(topic)
    caps = STEP_CAPABILITIES
    return [
        WorkflowStep("research", "", lambda r: {"topic": topic}, capabilities=caps["research"]),
        WorkflowStep(
            "docgen", "",
            lambda r: {"research": r["research"], "filename": f"drafts/{slug}.pptx"},
            depends_on=("research",), capabilities=caps["docgen"],
        ),
        WorkflowStep(
            "file", "",
            lambda r: {"src": r["docgen"]["path"], "dest": f"reports/{slug}.pptx", "mode": "move"},
            depends_on=("docgen",), capabilities=caps["file"],
        ),
        WorkflowStep(
            "email", "",
            lambda r: {
                "to": recipient,
                "subject": f"Report: {topic}",
                "body": r["research"]["summary"],
                "attachment": r["file"]["path"],
            },
            depends_on=("research", "file"), capabilities=caps["email"],
        ),
    ]


def create_demo_os(
    fail: str = "none",
    confirm: Union[str, ConfirmCallback] = "auto-approve",
    topic: str = "AI Agents in Healthcare",
    sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    research_delay: float = 0.0,
    echo: bool = False,
    llm: Optional["LLMClient"] = None,
) -> tuple[AgentOS, list[WorkflowStep]]:
    """Build a fresh AgentOS with the demo agents registered; return it and the steps.

    `llm` defaults to a real local Ollama client; pass MockLLMClient() explicitly
    for tests or offline demos.
    """
    aos = AgentOS(policy=PriorityPolicy(), confirm=confirm, max_retries=2,
                  echo=echo, sandbox_root=sandbox_root)
    steps = build_report_workflow(aos, topic=topic, fail=fail, research_delay=research_delay, llm=llm)
    return aos, steps


def research_client(aos: AgentOS) -> Any:
    """The LLM client used by the registered research instances (None if there are none)."""
    research = [a for a in aos.agents if "web_research" in a.capabilities and hasattr(a, "llm")]
    return research[0].llm if research else None


def run_demo(
    fail: str = "none",
    confirm: Union[str, ConfirmCallback] = "auto-approve",
    topic: str = "AI Agents in Healthcare",
    sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    research_delay: float = 0.0,
    verbose: bool = True,
    llm: Optional["LLMClient"] = None,
) -> tuple[AgentOS, WorkflowRun]:
    """Build a fresh AgentOS, run the 4-agent workflow and print a summary."""
    aos, steps = create_demo_os(fail, confirm, topic, sandbox_root, research_delay, echo=verbose, llm=llm)
    if verbose:
        print(f"=== AgentOS workflow: Research -> DocGen -> FileManager -> Email (fail={fail}) ===")
        client = research_client(aos)
        ok, message = client.health()
        print(f"Research backend: {client.describe()}")
        if client.backend == "mock":
            print("  NOTE: mock LLM selected explicitly - research output is placeholder text, not real analysis.")
        elif not ok:
            print(f"  WARNING: {message}\n  Research will fail and dependent steps will be skipped.")
        else:
            print("  Local LLM analysis (no web access). A research answer can take a minute or two.")
    run = run_workflow(aos, steps, verbose=verbose)
    if verbose:
        print("\n=== Agents used ===")
        aos.print_agents_used()
        print(f"\nWorkflow {'SUCCEEDED' if run.succeeded else 'DID NOT COMPLETE'}")
        research = run.acbs.get("research")
        if research is not None and research.outcome == "completed":
            r = research.result
            print(f"Research: {len(r['key_points'])} findings from {r['backend']} model {r['model']} "
                  f"({r['duration_s']} s) on {research.agent_id}")
        if "file" in run.acbs and run.acbs["file"].outcome == "completed":
            print(f"Report:  {aos.sandbox.resolve(run.acbs['file'].result['path'])}")
        if "email" in run.acbs and run.acbs["email"].outcome == "completed":
            print(f"Outbox:  {run.acbs['email'].result['outbox']}")
        print(f"Audit:   {aos.export_log()}")
    return aos, run


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Run the AgentOS 4-agent workflow demo.")
    parser.add_argument("--fail", choices=["none", "retry", "reassign"], default="none")
    parser.add_argument("--confirm", choices=["interactive", "auto-approve", "auto-deny"], default="auto-approve")
    parser.add_argument("--topic", default="AI Agents in Healthcare")
    parser.add_argument("--llm", choices=["ollama", "mock"], default="ollama",
                        help="ollama (default): real local model; mock: explicit placeholder output")
    args = parser.parse_args(argv)
    llm = None
    if args.llm == "mock":
        from llm import MockLLMClient

        llm = MockLLMClient()
    run_demo(fail=args.fail, confirm=args.confirm, topic=args.topic, llm=llm)


if __name__ == "__main__":
    main()
