# AgentOS — a runtime layer for AI agents

AgentOS manages AI agents the way an operating system manages processes.

- **Agents** are simple workers that each do one job (research, make slides, move a file, send an email).
- **AgentOS is not an agent.** It sits around the agents and does everything else: it decides
  **who runs when** (scheduling), **who may do what** (permissions), **what happens when
  something breaks** (recovery), and **keeps a record of it all** (audit log).

Agents never contain scheduling, permission or recovery code. When an agent needs something
sensitive, it asks AgentOS — just like a program makes a system call to the OS.

Phase 1 prototype. Everything is mock and runs **offline**: no API keys, no internet.

---

## Architecture

```
                 submit(task)                     dispatch
   user / workflow ─────────►  ┌──────────────── AgentOS kernel ───────────────┐
                               │                                               │
                               │  Scheduler ──► picks next task (FIFO /        │
                               │                Priority / Aging)              │
                               │      │                                        │
                               │      ▼                                        │
                               │  Permission manager ─ capability check,       │──► Agent.run()
                               │      │                human confirm for        │      │
                               │      │                send_email / delete_file │      │ syscalls:
                               │      ▼                                        │◄─────┘ request_permission()
                               │  Recovery engine ─ retry ► reassign to backup │        sandbox_path()
                               │                     ► escalate                │
                               │                                               │
                               │  Audit log ─ every event, timestamped ► JSON  │
                               │                                               │
                               │  [Memory Manager]   [Event Bus]   ← Phase 2   │
                               └───────────────────────────────────────────────┘
```

| OS concept | AgentOS equivalent | File |
|---|---|---|
| Process | `Agent` (states: IDLE, WAITING, RUNNING, COMPLETED, FAILED) | `runtime/agent.py` |
| Process Control Block | `AgentControlBlock` — one per task: priority, timings, attempts, result | `runtime/acb.py` |
| CPU scheduler | `Scheduler` + pluggable `SchedulingPolicy` | `runtime/scheduler.py` |
| Access control | `PermissionManager` — capability-based, deny by default | `runtime/permissions.py` |
| Fault handling | `RecoveryEngine` — retry → reassign → escalate | `runtime/recovery.py` |
| System log | `AuditLog` + agents-used table + JSON export | `runtime/audit.py` |
| File-system jail | `Sandbox` — agents can only touch `./output` | `runtime/sandbox.py` |
| Kernel | `AgentOS` — ties it all together | `runtime/kernel.py` |

### How a task flows through AgentOS

1. `aos.submit(agent, input, priority)` creates an ACB and puts it in the ready queue (`submitted`).
2. The scheduler picks the next task according to the active policy (`started`).
3. The kernel checks the task's required capabilities, then calls `agent.run(input)`.
4. While running, the agent may make system calls:
   - `request_permission("send_email", ...)` → capability check, plus human confirmation for sensitive actions.
   - `sandbox_path("reports/x.pptx", "write_file")` → capability check plus sandbox check, returns a safe path.
5. Outcome:
   - success → `completed`
   - exception → recovery engine: `retried` (same agent, up to N times) → `reassigned` (backup agent) → `escalated`
   - permission refused → `denied` / `rejected` — **final**, never retried (a security decision, not a glitch)

### Scheduling policies

| Policy | Rule | Good for | Weakness |
|---|---|---|---|
| **FIFO** | first submitted runs first | fairness, simplicity | urgent tasks wait behind everything |
| **Priority** | lowest number runs first, FIFO on ties | urgent tasks are fast | low-priority tasks can **starve** |
| **Aging** | `priority − rate × seconds_waited` | urgent tasks fast **and** no starvation | one extra tuning knob (`rate`) |

### Permissions

- Each agent declares the capabilities it needs (`web_search`, `read_file`, `write_file`, `send_email`, ...).
  AgentOS grants them at registration and denies everything else.
- `send_email` and `delete_file` are **sensitive**: even if granted, a confirmer must approve each use.
  - `interactive` — asks the human at the terminal
  - `auto-approve` / `auto-deny` — for unattended experiments and tests

---

## Setup

Requires Python 3.10+.

```bash
cd agentos
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS / Linux:
source .venv/bin/activate
pip install -r requirements.txt
```

All commands below are run from the `agentos/` folder with the virtual environment active.

## Running the demo menu

```bash
python main.py
```

| Option | What it shows |
|---|---|
| **1** Run the 4-agent workflow | Research → DocGen → FileManager → Email with live status lines. You can approve the email yourself (human-in-the-loop). Produces `output/reports/ai-agents-in-healthcare.pptx` and an entry in `output/sent_emails.json`. |
| **2** Workflow with forced failure | The primary ResearchAgent is "down". AgentOS retries it twice, reassigns the task to BackupResearchAgent, and the workflow still succeeds. |
| **3** 100-agent benchmark | Runs the same seeded workload under FIFO, Priority and Aging; prints a comparison table and saves `output/benchmark.png`. Takes ~5 s (the very first run is slower while matplotlib builds its font cache). |
| **4** Audit log + agents-used table | Full event log and per-task table of the last workflow from option 1 or 2; exported to `output/audit_log.json`. |
| **5** Runtime smoke demo | Five dummy agents covering every path: success, retry, reassign, escalate, permission denied. |

Each option can also be run directly, e.g. `python main.py 3`.

### Other commands

```bash
python -m pytest -v                                # all tests (~15 s)
python smoke_demo.py                               # same as menu option 5
python -m demo.workflow --fail retry               # primary fails once, retry succeeds
python -m demo.workflow --fail reassign            # same as menu option 2
python -m demo.workflow --confirm auto-deny        # email blocked by the confirmation gate
python -m demo.benchmark --tasks 200 --workers 8 --failure-rate 0.2 --aging-rate 5
```

### Output files (all inside `./output`)

| File | Created by |
|---|---|
| `reports/<topic>.pptx` | DocGenAgent (draft) moved by FileManagerAgent |
| `sent_emails.json` | EmailAgent's mock mail transport (nothing is really sent) |
| `audit_log.json` | workflow runs / menu option 4 |
| `benchmark.png` | benchmark |
| `benchmark_results.json` | benchmark (metrics shown by the dashboard) |

Delete the `output` folder at any time for a clean start.

---

## Web dashboard (Stage 5)

A Streamlit dashboard drives the same runtime from the browser. It runs locally and offline:
no API keys, no internet, and usage statistics are switched off in `.streamlit/config.toml`.

```bash
pip install -r requirements.txt     # adds streamlit
streamlit run dashboard/app.py      # run from the agentos/ folder
```

Streamlit prints a local URL (normally http://localhost:8501) and opens it in your browser.
Stop it with `Ctrl+C` in the terminal.

| Page | What it shows |
|---|---|
| **Overview** | What AgentOS is, the four subsystems, the demo pipeline, and the latest results. |
| **Workflow Runner** | Run the normal workflow or a forced-failure scenario (retry, or retry → reassign). Step cards show the real task state, attempts and durations as they change. **Approve or reject** the email when the EmailAgent asks. |
| **Agent Status** | Registered agents, granted and sensitive capabilities, backup links, and every task's control block (ACB). |
| **Audit Log** | The live run's events, or any saved `*audit_log.json` (including CLI runs), with category, task and text filters. |
| **Scheduler Benchmark** | Run FIFO vs Priority vs Aging from the browser. Shows the saved metrics, the high-priority wait per policy and `benchmark.png`. |

Downloads (presentations, `sent_emails.json`, audit logs, benchmark files) appear on the Workflow
Runner and Benchmark pages.

**How it fits the architecture**
- **UI only.** The dashboard (`dashboard/`) contains no scheduling, permission, recovery or audit
  logic. It calls the same `create_demo_os` / `run_workflow` / `run_benchmark` functions as the CLI
  and displays what the runtime records.
- **Human approval stays.** The CLI's `input()` prompt cannot work in a web page, so the dashboard plugs
  a different confirm callback, `ApprovalBroker`, into the unchanged `PermissionManager`. The workflow
  runs on a background thread, and the agent really pauses inside `request_permission()` until you
  click Approve or Reject. Unanswered requests are **rejected** after 5 minutes (fail closed).
  There is no auto-approve option in the dashboard.
- **No duplicate runs.** One job slot is shared by the whole server. Run buttons are disabled while a
  job is running, and a second start is refused, even from another browser tab.
- **Safe downloads.** Files are listed and read only through the runtime's `Sandbox`, restricted to
  `./output` and to `.pptx`, `.json` and `.png` files.

**Dashboard limitations**
- Dashboard state (the latest run) lives in server memory and is lost when Streamlit restarts. Saved
  files in `./output` stay.
- One job at a time per server. This is by design for a demo: concurrent runs would write the same
  output files.
- Pages refresh about once per second while a job runs. Tables use a static layout during that time
  and switch back to interactive tables when the job finishes.
- The workflow uses the demo's fixed priorities and Priority policy. Scheduling policies are compared
  on the Benchmark page.

---

## Project layout

```
agentos/
  runtime/   agent.py acb.py scheduler.py permissions.py recovery.py audit.py sandbox.py kernel.py
  agents/    research.py docgen.py filemanager.py email_agent.py synthetic.py
  demo/      workflow.py benchmark.py
  dashboard/ app.py (entry) views.py (pages) jobs.py approval.py files.py ui.py
  tests/     test_runtime.py test_workflow.py test_benchmark.py test_dashboard.py
  .streamlit/config.toml
  main.py  smoke_demo.py  requirements.txt  pytest.ini
```

## Extending AgentOS

**Add an agent** — subclass `Agent`, declare permissions, implement `run()`. No OS logic inside:

```python
from runtime import Agent, AgentOS

class SummariseAgent(Agent):
    def __init__(self):
        super().__init__("SummariseAgent", permissions=["read_file"])

    def run(self, task_input):
        path = self.sandbox_path(task_input["file"], "read_file")   # system call
        return path.read_text()[:200]

aos = AgentOS(confirm="interactive")
agent = aos.register(SummariseAgent())
acb = aos.run_task(agent, {"file": "notes.txt"}, priority=2)
print(acb.outcome, acb.result)
```

**Add a scheduling policy** — subclass `SchedulingPolicy` and return a sort key (lowest runs first):

```python
class ShortestJobFirst(SchedulingPolicy):
    name = "SJF"
    def sort_key(self, acb, current_time):
        return (acb.task_input.get("estimated_seconds", 1.0), acb.seq)
```

**Phase 2 extension points** are marked `EXTENSION POINT` in `runtime/kernel.py`:
`self.memory` (Memory Manager) and `self.event_bus` / `_emit()` (Event Bus — every kernel event
already flows through `_emit()`).

## Roadmap

| Phase | Feature | Idea |
|---|---|---|
| 2 | **Memory Manager** | Scoped, persistent memory per agent and per workflow (short-term context + long-term store), with permissions on who can read whose memory. |
| 2 | **Event Bus** | Publish/subscribe for kernel events, so agents can react to each other and a dashboard can stream live status. |
| 2 | **Dynamic scheduler** | Adjust policy and worker count at runtime from queue length, deadlines and observed failure rates. |
| 3 | **Real LLM agents** | Swap the mock agents for LLM-backed ones (e.g. Claude) behind the same `Agent` interface; add token/cost budgets as a scheduled resource. |
| ✓ | **Streamlit dashboard** | Done in Stage 5 (see *Web dashboard*). Next: stream events from the Event Bus instead of polling. |

## Known limitations (Phase 1)

- Retries run inline on the same worker (no back-off queue); `retry_delay` is available but defaults to 0.
- `Agent.state` reflects the most recent task only; the per-task truth is in the ACB.
- Permissions are granted from the agent's declaration at registration; there is no policy file yet.
- The audit log is in memory and exported to JSON on demand.
