"""Dashboard pages and shared widgets (imported by dashboard/app.py).

UI only: every action calls into dashboard.jobs / dashboard.files, which in
turn use the existing AgentOS runtime, workflow and benchmark modules.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st

from dashboard.files import MIME_TYPES, OutputFiles, UnsafeFileError
from dashboard.jobs import WORKFLOW_MODES, JobAlreadyRunning, JobManager, WorkflowJob
from dashboard.ui import events_dataframe, fmt_seconds, hero, status_badge
from demo.benchmark import HIGH_PRIORITY, LOW_PRIORITY, load_results
from runtime.permissions import SENSITIVE_ACTIONS
from runtime.sandbox import DEFAULT_OUTPUT_DIR

RECOVERY_EVENTS = ("failed", "retried", "reassigned", "escalated")
STEP_LABELS = {"research": "1 · Research", "docgen": "2 · DocGen", "file": "3 · File", "email": "4 · Email"}


def show_table(df: pd.DataFrame, live: bool, **kwargs: Any) -> None:
    """Interactive grid normally; a static table while auto-refreshing (the grid
    cannot repaint within a 1 s refresh cycle)."""
    if live:
        st.table(df.set_index(df.columns[0]))
    else:
        st.dataframe(df, hide_index=True, width="stretch", **kwargs)


def output_dir() -> Path:
    """The AgentOS output sandbox (override with AGENTOS_OUTPUT_DIR, e.g. in tests)."""
    return Path(os.environ.get("AGENTOS_OUTPUT_DIR", DEFAULT_OUTPUT_DIR))


@st.cache_resource
def get_manager() -> JobManager:
    """One manager per server process, shared by all tabs (one job slot)."""
    return JobManager(sandbox_root=output_dir())


@st.cache_resource
def get_files() -> OutputFiles:
    return OutputFiles(output_dir())


def flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


def show_flash() -> None:
    if "flash" in st.session_state:
        kind, message = st.session_state.pop("flash")
        getattr(st, kind)(message)


def download(item_relative: str, label: Optional[str] = None, key: str = "") -> None:
    """Download button for a file in the output sandbox (validated by OutputFiles)."""
    files = get_files()
    try:
        data = files.read(item_relative)
    except UnsafeFileError as exc:
        st.caption(f"Unavailable: {exc}")
        return
    name = Path(item_relative).name
    mime = MIME_TYPES[Path(name).suffix.lower()]
    st.download_button(
        label or name, data=data, file_name=name, mime=mime, on_click="ignore",
        icon=":material/download:", key=f"dl-{key}-{item_relative}", width="stretch",
    )


def downloads_panel(categories: tuple[str, ...], key: str) -> None:
    files = [f for f in get_files().list() if f.category in categories]
    if not files:
        st.caption("No files yet. They appear here after a run.")
        return
    for f in files:
        c1, c2 = st.columns([3, 2], vertical_alignment="center")
        c1.markdown(f"**{f.relative}**  \n{f.category} · {f.size / 1024:.1f} KB · {f.modified:%Y-%m-%d %H:%M:%S}")
        with c2:
            download(f.relative, "Download", key=key)


# ---------------------------------------------------------------- sidebar
def sidebar() -> None:
    mgr = get_manager()
    with st.sidebar:
        st.caption("Runtime status")

        @st.fragment(run_every=1.0 if mgr.busy else None)
        def watcher() -> None:
            job = mgr.current
            if mgr.busy and job is not None:
                st.session_state["watching_job"] = id(job)
                st.badge(f"{job.kind} running", icon=":material/progress_activity:", color="blue")
                st.caption(f"Elapsed {job.elapsed:.1f} s")
                if isinstance(job, WorkflowJob) and job.broker.pending() is not None:
                    st.warning("A sensitive action is waiting for your approval.", icon=":material/front_hand:")
                    st.page_link(PAGES["workflow"], label="Review request", icon=":material/arrow_forward:")
            else:
                if st.session_state.pop("watching_job", None) is not None:
                    st.rerun()  # job just finished: refresh the whole page once
                st.badge("Idle", icon=":material/check:", color="gray")

        watcher()
        st.divider()
        st.caption("Research uses a local Ollama model; the other agents are mock. "
                   "Everything runs offline. Files are confined to ./output.")


# ---------------------------------------------------------------- overview
def page_overview() -> None:
    hero("AgentOS", "A runtime layer that manages AI agents the way an operating system manages processes.")
    st.write(
        "Agents are simple workers that each do one job. **AgentOS is not an agent**: it sits around "
        "them and handles scheduling, permissions, failure recovery and logging, so agents never have to."
    )

    cards = [
        (":material/schedule:", "Scheduler", "Pluggable policies: FIFO, Priority and Aging (anti-starvation)."),
        (":material/shield:", "Permissions", f"Capability-based, deny by default. Human approval for {', '.join(sorted(SENSITIVE_ACTIONS))}."),
        (":material/healing:", "Recovery", "Retry the same agent, reassign to a compatible instance, then escalate."),
        (":material/receipt_long:", "Audit log", "Every decision is timestamped and exportable to JSON."),
    ]
    cols = st.columns(4)
    for col, (icon, title, text) in zip(cols, cards):
        with col.container(border=True, height="stretch"):
            st.markdown(f"#### {icon} {title}")
            st.caption(text)

    st.subheader("Demo workflow")
    steps = [("Research", "llm_inference · local Ollama"), ("DocGen", "write_file"), ("FileManager", "read/write_file"), ("Email", "send_email · needs approval")]
    cols = st.columns([4, 1, 4, 1, 4, 1, 4])
    for i, (name, perm) in enumerate(steps):
        with cols[i * 2].container(border=True, height="stretch"):
            st.markdown(f"**{name}Agent**")
            st.caption(perm)
        if i < len(steps) - 1:
            cols[i * 2 + 1].markdown('<div class="aos-step-arrow">&rarr;</div>', unsafe_allow_html=True)
    st.caption("Each step is dispatched by capability to one of 9 registered instances "
               "(3 research, 2 docgen, 2 file manager, 2 email). If an instance fails, recovery "
               "moves the task to another compatible instance.")

    st.subheader("Latest activity")
    mgr = get_manager()
    job = mgr.last_workflow
    bench = load_results(get_files().sandbox.root / "benchmark_results.json")
    m = st.columns(4)
    if job is None:
        m[0].metric("Last workflow", "None yet", border=True)
        m[1].metric("Tasks", "-", border=True)
        m[2].metric("Recovery events", "-", border=True)
    else:
        outcome = "Running" if job.running else ("Succeeded" if job.succeeded else "Did not complete")
        m[0].metric("Last workflow", outcome, border=True)
        m[1].metric("Tasks", len(job.aos.tasks), border=True)
        m[2].metric(
            "Recovery events", sum(job.aos.audit.count(e) for e in ("retried", "reassigned", "escalated")),
            help="retried + reassigned + escalated", border=True,
        )
    if bench:
        res = {r.policy: r for r in bench["results"]}
        if "FIFO" in res and "Priority" in res and res["Priority"].high_avg_wait > 0:
            speedup = res["FIFO"].high_avg_wait / res["Priority"].high_avg_wait
            m[3].metric("High-priority speed-up", f"{speedup:.0f}x", help="FIFO vs Priority, average wait of priority 1-2 tasks", border=True)
        else:
            m[3].metric("Benchmark", "Available", border=True)
    else:
        m[3].metric("Benchmark", "Not run yet", border=True)

    st.subheader("Try it")
    c = st.columns(3)
    c[0].page_link(PAGES["workflow"], label="Run the 4-agent workflow", icon=":material/play_circle:")
    c[1].page_link(PAGES["workflow"], label="Force a failure and watch recovery", icon=":material/healing:")
    c[2].page_link(PAGES["benchmark"], label="Compare scheduling policies", icon=":material/bar_chart:")


# ---------------------------------------------------------------- workflow
def _start_workflow() -> None:
    try:
        get_manager().start_workflow(st.session_state["wf_mode"], st.session_state["wf_topic"])
        flash("success", "Workflow started. Progress below is read live from the AgentOS runtime.")
    except JobAlreadyRunning as exc:
        flash("error", f"{exc}. Wait for it to finish.")


def _resolve(job: WorkflowJob, request_id: int, approved: bool) -> None:
    if not job.broker.resolve(request_id, approved):
        flash("warning", "That approval request was already decided.")


def render_approval(job: WorkflowJob) -> None:
    req = job.broker.pending()
    if req is None:
        return
    with st.container(border=True):
        st.markdown("#### :material/front_hand: Human approval required")
        st.write(f"**{req.agent_name}** is paused and asks to perform the sensitive action **`{req.action}`**.")
        st.code(req.detail, language=None, wrap_lines=True)
        st.caption(f"If nobody decides within {req.seconds_left:.0f} s, AgentOS rejects it automatically (fail closed).")
        a, r = st.columns(2)
        a.button("Approve", type="primary", icon=":material/check:", width="stretch",
                 key=f"approve-{id(job)}-{req.request_id}", on_click=_resolve, args=(job, req.request_id, True))
        r.button("Reject", icon=":material/close:", width="stretch",
                 key=f"reject-{id(job)}-{req.request_id}", on_click=_resolve, args=(job, req.request_id, False))


def render_workflow_live(job: WorkflowJob) -> None:
    head = st.columns([3, 1], vertical_alignment="center")
    head[0].markdown(f"**Run:** {WORKFLOW_MODES[job.mode]}  \n**Topic:** {job.topic}")
    with head[1]:
        status_badge("running" if job.running else ("completed" if job.succeeded else "incomplete"))
        st.caption(f"{job.elapsed:.1f} s")

    render_approval(job)

    cols = st.columns(len(job.steps))
    for col, step in zip(cols, job.steps):
        acb = job.step_acb(step)
        with col.container(border=True, height="stretch"):
            st.markdown(f"**{STEP_LABELS.get(step.name, step.name)}**")
            status_badge(job.step_status(step))
            if acb is None:
                st.caption(", ".join(step.capabilities) or step.agent_id)
            else:
                agents = " → ".join(job.aos.agent(a).name for a in acb.agents_tried) or "awaiting dispatch"
                st.caption(f"{acb.task_id} · {agents}")
                st.caption(f"Attempts: {acb.attempts} · Duration: {fmt_seconds(acb.duration)}")
                if acb.metadata.get("llm_model"):
                    st.caption(f"Model: {acb.metadata['llm_model']} ({acb.metadata.get('llm_backend', '?')})")

    recovery = [asdict(e) for e in job.aos.audit.events() if e.event in RECOVERY_EVENTS]
    if recovery:
        st.markdown("**Recovery events** (from the audit log)")
        show_table(events_dataframe(recovery), live=job.running)

    if job.status == "failed":
        st.error(f"The workflow thread crashed: {job.error}")
    elif not job.running:
        if job.succeeded:
            st.success("Workflow succeeded: report generated, filed and emailed (mock).", icon=":material/check_circle:")
        else:
            st.warning("Workflow did not complete. See the step outcomes and the Audit Log page.", icon=":material/warning:")
        with st.expander("Step results and errors"):
            for step in job.steps:
                acb = job.step_acb(step)
                st.markdown(f"**{step.name}**: {acb.outcome if acb else 'not run'}")
                if acb is not None and acb.outcome == "completed":
                    st.json(acb.result, expanded=False)
                elif acb is not None and acb.error:
                    st.code(acb.error, language=None, wrap_lines=True)


def llm_status() -> tuple[bool, str]:
    """Reachability of the research model, cached for a few seconds per page render."""
    now = time.time()
    cached = st.session_state.get("llm_status")
    if cached and now - cached[0] < 10:
        return cached[1]
    status = get_manager().llm.health(timeout=2.0)
    st.session_state["llm_status"] = (now, status)
    return status


def page_workflow() -> None:
    mgr = get_manager()
    hero("Workflow Runner", "Research → DocGen → FileManager → Email, executed by the AgentOS kernel.")
    show_flash()

    with st.container(border=True):
        c1, c2 = st.columns([2, 3])
        c1.text_input("Research topic", value="AI Agents in Healthcare", max_chars=80, key="wf_topic",
                      disabled=mgr.busy)
        c2.radio("Scenario", list(WORKFLOW_MODES), format_func=WORKFLOW_MODES.get, key="wf_mode",
                 disabled=mgr.busy)
        st.caption(":material/shield: The email step needs **your approval on this page**. "
                   "Nothing is approved automatically; no answer within 5 minutes means rejected.")
        llm_ok, llm_msg = llm_status()
        if mgr.llm.backend == "mock":
            st.warning(f"Research uses a **mock LLM** ({llm_msg}): placeholder text, not real analysis.",
                       icon=":material/science:")
        elif llm_ok:
            st.caption(f":material/psychology: Research: local LLM analysis with {llm_msg} "
                       "(no web access; one answer can take a minute or two).")
        else:
            st.error(f"Research model unavailable: {llm_msg}. The research step will fail and "
                     "later steps will be skipped.", icon=":material/error:")
        st.button("Run workflow", type="primary", icon=":material/play_arrow:", disabled=mgr.busy,
                  on_click=_start_workflow, help="Disabled while another job is running")

    @st.fragment(run_every=1.0 if mgr.busy else None)
    def live() -> None:
        job = mgr.last_workflow
        if job is None:
            st.info("No workflow has run yet in this dashboard session.", icon=":material/info:")
        else:
            render_workflow_live(job)

    live()

    st.subheader("Downloads")
    downloads_panel(("Presentation", "Email outbox", "Audit log"), key="wf")


# ---------------------------------------------------------------- agents
def page_agents() -> None:
    mgr = get_manager()
    hero("Agent Status", "Registered agents, their capabilities and every task's control block (ACB).")
    job = mgr.last_workflow
    if job is None:
        st.info("Run a workflow first; this page shows the agents and tasks of the latest run.", icon=":material/info:")
        st.page_link(PAGES["workflow"], label="Go to Workflow Runner", icon=":material/arrow_forward:")
        return

    @st.fragment(run_every=1.0 if mgr.busy else None)
    def live() -> None:
        aos = job.aos
        backups = {a.id: aos.recovery.backup_for(a.id) for a in aos.agents}
        st.subheader("Agents")
        live_now = mgr.busy
        records = {r.agent.id: r for r in aos.registry.records()}
        show_table(pd.DataFrame([{
            "Agent": a.name,
            "Type": records[a.id].agent_type,
            "Capabilities": ", ".join(sorted(a.capabilities)) or "-",
            "Available": "yes" if records[a.id].available else "no",
            "Dispatched": records[a.id].dispatch_count,
            "State": a.state.value,
            "Granted capabilities": ", ".join(sorted(aos.permissions.capabilities(a.id))) or "-",
            "Sensitive (needs approval)": ", ".join(sorted(aos.permissions.capabilities(a.id) & aos.permissions.sensitive_actions)) or "-",
            "Backup agent": aos.agent(backups[a.id]).name if backups[a.id] else "-",
            "Backup for": ", ".join(aos.agent(p).name for p, b in backups.items() if b == a.id) or "-",
        } for a in aos.agents]), live=live_now)

        st.subheader("Tasks (Agent Control Blocks)")
        tasks = aos.tasks
        if not tasks:
            st.caption("No tasks submitted yet.")
            return
        show_table(pd.DataFrame([{
            "Task": a.task_id,
            "Step": a.label or "-",
            "Agent(s)": " → ".join(aos.agent(i).name for i in a.agents_tried) or "-",
            "State": a.state.value,
            "Outcome": a.outcome,
            "Attempts": a.attempts,
            "Priority": a.priority,
            "Wait (s)": a.wait_time,
            "Duration (s)": a.duration,
            "Error": a.error or "",
        } for a in tasks]).round(3), live=live_now,
            column_config={
                "Wait (s)": st.column_config.NumberColumn(format="%.3f"),
                "Duration (s)": st.column_config.NumberColumn(format="%.3f"),
            })
        st.caption(f"Scheduler policy: **{aos.scheduler.policy.name}** · max retries per agent: "
                   f"**{aos.recovery.max_retries}** · agent state shows its most recent task.")

    live()


# ---------------------------------------------------------------- audit
LIVE_SOURCE = "Latest dashboard run (live)"


def _load_events(source: str) -> tuple[list[dict[str, Any]], Optional[bytes]]:
    """Events for the chosen source, plus raw bytes for download (files only)."""
    if source == LIVE_SOURCE:
        job = get_manager().last_workflow
        return ([asdict(e) for e in job.aos.audit.events()] if job else []), None
    raw = get_files().read(source)
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(e, dict) for e in data):
        raise ValueError("not an AgentOS audit log")
    return data, raw


def page_audit() -> None:
    mgr = get_manager()
    hero("Audit Log", "Every scheduling, permission and recovery decision, as recorded by the runtime.")
    sources = ([LIVE_SOURCE] if mgr.last_workflow else []) + [f.relative for f in get_files().list("Audit log")]
    if not sources:
        st.info("No audit log yet. Run a workflow here, or `python main.py` from the CLI.", icon=":material/info:")
        return
    source = st.selectbox("Source", sources, help="Saved logs come from ./output (dashboard and CLI runs)")

    @st.fragment(run_every=1.0 if (mgr.busy and source == LIVE_SOURCE) else None)
    def live() -> None:
        try:
            events, raw = _load_events(source)
        except (UnsafeFileError, ValueError) as exc:
            st.error(f"Cannot read {source}: {exc}")
            return
        df = events_dataframe(events)

        m = st.columns(4)
        m[0].metric("Events", len(df), border=True)
        m[1].metric("Permission checks", int((df["Category"] == "Permission").sum()), border=True)
        m[2].metric("Denied / rejected", int(df["Event"].isin(["permission_denied", "permission_rejected"]).sum()), border=True)
        m[3].metric("Retry / reassign / escalate",
                    " / ".join(str(int((df["Event"] == e).sum())) for e in ("retried", "reassigned", "escalated")),
                    border=True)

        f1, f2, f3 = st.columns([2, 1, 2])
        cats = f1.multiselect("Category", ["Lifecycle", "Dispatch", "Recovery", "Permission"],
                              default=["Lifecycle", "Dispatch", "Recovery", "Permission"], key=f"cat-{source}")
        tasks = f2.selectbox("Task", ["All"] + sorted(t for t in df["Task"].unique() if t != "-"), key=f"task-{source}")
        text = f3.text_input("Search", placeholder="agent, action, error ...", key=f"q-{source}")
        view = df[df["Category"].isin(cats)]
        if tasks != "All":
            view = view[view["Task"] == tasks]
        if text:
            view = view[view.apply(lambda r: text.lower() in " ".join(map(str, r.values)).lower(), axis=1)]
        show_table(view, live=mgr.busy and source == LIVE_SOURCE, height=min(38 + 35 * len(view), 560))

        data = raw if raw is not None else json.dumps(events, indent=2, default=str).encode()
        st.download_button("Download this log (JSON)", data=data, file_name=Path(source).name if raw else "audit_log_live.json",
                           mime="application/json", on_click="ignore", icon=":material/download:",
                           key=f"dl-audit-{source}")

    live()


# ---------------------------------------------------------------- benchmark
def _start_benchmark() -> None:
    s = st.session_state
    try:
        get_manager().start_benchmark(s["bm_tasks"], s["bm_workers"], s["bm_seed"], s["bm_fail"])
        flash("success", "Benchmark started: FIFO, Priority and Aging on the same seeded workload.")
    except JobAlreadyRunning as exc:
        flash("error", f"{exc}. Wait for it to finish.")


def page_benchmark() -> None:
    mgr = get_manager()
    files = get_files()
    hero("Scheduler Benchmark", "FIFO vs Priority vs Aging on an identical, seeded synthetic workload.")
    show_flash()

    with st.container(border=True):
        c = st.columns(4)
        c[0].number_input("Tasks", 20, 500, 100, step=10, key="bm_tasks", disabled=mgr.busy)
        c[1].number_input("Workers", 1, 16, 4, key="bm_workers", disabled=mgr.busy)
        c[2].number_input("Seed", 0, 10_000, 42, key="bm_seed", disabled=mgr.busy)
        c[3].slider("Failure rate", 0.0, 0.5, 0.1, 0.05, key="bm_fail", disabled=mgr.busy)
        st.button("Run benchmark", type="primary", icon=":material/play_arrow:", disabled=mgr.busy,
                  on_click=_start_benchmark, help="Takes a few seconds; disabled while another job is running")

    @st.fragment(run_every=1.0 if mgr.busy else None)
    def progress() -> None:
        job = mgr.last_benchmark
        if job is None:
            return
        if job.running:
            with st.status(f"Benchmark running ({job.elapsed:.0f} s)", expanded=True):
                for line in list(job.progress):
                    st.write(line.strip())
        elif job.status == "failed":
            st.error(f"Benchmark failed: {job.error}")

    progress()

    data = load_results(files.sandbox.root / "benchmark_results.json")
    if not data:
        st.info("No saved benchmark metrics yet. Run the benchmark above (or `python main.py 3`).", icon=":material/info:")
    else:
        results = data["results"]
        settings = data.get("settings", {})
        st.caption(f"Results from {data.get('generated_at', '?')} · "
                   + " · ".join(f"{k}={v}" for k, v in settings.items()))
        st.subheader(f"Average wait of high-priority tasks (priority 1-{HIGH_PRIORITY})")
        base = next((r for r in results if r.policy == "FIFO"), None)
        cols = st.columns(len(results))
        for col, r in zip(cols, results):
            delta = None
            if base is not None and r is not base:
                delta = f"{r.high_avg_wait - base.high_avg_wait:+.3f} s vs FIFO"
            col.metric(r.policy, f"{r.high_avg_wait:.3f} s", delta=delta, delta_color="inverse", border=True)

        st.subheader("All metrics")
        st.dataframe(pd.DataFrame([{
            "Policy": r.policy,
            "Total time (s)": r.total_time,
            "Avg wait (s)": r.avg_wait,
            "Avg turnaround (s)": r.avg_turnaround,
            "Completion rate": r.completion_rate * 100,
            "Retries": r.retries,
            "Escalations": r.escalations,
            f"High-prio wait (s)": r.high_avg_wait,
            f"Low-prio wait (s)": r.low_avg_wait,
            "Max wait (s)": r.max_wait,
        } for r in results]), hide_index=True, width="stretch", column_config={
            **{k: st.column_config.NumberColumn(format="%.3f") for k in
               ("Total time (s)", "Avg wait (s)", "Avg turnaround (s)", "High-prio wait (s)", "Low-prio wait (s)", "Max wait (s)")},
            "Completion rate": st.column_config.NumberColumn(format="%.0f%%"),
        })
        st.caption(f"High-prio = priority 1-{HIGH_PRIORITY}; low-prio = priority {LOW_PRIORITY}-10. Lower is better.")

        by = {r.policy: r for r in results}
        if {"FIFO", "Priority", "Aging"} <= by.keys():
            f, p, a = by["FIFO"], by["Priority"], by["Aging"]
            st.markdown(
                f"- **Priority** changes high-priority wait from {f.high_avg_wait:.3f} s (FIFO) to "
                f"{p.high_avg_wait:.3f} s, while low-priority wait goes from {f.low_avg_wait:.3f} s to {p.low_avg_wait:.3f} s.\n"
                f"- **Aging** has high-priority wait {a.high_avg_wait:.3f} s and max wait {a.max_wait:.3f} s "
                f"(Priority: {p.max_wait:.3f} s), showing its effect on starvation.\n"
                f"- Total time: FIFO {f.total_time:.2f} s, Priority {p.total_time:.2f} s, Aging {a.total_time:.2f} s."
            )

    st.subheader("Chart")
    try:
        png = files.read("benchmark.png")
        chart = next(f for f in files.list("Benchmark") if f.relative == "benchmark.png")
        st.image(png, caption=f"output/benchmark.png · generated {chart.modified:%Y-%m-%d %H:%M:%S}", width="stretch")
    except (UnsafeFileError, StopIteration):
        st.caption("No chart yet. It is generated when the benchmark runs.")

    st.subheader("Downloads")
    downloads_panel(("Benchmark",), key="bm")


# ---------------------------------------------------------------- navigation
PAGES = {
    "overview": st.Page(page_overview, title="Overview", icon=":material/dashboard:", url_path="overview", default=True),
    "workflow": st.Page(page_workflow, title="Workflow Runner", icon=":material/account_tree:", url_path="workflow"),
    "agents": st.Page(page_agents, title="Agent Status", icon=":material/smart_toy:", url_path="agents"),
    "audit": st.Page(page_audit, title="Audit Log", icon=":material/receipt_long:", url_path="audit"),
    "benchmark": st.Page(page_benchmark, title="Scheduler Benchmark", icon=":material/bar_chart:", url_path="benchmark"),
}

