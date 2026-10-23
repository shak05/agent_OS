"""Stage 5 tests: dashboard integration logic (approval gate, jobs, safe files, pages)."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from dashboard.approval import ApprovalBroker
from dashboard.files import OutputFiles, UnsafeFileError
from dashboard.jobs import JobAlreadyRunning, JobManager, WorkflowJob
from demo.benchmark import PolicyResult, load_results, save_results
from runtime import Agent, AgentOS


def wait_for_pending(broker: ApprovalBroker, timeout: float = 10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        req = broker.pending()
        if req is not None:
            return req
        time.sleep(0.01)
    raise AssertionError("no approval request appeared")


# --------------------------------------------------------------- approval broker
def _ask_in_thread(broker: ApprovalBroker) -> tuple[threading.Thread, list[bool]]:
    answers: list[bool] = []
    t = threading.Thread(target=lambda: answers.append(broker("Mailer", "send_email", "to=x")))
    t.start()
    return t, answers


def test_broker_approve_and_reject() -> None:
    for decision in (True, False):
        broker = ApprovalBroker(timeout=10)
        t, answers = _ask_in_thread(broker)
        req = wait_for_pending(broker)
        assert (req.agent_name, req.action, req.detail) == ("Mailer", "send_email", "to=x")
        assert broker.resolve(req.request_id, decision)
        t.join(5)
        assert answers == [decision]
        assert req.decided_by == "human"
        assert broker.pending() is None


def test_broker_times_out_to_reject() -> None:
    broker = ApprovalBroker(timeout=0.1)
    assert broker("Mailer", "send_email", "to=x") is False
    assert broker.history[-1].decided_by == "timeout"


def test_broker_ignores_stale_and_duplicate_clicks() -> None:
    broker = ApprovalBroker(timeout=10)
    t, answers = _ask_in_thread(broker)
    req = wait_for_pending(broker)
    assert not broker.resolve(req.request_id + 1, True)  # wrong id: ignored
    assert broker.pending() is req and req.is_pending
    assert broker.resolve(req.request_id, False)
    assert not broker.resolve(req.request_id, True)  # second click: ignored
    t.join(5)
    assert answers == [False]


def test_broker_plugs_into_permission_manager() -> None:
    class Mailer(Agent):
        def run(self, task_input):
            self.request_permission("send_email", "to=bob")
            return "sent"

    broker = ApprovalBroker(timeout=10)
    aos = AgentOS(confirm=broker)
    mailer = aos.register(Mailer("Mailer", permissions=["send_email"]))
    result: list = []
    t = threading.Thread(target=lambda: result.append(aos.run_task(mailer)))
    t.start()
    broker.resolve(wait_for_pending(broker).request_id, True)
    t.join(5)
    assert result[0].outcome == "completed"
    assert aos.audit.count("permission_confirmed") == 1


# ------------------------------------------------------------------ workflow jobs
def _manager(tmp_path: Path, timeout: float = 10.0) -> JobManager:
    return JobManager(sandbox_root=tmp_path, research_delay=0, approval_timeout=timeout)


def test_workflow_job_waits_for_human_then_completes(tmp_path) -> None:
    mgr = _manager(tmp_path)
    job = mgr.start_workflow("none")
    req = wait_for_pending(job.broker)
    email = job.steps[-1]
    assert job.step_status(email) == "awaiting approval"
    assert not (tmp_path / "sent_emails.json").exists()  # nothing sent before approval
    job.broker.resolve(req.request_id, True)
    assert job.wait(10)
    assert job.status == "finished" and job.succeeded
    assert [job.step_status(s) for s in job.steps] == ["completed"] * 4
    assert (tmp_path / "sent_emails.json").is_file()
    assert job.audit_path is not None and job.audit_path.is_file()


def test_workflow_job_rejection_sends_nothing(tmp_path) -> None:
    job = _manager(tmp_path).start_workflow("none")
    job.broker.resolve(wait_for_pending(job.broker).request_id, False)
    assert job.wait(10)
    assert not job.succeeded
    assert job.step_status(job.steps[-1]) == "rejected"
    assert not (tmp_path / "sent_emails.json").exists()
    assert job.aos.audit.count("permission_rejected") == 1


def test_workflow_job_unanswered_request_is_rejected(tmp_path) -> None:
    job = _manager(tmp_path, timeout=0.2).start_workflow("none")
    assert job.wait(10)
    assert job.step_status(job.steps[-1]) == "rejected"
    assert job.broker.history[-1].decided_by == "timeout"


def test_workflow_job_shows_real_recovery(tmp_path) -> None:
    job = _manager(tmp_path).start_workflow("reassign")
    job.broker.resolve(wait_for_pending(job.broker).request_id, True)
    assert job.wait(10) and job.succeeded
    research = job.step_acb(job.steps[0])
    assert research.agents_tried == ["researchagent", "research-backup"]
    assert job.aos.audit.count("retried") == 2 and job.aos.audit.count("reassigned") == 1


def test_manager_blocks_duplicate_runs(tmp_path) -> None:
    mgr = _manager(tmp_path)
    job = mgr.start_workflow("none")
    wait_for_pending(job.broker)
    with pytest.raises(JobAlreadyRunning):
        mgr.start_workflow("none")
    with pytest.raises(JobAlreadyRunning):
        mgr.start_benchmark(tasks=20)
    job.broker.resolve(job.broker.pending().request_id, False)
    assert job.wait(10)
    second = mgr.start_workflow("none")  # free again once finished
    second.broker.resolve(wait_for_pending(second.broker).request_id, False)
    assert second.wait(10)


def test_unknown_mode_rejected(tmp_path) -> None:
    with pytest.raises(ValueError):
        WorkflowJob("explode", sandbox_root=tmp_path)


def test_benchmark_job_saves_metrics_and_chart(tmp_path) -> None:
    job = _manager(tmp_path).start_benchmark(tasks=20, workers=4)
    assert job.wait(60) and job.status == "finished", job.error
    assert any("FIFO" in line for line in job.progress)
    data = load_results(tmp_path / "benchmark_results.json")
    assert [r.policy for r in data["results"]] == ["FIFO", "Priority", "Aging"]
    assert data["settings"]["tasks"] == 20
    assert (tmp_path / "benchmark.png").stat().st_size > 0


def test_results_roundtrip_and_bad_files(tmp_path) -> None:
    r = PolicyResult("FIFO", 1.0, 0.1, 0.2, 1.0, 3, 0, 0.1, 0.1, 0.5)
    path = save_results([r], tmp_path / "res.json", tasks=5)
    assert load_results(path)["results"] == [r]
    assert load_results(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert load_results(tmp_path / "bad.json") is None


# ------------------------------------------------------------------- safe files
def test_output_files_only_serves_allowed_files_in_sandbox(tmp_path) -> None:
    out = tmp_path / "output"
    (out / "reports").mkdir(parents=True)
    (out / "reports" / "r.pptx").write_bytes(b"pptx")
    (out / "sent_emails.json").write_text("[]")
    (out / "audit_log.json").write_text("[]")
    (out / "notes.txt").write_text("secret")  # wrong type: never listed
    (tmp_path / "outside.json").write_text("{}")  # outside sandbox
    files = OutputFiles(out)

    listed = {f.relative: f.category for f in files.list()}
    assert listed == {
        "reports/r.pptx": "Presentation",
        "sent_emails.json": "Email outbox",
        "audit_log.json": "Audit log",
    }
    assert [f.relative for f in files.list("Presentation")] == ["reports/r.pptx"]
    assert files.read("reports/r.pptx") == b"pptx"
    for bad in ["../outside.json", str(tmp_path / "outside.json"), "notes.txt", "missing.json", "reports/../../outside.json"]:
        with pytest.raises(UnsafeFileError):
            files.read(bad)


# ------------------------------------------------------------------ page smoke
def _render_page(page_name: str) -> None:
    import sys
    from pathlib import Path

    import streamlit as st

    root = Path.cwd()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from dashboard import views

    st.cache_resource.clear()  # fresh manager/files for this test's output dir
    st.navigation(list(views.PAGES.values()), position="hidden")
    views.sidebar()
    getattr(views, page_name)()


@pytest.mark.parametrize("page", ["page_overview", "page_workflow", "page_agents", "page_audit", "page_benchmark"])
def test_dashboard_pages_render(page, tmp_path, monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    out = tmp_path / "output"
    out.mkdir()
    (out / "audit_log.json").write_text(json.dumps([
        {"timestamp": "2026-01-01T10:00:00.000", "event": "submitted", "task_id": "T001",
         "agent": "ResearchAgent", "details": {"priority": 5}},
    ]))
    save_results([PolicyResult("FIFO", 1, 0.3, 0.4, 1, 0, 0, 0.3, 0.3, 0.7),
                  PolicyResult("Priority", 1, 0.3, 0.4, 1, 0, 0, 0.01, 0.9, 1.2),
                  PolicyResult("Aging", 1, 0.3, 0.4, 1, 0, 0, 0.01, 0.8, 0.9)],
                 out / "benchmark_results.json", tasks=100)
    monkeypatch.setenv("AGENTOS_OUTPUT_DIR", str(out))

    at = AppTest.from_function(_render_page, args=(page,), default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]


def test_benchmark_page_shows_saved_metrics(tmp_path, monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    out = tmp_path / "output"
    save_results([PolicyResult("FIFO", 1, 0.3, 0.4, 1, 0, 0, 0.300, 0.3, 0.7),
                  PolicyResult("Priority", 1, 0.3, 0.4, 1, 0, 0, 0.012, 0.9, 1.2),
                  PolicyResult("Aging", 1, 0.3, 0.4, 1, 0, 0, 0.011, 0.8, 0.9)],
                 out / "benchmark_results.json", tasks=100)
    monkeypatch.setenv("AGENTOS_OUTPUT_DIR", str(out))
    at = AppTest.from_function(_render_page, args=("page_benchmark",), default_timeout=30).run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric} == {"FIFO": "0.300 s", "Priority": "0.012 s", "Aging": "0.011 s"}


def test_workflow_page_has_no_auto_approve_option(tmp_path, monkeypatch) -> None:
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("AGENTOS_OUTPUT_DIR", str(tmp_path))
    at = AppTest.from_function(_render_page, args=("page_workflow",), default_timeout=30).run()
    assert not at.exception
    labels = [o for r in at.radio for o in r.options]
    assert labels and not any("approve" in o.lower() for o in labels)
    assert [b.label for b in at.button] == ["Run workflow"]
