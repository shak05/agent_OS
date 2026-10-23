"""Background jobs for the dashboard.

Workflows and benchmarks run on background threads so the UI stays
responsive and can answer approval requests while an agent is paused.
All real work is delegated to the existing runtime and demo modules; this
file only starts jobs, guards against duplicates and exposes their state.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

from dashboard.approval import ApprovalBroker
from demo.benchmark import PolicyResult, run_benchmark, save_chart, save_results
from demo.workflow import WorkflowRun, WorkflowStep, create_demo_os, run_workflow
from runtime.acb import AgentControlBlock
from runtime.kernel import AgentOS
from runtime.sandbox import DEFAULT_OUTPUT_DIR

WORKFLOW_MODES = {
    "none": "Normal run",
    "retry": "Forced failure: primary fails once (retry)",
    "reassign": "Forced failure: primary is down (retry -> reassign)",
}


class JobAlreadyRunning(RuntimeError):
    """A job is already running; refuse to start another."""


class Job:
    """Base class: runs `_work()` once on a daemon thread and records the outcome."""

    kind = "job"

    def __init__(self) -> None:
        self.status = "pending"  # pending | running | finished | failed
        self.error: Optional[str] = None
        self.started_at: Optional[float] = None
        self.finished_at: Optional[float] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "Job":
        self.status = "running"
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, name=f"agentos-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        try:
            self._work()
            self.status = "finished"
        except Exception as exc:  # surfaced in the UI, never swallowed
            self.error = f"{type(exc).__name__}: {exc}"
            self.status = "failed"
        finally:
            self.finished_at = time.time()

    def _work(self) -> None:
        raise NotImplementedError

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until finished (used by tests). Returns True if done."""
        if self._thread is not None:
            self._thread.join(timeout)
        return not self.running


class WorkflowJob(Job):
    """The 4-agent workflow, with sensitive actions confirmed via the UI."""

    kind = "workflow"

    def __init__(
        self,
        mode: str = "none",
        topic: str = "AI Agents in Healthcare",
        sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
        research_delay: float = 0.3,
        approval_timeout: float = 300.0,
    ) -> None:
        super().__init__()
        if mode not in WORKFLOW_MODES:
            raise ValueError(f"Unknown workflow mode {mode!r}")
        self.mode = mode
        self.topic = topic
        self.broker = ApprovalBroker(timeout=approval_timeout)
        # Built up front so the UI can show agents and steps immediately.
        self.aos: AgentOS
        self.steps: list[WorkflowStep]
        self.aos, self.steps = create_demo_os(
            fail=mode, confirm=self.broker, topic=topic,
            sandbox_root=sandbox_root, research_delay=research_delay,
        )
        self.run_result: Optional[WorkflowRun] = None
        self.audit_path: Optional[Path] = None

    def _work(self) -> None:
        try:
            self.run_result = run_workflow(self.aos, self.steps, verbose=False)
        finally:
            self.broker.cancel()
            self.audit_path = self.aos.export_log()

    def step_acb(self, step: WorkflowStep) -> Optional[AgentControlBlock]:
        """The ACB submitted for a step (matched by the agent it was submitted to)."""
        for acb in self.aos.tasks:
            if acb.original_agent_id == step.agent_id:
                return acb
        return None

    def step_status(self, step: WorkflowStep) -> str:
        """queued | running | awaiting approval | <outcome> | skipped, from real runtime state."""
        acb = self.step_acb(step)
        if acb is None:
            if self.run_result is not None and step.name in self.run_result.skipped:
                return "skipped"
            return "not started" if self.running else "skipped"
        if acb.done:
            return acb.outcome
        if acb.start_time is None:
            return "queued"
        pending = self.broker.pending()
        if pending is not None and pending.agent_name == self.aos.agent(acb.agent_id).name:
            return "awaiting approval"
        return "running"

    @property
    def succeeded(self) -> bool:
        return self.run_result is not None and self.run_result.succeeded


class BenchmarkJob(Job):
    """Runs the existing scheduler benchmark and saves metrics + chart."""

    kind = "benchmark"

    def __init__(
        self,
        tasks: int = 100,
        workers: int = 4,
        seed: int = 42,
        failure_rate: float = 0.1,
        output_dir: Union[str, Path] = DEFAULT_OUTPUT_DIR,
    ) -> None:
        super().__init__()
        self.settings: dict[str, Any] = {
            "tasks": tasks, "workers": workers, "seed": seed, "failure_rate": failure_rate,
        }
        self.output_dir = Path(output_dir)
        self.progress: list[str] = []
        self.results: Optional[list[PolicyResult]] = None

    def _work(self) -> None:
        s = self.settings
        self.results = run_benchmark(
            n=s["tasks"], workers=s["workers"], seed=s["seed"],
            failure_rate=s["failure_rate"], progress=self.progress.append,
        )
        self.progress.append("  saving metrics and chart ...")
        save_results(self.results, self.output_dir / "benchmark_results.json", **s)
        save_chart(self.results, self.output_dir / "benchmark.png")


class JobManager:
    """One job slot per process: prevents duplicate runs across reruns and tabs."""

    def __init__(
        self,
        sandbox_root: Union[str, Path] = DEFAULT_OUTPUT_DIR,
        research_delay: float = 0.3,
        approval_timeout: float = 300.0,
    ) -> None:
        self.sandbox_root = Path(sandbox_root)
        self.research_delay = research_delay
        self.approval_timeout = approval_timeout
        self.current: Optional[Job] = None
        self.last_workflow: Optional[WorkflowJob] = None
        self.last_benchmark: Optional[BenchmarkJob] = None
        self._lock = threading.Lock()

    @property
    def busy(self) -> bool:
        return self.current is not None and self.current.running

    def start_workflow(self, mode: str = "none", topic: str = "AI Agents in Healthcare") -> WorkflowJob:
        with self._lock:
            self._ensure_idle()
            job = WorkflowJob(
                mode, topic.strip() or "AI Agents in Healthcare", self.sandbox_root,
                self.research_delay, self.approval_timeout,
            )
            self.current = self.last_workflow = job
            job.start()
            return job

    def start_benchmark(self, tasks: int = 100, workers: int = 4, seed: int = 42,
                        failure_rate: float = 0.1) -> BenchmarkJob:
        with self._lock:
            self._ensure_idle()
            job = BenchmarkJob(tasks, workers, seed, failure_rate, self.sandbox_root)
            self.current = self.last_benchmark = job
            job.start()
            return job

    def _ensure_idle(self) -> None:
        if self.busy:
            raise JobAlreadyRunning(f"A {self.current.kind} is already running")
