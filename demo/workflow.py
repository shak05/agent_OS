"""Workflow runner: Research -> DocGen -> FileManager -> Email, all through AgentOS.

Run from the agentos/ folder:
    python -m demo.workflow                       # normal run
    python -m demo.workflow --fail reassign       # primary researcher is down
    python -m demo.workflow --confirm interactive # you approve the email
"""
from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agents import DocGenAgent, EmailAgent, FileManagerAgent, MockMailTransport, ResearchAgent
from agents.docgen import slugify
from runtime import AgentOS, PriorityPolicy
from runtime.acb import AgentControlBlock
from runtime.permissions import ConfirmCallback
from runtime.sandbox import DEFAULT_OUTPUT_DIR


@dataclass
class WorkflowStep:
    """One node in the workflow graph."""

    name: str
    agent_id: str
    make_input: Callable[[dict[str, Any]], Any]  # gets results of earlier steps by name
    depends_on: tuple[str, ...] = ()
    priority: int = 5


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
        agent_name = aos.agent(step.agent_id).name
        if blocked:
            run.skipped.append(step.name)
            if verbose:
                print(f"\n[{i}/{len(ordered)}] {step.name}: SKIPPED (waiting on failed {blocked})")
            continue
        if verbose:
            print(f"\n[{i}/{len(ordered)}] {step.name} -> {agent_name}")
        acb = aos.run_task(step.agent_id, step.make_input(results), priority=step.priority)
        run.acbs[step.name] = acb
        if acb.outcome == "completed":
            results[step.name] = acb.result
        if verbose:
            print(f"      => {acb.outcome.upper()}" + (f": {acb.error}" if acb.error and acb.outcome != "completed" else ""))
    return run


def build_report_workflow(
    aos: AgentOS,
    topic: str = "AI Agents in Healthcare",
    recipient: str = "supervisor@university.example",
    fail: str = "none",
    research_delay: float = 0.3,
) -> list[WorkflowStep]:
    """Register the four demo agents (+ a backup researcher) and return the steps.

    fail: "none" | "retry" (primary fails once) | "reassign" (primary is down,
    so AgentOS retries, then reassigns to the backup researcher).
    """
    fail_first = {"none": 0, "retry": 1, "reassign": 10**6}[fail]
    research = aos.register(ResearchAgent("ResearchAgent", delay=research_delay, fail_first=fail_first))
    aos.register(
        ResearchAgent("BackupResearchAgent", delay=research_delay, agent_id="research-backup"),
        backup_for=research,
    )
    docgen = aos.register(DocGenAgent())
    files = aos.register(FileManagerAgent())
    email = aos.register(EmailAgent(transport=MockMailTransport(aos.sandbox.root / "sent_emails.json")))

    slug = slugify(topic)
    return [
        WorkflowStep("research", research.id, lambda r: {"topic": topic}),
        WorkflowStep(
            "docgen", docgen.id,
            lambda r: {"research": r["research"], "filename": f"drafts/{slug}.pptx"},
            depends_on=("research",),
        ),
        WorkflowStep(
            "file", files.id,
            lambda r: {"src": r["docgen"]["path"], "dest": f"reports/{slug}.pptx", "mode": "move"},
            depends_on=("docgen",),
        ),
        WorkflowStep(
            "email", email.id,
            lambda r: {
                "to": recipient,
                "subject": f"Report: {topic}",
                "body": r["research"]["summary"],
                "attachment": r["file"]["path"],
            },
            depends_on=("research", "file"),
        ),
    ]


def create_demo_os(
    fail: str = "none",
    confirm: Union[str, ConfirmCallback] = "auto-approve",
    topic: str = "AI Agents in Healthcare",
    sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    research_delay: float = 0.3,
    echo: bool = False,
) -> tuple[AgentOS, list[WorkflowStep]]:
    """Build a fresh AgentOS with the demo agents registered; return it and the steps."""
    aos = AgentOS(policy=PriorityPolicy(), confirm=confirm, max_retries=2,
                  echo=echo, sandbox_root=sandbox_root)
    steps = build_report_workflow(aos, topic=topic, fail=fail, research_delay=research_delay)
    return aos, steps


def run_demo(
    fail: str = "none",
    confirm: Union[str, ConfirmCallback] = "auto-approve",
    topic: str = "AI Agents in Healthcare",
    sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    research_delay: float = 0.3,
    verbose: bool = True,
) -> tuple[AgentOS, WorkflowRun]:
    """Build a fresh AgentOS, run the 4-agent workflow and print a summary."""
    aos, steps = create_demo_os(fail, confirm, topic, sandbox_root, research_delay, echo=verbose)
    if verbose:
        print(f"=== AgentOS workflow: Research -> DocGen -> FileManager -> Email (fail={fail}) ===")
    run = run_workflow(aos, steps, verbose=verbose)
    if verbose:
        print("\n=== Agents used ===")
        aos.print_agents_used()
        print(f"\nWorkflow {'SUCCEEDED' if run.succeeded else 'DID NOT COMPLETE'}")
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
    args = parser.parse_args(argv)
    run_demo(fail=args.fail, confirm=args.confirm, topic=args.topic)


if __name__ == "__main__":
    main()
