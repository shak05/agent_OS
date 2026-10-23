"""Stage 3 tests: synthetic agents, staggered arrivals and the benchmark."""
from __future__ import annotations

import threading
import time

import pytest

from agents.synthetic import SyntheticAgent
from demo.benchmark import format_results, generate_tasks, run_benchmark, save_chart
from runtime import AgentOS


def test_generate_tasks_is_seeded() -> None:
    assert generate_tasks(20, seed=1) == generate_tasks(20, seed=1)
    assert generate_tasks(20, seed=1) != generate_tasks(20, seed=2)


def test_synthetic_agent_fails_and_succeeds() -> None:
    ok = SyntheticAgent("ok", failure_rate=0.0, max_time=0.01, seed=1)
    assert ok.run(None)["agent"] == "ok"
    bad = SyntheticAgent("bad", failure_rate=1.0, max_time=0.01, seed=1)
    with pytest.raises(RuntimeError):
        bad.run(None)


def test_run_all_waits_for_late_arrivals() -> None:
    aos = AgentOS()
    agent = aos.register(SyntheticAgent("s", failure_rate=0.0, max_time=0.01, seed=1))
    closed = threading.Event()

    def feeder() -> None:
        for _ in range(5):
            time.sleep(0.02)  # queue is empty in between: workers must not quit
            aos.submit(agent, None)
        closed.set()

    threading.Thread(target=feeder).start()
    finished = aos.run_all(workers=2, input_closed=closed)
    assert len(finished) == 5


def test_benchmark_runs_all_policies(tmp_path) -> None:
    results = run_benchmark(n=30, workers=4, arrival_rate=300, progress=None)
    assert [r.policy for r in results] == ["FIFO", "Priority", "Aging"]
    # Same seeded workload -> same failures under every policy.
    assert len({(r.retries, r.escalations) for r in results}) == 1
    assert all(0 < r.completion_rate <= 1 for r in results)
    assert "Priority" in format_results(results)
    assert save_chart(results, tmp_path / "bench.png").stat().st_size > 0
