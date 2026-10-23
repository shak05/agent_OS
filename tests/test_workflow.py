"""Stage 2 tests: demo agents, sandboxing and the 4-agent workflow."""
from __future__ import annotations

import json

import pytest
from pptx import Presentation

from agents import FileManagerAgent
from demo.workflow import WorkflowStep, order_steps, run_demo
from runtime import AgentOS


def test_workflow_end_to_end(tmp_path) -> None:
    aos, run = run_demo(sandbox_root=tmp_path, research_delay=0, verbose=False)
    assert run.succeeded
    report = tmp_path / "reports" / "ai-agents-in-healthcare.pptx"
    assert report.is_file()
    assert not (tmp_path / "drafts" / "ai-agents-in-healthcare.pptx").exists()  # moved
    assert len(Presentation(report).slides) == 4
    sent = json.loads((tmp_path / "sent_emails.json").read_text(encoding="utf-8"))
    assert sent[-1]["attachment"] == "reports/ai-agents-in-healthcare.pptx"
    assert aos.audit.count("permission_confirmed") == 1


def test_workflow_recovers_by_reassigning_research(tmp_path) -> None:
    aos, run = run_demo(fail="reassign", sandbox_root=tmp_path, research_delay=0, verbose=False)
    assert run.succeeded
    research = run.acbs["research"]
    assert research.agents_tried == ["researchagent", "research-backup"]
    assert aos.audit.count("retried") == 2 and aos.audit.count("reassigned") == 1


def test_workflow_recovers_by_retry(tmp_path) -> None:
    aos, run = run_demo(fail="retry", sandbox_root=tmp_path, research_delay=0, verbose=False)
    assert run.succeeded
    assert run.acbs["research"].attempts == 2
    assert aos.audit.count("reassigned") == 0


def test_email_rejected_means_nothing_sent(tmp_path) -> None:
    _, run = run_demo(confirm="auto-deny", sandbox_root=tmp_path, research_delay=0, verbose=False)
    assert not run.succeeded
    assert run.acbs["email"].outcome == "rejected"
    assert not (tmp_path / "sent_emails.json").exists()
    assert (tmp_path / "reports" / "ai-agents-in-healthcare.pptx").is_file()


def test_sandbox_blocks_escape(tmp_path) -> None:
    aos = AgentOS(sandbox_root=tmp_path / "out")
    fm = aos.register(FileManagerAgent())
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_text("hi")
    acb = aos.run_task(fm, {"src": "a.txt", "dest": "../stolen.txt", "mode": "copy"})
    assert acb.outcome == "denied"
    assert not (tmp_path / "stolen.txt").exists()
    assert aos.audit.count("permission_denied") == 1


def test_order_steps_detects_cycle() -> None:
    steps = [
        WorkflowStep("a", "x", lambda r: None, depends_on=("b",)),
        WorkflowStep("b", "x", lambda r: None, depends_on=("a",)),
    ]
    with pytest.raises(ValueError, match="cycle"):
        order_steps(steps)
