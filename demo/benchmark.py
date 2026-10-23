"""Synthetic load test: compare FIFO, Priority and Aging scheduling.

N synthetic agents (one task each) arrive over time (seeded Poisson arrivals)
while a pool of workers drains the queue, so queue waiting is real.
Every policy sees the identical workload: same arrivals, priorities, work
times and failures.

Run from the agentos/ folder:
    python -m demo.benchmark
    python -m demo.benchmark --tasks 200 --workers 8 --failure-rate 0.2
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional, Union

from agents.synthetic import SyntheticAgent
from runtime import AgentOS, AgingPolicy, FIFOPolicy, PriorityPolicy, SchedulingPolicy
from runtime.sandbox import DEFAULT_OUTPUT_DIR

PERMISSIONS = ("compute", "web_search", "read_file", "write_file")
HIGH_PRIORITY = 2  # priority <= this counts as "high"
LOW_PRIORITY = 9  # priority >= this counts as "low"


@dataclass(frozen=True)
class TaskSpec:
    """One synthetic task, generated up front from the seed."""

    index: int
    arrival: float  # seconds after the benchmark starts
    priority: int  # 1 (highest) .. 10 (lowest)
    permission: str
    seed: int


@dataclass
class PolicyResult:
    policy: str
    total_time: float
    avg_wait: float
    avg_turnaround: float
    completion_rate: float
    retries: int
    escalations: int
    high_avg_wait: float
    low_avg_wait: float
    max_wait: float


def generate_tasks(n: int = 100, seed: int = 42, arrival_rate: float = 150.0) -> list[TaskSpec]:
    """Seeded workload: Poisson arrivals, uniform priorities 1-10."""
    rng = random.Random(seed)
    t = 0.0
    specs = []
    for i in range(n):
        specs.append(TaskSpec(i, t, rng.randint(1, 10), rng.choice(PERMISSIONS), rng.randrange(2**31)))
        t += rng.expovariate(arrival_rate)
    return specs


def run_policy(
    policy: SchedulingPolicy,
    specs: list[TaskSpec],
    workers: int = 4,
    failure_rate: float = 0.1,
    max_retries: int = 2,
) -> PolicyResult:
    """Run the whole workload under one policy and collect metrics."""
    aos = AgentOS(policy=policy, max_retries=max_retries, sandbox_root=DEFAULT_OUTPUT_DIR)
    agents = [
        aos.register(
            SyntheticAgent(
                f"Synthetic-{s.index:03d}", priority=s.priority, required_permission=s.permission,
                failure_rate=failure_rate, seed=s.seed,
            )
        )
        for s in specs
    ]
    closed = threading.Event()

    def feeder() -> None:
        t0 = time.perf_counter()
        for spec, agent in zip(specs, agents):
            delay = spec.arrival - (time.perf_counter() - t0)
            if delay > 0:
                time.sleep(delay)
            aos.submit(agent, {"task": spec.index}, priority=agent.priority,
                       required_permissions=[agent.required_permission])
        closed.set()

    start = time.perf_counter()
    feed = threading.Thread(target=feeder, daemon=True)
    feed.start()
    aos.run_all(workers=workers, input_closed=closed)
    feed.join()
    total = time.perf_counter() - start

    tasks = aos.tasks
    waits = [a.wait_time for a in tasks]
    high = [a.wait_time for a in tasks if a.priority <= HIGH_PRIORITY]
    low = [a.wait_time for a in tasks if a.priority >= LOW_PRIORITY]
    return PolicyResult(
        policy=policy.name,
        total_time=total,
        avg_wait=statistics.mean(waits),
        avg_turnaround=statistics.mean(a.turnaround for a in tasks),
        completion_rate=sum(a.outcome == "completed" for a in tasks) / len(tasks),
        retries=aos.audit.count("retried"),
        escalations=aos.audit.count("escalated"),
        high_avg_wait=statistics.mean(high) if high else 0.0,
        low_avg_wait=statistics.mean(low) if low else 0.0,
        max_wait=max(waits),
    )


def default_policies(aging_rate: float = 10.0) -> list[SchedulingPolicy]:
    return [FIFOPolicy(), PriorityPolicy(), AgingPolicy(rate=aging_rate)]


def run_benchmark(
    n: int = 100,
    workers: int = 4,
    seed: int = 42,
    failure_rate: float = 0.1,
    arrival_rate: float = 150.0,
    aging_rate: float = 10.0,
    progress: Optional[Callable[[str], None]] = print,
) -> list[PolicyResult]:
    specs = generate_tasks(n, seed, arrival_rate)
    results = []
    for policy in default_policies(aging_rate):
        if progress:
            progress(f"  running {policy.name:<8} ({n} tasks, {workers} workers) ...")
        results.append(run_policy(policy, specs, workers, failure_rate))
    return results


def format_results(results: list[PolicyResult]) -> str:
    """Comparison table (times in seconds)."""
    header = (
        f"{'Policy':<9}{'Total':>8}{'AvgWait':>9}{'AvgTurn':>9}{'Done%':>8}"
        f"{'Retries':>9}{'Escal.':>8}{'HighWait':>10}{'LowWait':>9}{'MaxWait':>9}"
    )
    lines = [header, "-" * len(header)]
    for r in results:
        lines.append(
            f"{r.policy:<9}{r.total_time:>8.2f}{r.avg_wait:>9.3f}{r.avg_turnaround:>9.3f}"
            f"{r.completion_rate * 100:>7.0f}%{r.retries:>9}{r.escalations:>8}"
            f"{r.high_avg_wait:>10.3f}{r.low_avg_wait:>9.3f}{r.max_wait:>9.3f}"
        )
    return "\n".join(lines)


# Reference palette, categorical slots 1-3 (validated all-pairs) + text/surface tokens.
_SURFACE, _TEXT, _TEXT_2, _GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
_SERIES = ("#2a78d6", "#eb6834", "#1baf7a")


def save_chart(results: list[PolicyResult], path: Union[str, Path] = DEFAULT_OUTPUT_DIR / "benchmark.png") -> Path:
    """Grouped bar chart of wait/turnaround metrics per policy."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    metrics = [
        ("Avg wait", "avg_wait"),
        (f"High-priority wait\n(priority 1-{HIGH_PRIORITY})", "high_avg_wait"),
        (f"Low-priority wait\n(priority {LOW_PRIORITY}-10)", "low_avg_wait"),
        ("Max wait", "max_wait"),
        ("Avg turnaround", "avg_turnaround"),
    ]
    fig, ax = plt.subplots(figsize=(11, 5.5), dpi=150)
    fig.patch.set_facecolor(_SURFACE)
    ax.set_facecolor(_SURFACE)

    group_w = 0.8
    bar_w = group_w / len(results)
    for i, (r, color) in enumerate(zip(results, _SERIES)):
        xs = [m + (i - (len(results) - 1) / 2) * bar_w for m in range(len(metrics))]
        values = [getattr(r, attr) for _, attr in metrics]
        bars = ax.bar(xs, values, width=bar_w, color=color, label=r.policy,
                      edgecolor=_SURFACE, linewidth=2, zorder=3)
        ax.bar_label(bars, labels=[f"{v:.2f}" for v in values], padding=2,
                     fontsize=8, color=_TEXT_2)

    ax.set_xticks(range(len(metrics)), [m for m, _ in metrics], color=_TEXT, fontsize=9)
    ax.set_ylabel("Seconds", color=_TEXT_2)
    ax.tick_params(axis="y", colors=_TEXT_2, labelsize=8)
    ax.tick_params(axis="x", length=0)
    ax.grid(axis="y", color=_GRID, linewidth=0.8, zorder=0)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(_TEXT_2)
    ax.set_title("AgentOS scheduling policies: queue wait and turnaround (lower is better)",
                 color=_TEXT, fontsize=12, loc="left", pad=14)
    ax.legend(frameon=False, labelcolor=_TEXT, ncols=len(results), loc="upper left")
    fig.tight_layout()

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor=_SURFACE)
    plt.close(fig)
    return path


RESULTS_FILE = DEFAULT_OUTPUT_DIR / "benchmark_results.json"


def save_results(
    results: list[PolicyResult], path: Union[str, Path] = RESULTS_FILE, **settings: Any
) -> Path:
    """Write metrics plus the run settings (tasks, workers, seed, ...) to JSON."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "settings": settings,
        "results": [asdict(r) for r in results],
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def load_results(path: Union[str, Path] = RESULTS_FILE) -> Optional[dict[str, Any]]:
    """Read saved benchmark metrics, or None if missing/unreadable."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["results"] = [PolicyResult(**r) for r in data["results"]]
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="AgentOS synthetic scheduling benchmark.")
    parser.add_argument("--tasks", type=int, default=100)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--failure-rate", type=float, default=0.1)
    parser.add_argument("--arrival-rate", type=float, default=150.0, help="tasks per second")
    parser.add_argument("--aging-rate", type=float, default=10.0, help="priority levels gained per second waited")
    parser.add_argument("--no-chart", action="store_true")
    args = parser.parse_args(argv)

    print(f"=== AgentOS benchmark: {args.tasks} tasks, {args.workers} workers, seed {args.seed} ===")
    results = run_benchmark(args.tasks, args.workers, args.seed, args.failure_rate,
                            args.arrival_rate, args.aging_rate)
    print()
    print(format_results(results))
    print("\nTimes in seconds. HighWait = priority 1-2, LowWait = priority 9-10.")
    print(f"Metrics saved to {save_results(results, tasks=args.tasks, workers=args.workers, seed=args.seed, failure_rate=args.failure_rate, arrival_rate=args.arrival_rate, aging_rate=args.aging_rate)}")
    if not args.no_chart:
        print(f"Chart saved to {save_chart(results)}")


if __name__ == "__main__":
    main()
