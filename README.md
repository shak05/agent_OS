# AgentOS — a runtime layer for AI agents

AgentOS manages AI agents the way an operating system manages processes.

- **Agents** are simple workers that each do one job (research, make slides, move a file, send an email).
- **AgentOS is not an agent.** It sits around the agents and does everything else: it decides
  **which task runs next** (scheduler), **which agent instance runs it** (dispatcher + agent
  registry), **who may do what** (permissions), **what happens when something breaks**
  (recovery), and **keeps a record of it all** (audit log).

Agents never contain scheduling, permission or recovery code. When an agent needs something
sensitive, it asks AgentOS — just like a program makes a system call to the OS.

Phase 1 prototype. Everything runs **offline** with no API keys and no internet. The
ResearchAgent uses a **local Ollama model** (`llama3:latest`) for real LLM analysis; the other
agents (slides, files, email) are mock implementations.

---

## Architecture

```
                         ┌──────────────────────── AgentOS kernel (coordinates) ───────────────────────┐
  submit(task) ───────►  │  Task queue ──► SCHEDULER ──► selected TASK                                 │
  (agent pinned, or      │                 FIFO / Priority / Aging                                     │
   required capabilities)│                     │                                                       │
                         │                     ▼  selected task + required capabilities                │
                         │                 DISPATCHER ◄──── AGENT REGISTRY (instances, capabilities,   │
                         │                     │            availability)                              │
                         │                     ▼  selected agent INSTANCE  (or "no_agent")             │
                         │                 Permission manager ─ grant check, human confirm             │──► agent.run()
                         │                     │                for send_email / delete_file           │◄── syscalls
                         │                     ▼                                                       │
                         │                 Recovery engine ─ retry ► compatible replacement ► escalate │
                         │                                                                             │
                         │                 Audit log ─ every step above, timestamped ► JSON            │
                         │                 [Memory Manager]  [Event Bus]   ← not built yet             │
                         └─────────────────────────────────────────────────────────────────────────────┘
```

**Scheduler selects a task. Dispatcher selects an agent instance. Kernel coordinates.**

| OS concept | AgentOS equivalent | File |
|---|---|---|
| Process | `Agent` (states: IDLE, WAITING, RUNNING, COMPLETED, FAILED) | `runtime/agent.py` |
| Process Control Block | `AgentControlBlock` — one per task: priority, required capabilities, timings, attempts, result | `runtime/acb.py` |
| CPU scheduler | `Scheduler` + pluggable `SchedulingPolicy` — picks the next **task** | `runtime/scheduler.py` |
| Process table | `AgentRegistry` — registered agent **instances**, type, availability, dispatch counts | `runtime/registry.py` |
| Dispatcher / CPU assignment | `Dispatcher` — picks the agent **instance** for a task, by capability | `runtime/dispatcher.py` |
| Access control | `PermissionManager` — grants per agent, deny by default, human confirmation | `runtime/permissions.py` |
| Fault handling | `RecoveryEngine` — retry → reassign to a compatible instance → escalate | `runtime/recovery.py` |
| System log | `AuditLog` + agents-used table + JSON export | `runtime/audit.py` |
| File-system jail | `Sandbox` — agents can only touch `./output` | `runtime/sandbox.py` |
| Kernel | `AgentOS` — coordinates all of the above | `runtime/kernel.py` |

### How a task flows through AgentOS

1. **Submit.** `aos.submit(None, input, priority, required_capabilities=["web_research"])` creates an
   ACB with no agent bound yet and queues it (`submitted`). You can still pin a task to one instance
   with `aos.submit(agent, ...)`, which is what the benchmark and the older demos do.
2. **Schedule.** The scheduler picks the next queued task according to the policy (`scheduled`,
   with `policy=`). It does not look at agents at all.
3. **Dispatch.** The dispatcher asks the registry for instances and picks one that is compatible,
   available and authorized (`dispatched`, with the eligible set and the reason). If there is none,
   the task ends with outcome `no_agent` (`dispatch_failed`). It never falls back to an incompatible
   agent.
4. **Authorize and run.** The kernel checks the task's required permissions, then calls `agent.run(input)`.
   While running, the agent may make system calls:
   - `request_permission("send_email", ...)`: a grant check, plus human confirmation for sensitive actions.
   - `sandbox_path("reports/x.pptx", "write_file")`: a grant check plus a sandbox check; returns a safe path.
5. **Outcome.**
   - success → `completed`
   - exception → recovery engine: `retried` (same instance, up to N times) → `reassigned` (a compatible,
     authorized instance not yet tried) → `escalated`
   - permission refused → `denied` / `rejected`. This is **final** and never retried, because it is
     a security decision, not a glitch.

Every audit event records the task id, the agent name, and (once bound) the instance id
(`agent_id`) and class (`agent_type`). Before an instance is bound, these fields are `null`
rather than guessed.

### Scheduling policies

| Policy | Rule | Good for | Weakness |
|---|---|---|---|
| **FIFO** | first submitted runs first | fairness, simplicity | urgent tasks wait behind everything |
| **Priority** | lowest number runs first, FIFO on ties | urgent tasks are fast | low-priority tasks can **starve** |
| **Aging** | `priority − rate × seconds_waited` | urgent tasks fast **and** no starvation | one extra tuning knob (`rate`) |

### Permissions

- Each agent declares the permissions it needs (`llm_inference`, `read_file`, `write_file`, `send_email`, ...).
  AgentOS grants them at registration and denies everything else.
- `send_email` and `delete_file` are **sensitive**: even if granted, a confirmer must approve each use.
  - `interactive` — asks the human at the terminal
  - `auto-approve` / `auto-deny` — for unattended experiments and tests

---

## Agent Registry and capability-based dispatch (Stage 6)

AgentOS no longer relies on one fixed instance per agent type. Any number of instances of the
existing agent classes can be registered, and each task is matched to one of them at dispatch time.

### Capabilities vs permissions

| | Capabilities | Permissions |
|---|---|---|
| Meaning | what an agent **can do** | what an agent is **allowed to do** |
| Examples | `web_research`, `document_generation`, `file_read`, `file_write`, `email_send` | `llm_inference`, `write_file`, `read_file`, `send_email` |
| Declared on | `Agent(capabilities=...)` | `Agent(permissions=...)`, granted by the `PermissionManager` |
| Used by | the **dispatcher**, to find compatible instances | the **permission manager**, at execution time |

A capability is never an authorization. A task needing `email_send` can only be dispatched to an
email-capable instance, and that instance still needs the `send_email` grant **and** a human
confirmation before anything is sent. If a grant is revoked, the instance keeps its capability
but the action is denied at runtime.

### Agent Registry (`runtime/registry.py`)

- `register(agent)`: instance ids must be unique (`DuplicateAgentError` otherwise).
- `get(id)`, `list()`, `unregister(id)`: an unknown id raises `AgentNotFoundError`.
- `find(capabilities)`: instances that have **all** the listed capabilities.
- `set_available(id, False)`: takes an instance out of service; the dispatcher skips it.
- Per-instance metadata (type, availability, dispatch count, last dispatch) lives in the registry,
  separate from task data in the ACB.

The demo fleet is set up in `demo/fleet.py`, not in the kernel:

| Agent type | Instances | Capabilities |
|---|---:|---|
| ResearchAgent | `research-01..03` | `web_research` |
| DocGenAgent | `docgen-01..02` | `document_generation` |
| FileManagerAgent | `filemanager-01..02` | `file_read`, `file_write` |
| EmailAgent | `email-01..02` | `email_send` |

### Dispatcher (`runtime/dispatcher.py`)

An instance is **eligible** for a task when all of these hold:
1. it is registered;
2. it has every required capability;
3. it is available;
4. it already holds a grant for every permission the task declares. This is a read-only check;
   the real check, including confirmation, still happens at execution time.
5. it has not already been tried for this task (during recovery).

**Selection policy: least-recently-dispatched.** Among eligible instances, the one that was
dispatched longest ago (or never) wins, and ties go to registration order. It is deterministic
and spreads work evenly: with `research-01..03`, consecutive research tasks go to 01, 02, 03,
01, ... Because the policy uses dispatch history, adding or removing instances simply changes
who is eligible. When there is no eligible instance, `NoEligibleAgentError` explains each
compatible instance's rejection, and the kernel ends the task with outcome `no_agent`.

### Scheduler vs Dispatcher vs Kernel

| Component | Question it answers | Knows about | Must not |
|---|---|---|---|
| **Scheduler** | *Which queued task runs next?* | the queue of ACBs, the policy (FIFO / Priority / Aging) | look at agents or capabilities |
| **Dispatcher** | *Which instance should run this task?* | the registry, capabilities, availability, grants | see the queue or reorder tasks |
| **Kernel** | *How is the task assigned, authorized, executed, recovered and recorded?* | all components | contain per-agent-type rules |

Tests enforce this split. The scheduler module does not import the registry, dispatcher or
agents. The dispatcher's `select()` only receives requirements, never tasks or the queue.
Changing the policy changes the task order but not the matching rules, and registering another
instance changes which instance runs a task but not the order (`tests/test_registry_dispatch.py`).

### Recovery with multiple instances

1. **Retry** the same instance up to `max_retries` times.
2. **Reassign**:
   - first to the explicitly linked backup (`register(..., backup_for=...)`), **only if it is
     eligible** for this task;
   - otherwise to another instance chosen by the dispatcher (same capabilities, available,
     authorized, not tried yet).
   Each replacement gets the same retry budget.
3. **Escalate** when nothing compatible is left. Every instance is tried at most once per task,
   so recovery always ends.

Tasks pinned to an instance without declared capabilities are never moved onto an arbitrary
agent; only an explicit, eligible backup can take them over. The fixed workflow now has no
hard-coded backup. When `research-01` is down, the dispatcher hands the task to `research-02`.

### Sample output (`python main.py 6`)

```
=== 3. Scheduler picks the TASK (Priority policy), dispatcher picks the INSTANCE ===
  #  Task  Label       Priority  Needs                Eligible                             Dispatched to  Why
  1  T002  research B  1         web_research         research-01,research-02,research-03  research-01    least-recently-dispatched of 3 eligible
  2  T006  slides B    1         document_generation  docgen-01,docgen-02                  docgen-01      least-recently-dispatched of 2 eligible
  3  T004  research C  2         web_research         research-01,research-02,research-03  research-02    least-recently-dispatched of 3 eligible
  4  T003  slides A    3         document_generation  docgen-01,docgen-02                  docgen-02      least-recently-dispatched of 2 eligible
  5  T007  research E  4         web_research         research-01,research-02,research-03  research-03    least-recently-dispatched of 3 eligible
  6  T001  research A  5         web_research         research-01,research-02,research-03  research-01    least-recently-dispatched of 3 eligible
  7  T005  research D  5         web_research         research-01,research-02,research-03  research-02    least-recently-dispatched of 3 eligible
  Tasks per instance: research-01=2, research-02=2, research-03=1, docgen-01=1, docgen-02=1

=== 5. No suitable instance ===
  T001 'slides C': outcome=no_agent
      no eligible agent for capabilities [document_generation] (docgen-01: unavailable; docgen-02: unavailable; 7 other instance(s) lack the capability)
  T002 'translate': outcome=no_agent
      no eligible agent for capabilities [translation] (none of the 9 registered instances has it)

=== 6. research-01 is down: retry, then reassign to a compatible instance ===
  ...
  reassigned  research-01  {'to': 'research-02', 'via': 'dispatcher'}
  completed   research-02  {}
  => outcome=completed, instances tried: research-01 -> research-02
```

Section 4 of the demo reruns the same tasks under FIFO. The task order changes, and the
n-th dispatched task still goes to the same instance.

---

## Research with a local LLM (Ollama) — Stage 7

### What the ResearchAgent does

1. It receives the task (`{"topic": ..., "question": ..., "material": ...}`) through the normal
   AgentOS flow: scheduler, then dispatcher, then a `research-0N` instance.
2. It asks the permission manager for `llm_inference`, then sends a prompt to **`llama3:latest`**
   on your machine (`POST http://localhost:11434/api/chat`, non-streaming).
3. The prompt asks for a summary, key findings, supporting reasoning, conclusions, and
   assumptions & uncertainties. It also tells the model that it has **no web access**, must keep
   supplied facts separate from general knowledge and assumptions, and must not claim anything
   was verified.
4. The answer is split into those sections and returned as the task result, together with the
   model name, backend (`ollama`) and the raw text. `completed` / `failed` audit events record
   `llm_model` and `llm_backend`.
5. **DocGenAgent builds the slides from that result**: title, summary, key findings, reasoning,
   conclusions, assumptions & uncertainties, and sources & method. The email body is the
   summary. DocGen refuses a result without a summary and findings.
6. If the model fails (Ollama down, timeout, missing model, empty or malformed answer), the
   normal recovery applies: retry the instance, then reassign to another research instance,
   then escalate. The DocGen, File and Email steps are then **skipped**, so no presentation is
   made from failed research and nothing is emailed.

All research instances share one `OllamaClient` (`llm/ollama.py`, standard library only, no new
dependency). AgentOS **never falls back to fake output**: the mock LLM (`llm/mock.py`) is used only
when passed in explicitly (tests, `--llm mock`, the dispatch demo), and is labelled `mock` everywhere.

> **LLM analysis is not web research.** The model analyses the topic using what it learned
> during training, plus any `material` you supply in the task. It does not browse the web,
> fetch current facts or check sources, so its findings can be outdated or wrong. The deck's
> "Sources & method" slide says so. Live web search is a possible future extension, not part
> of this stage. (The capability is still named `web_research` so dispatch stays compatible.)

### Install and check Ollama

1. Install Ollama from https://ollama.com/download (Windows, macOS, Linux), then start the
   Ollama app (or run `ollama serve` in a terminal).
2. Check the installed models:
   ```bash
   ollama list
   ```
3. If `llama3:latest` is missing, download it (about 4.7 GB):
   ```bash
   ollama pull llama3
   ```
4. Check that the service is running. This should return JSON that lists your models:
   ```bash
   curl http://localhost:11434/api/tags
   ```
5. Try the model directly:
   ```bash
   ollama run llama3 "In two sentences, what does an OS scheduler do?"
   ```

### Configuration

Environment variables (all optional):

| Variable | Default | Meaning |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | where Ollama listens |
| `OLLAMA_MODEL` | `llama3:latest` | model used by every research instance |
| `OLLAMA_TIMEOUT` | `300` | seconds to wait for one answer |

For example, in PowerShell: `$env:OLLAMA_MODEL = "llama3:8b"`. Nothing machine-specific is stored
in the repository, and `.env` files are git-ignored.

### Run it

- `python main.py`, then option **1** or **2**. Before running, the CLI prints the research backend
  and whether it is reachable.
- `python -m demo.workflow --topic "Edge Computing in Agriculture"` runs the workflow from the
  command line.
- Dashboard: the **Workflow Runner** page shows whether the model is reachable, and each research
  card shows the model and backend that answered.

On a typical laptop one research answer takes **about 1–2 minutes**. In testing, `llama3:latest`
generated roughly 5 tokens per second; the first call also loads the model.

### Tests without Ollama, plus an optional live test

```bash
python -m pytest -q                              # never calls Ollama (a guard in tests/conftest.py blocks it)
```
The client is tested against a tiny fake Ollama HTTP server (success, connection refused,
timeout, HTTP 500, missing model, malformed and empty answers). Agent and workflow tests inject
`MockLLMClient`.

The live test runs only when you enable it (it takes 1–2 minutes):
```bash
# PowerShell:  $env:AGENTOS_LIVE_OLLAMA = "1"
# Git Bash:    export AGENTOS_LIVE_OLLAMA=1
python -m pytest tests/test_ollama_live.py -v -s
```

The scheduler benchmark never calls a model: it uses `SyntheticAgent` only, and a test checks this.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `Cannot reach Ollama at http://localhost:11434 ... Is Ollama running?` | Start the Ollama app or `ollama serve`; check `curl http://localhost:11434/api/tags`. |
| `Model 'llama3:latest' is not installed ... Run: ollama pull llama3:latest` | Run that command, or set `OLLAMA_MODEL` to a model shown by `ollama list`. |
| `did not answer within 300 s` | The machine is busy or the model is still loading: try again, close heavy apps, or raise `OLLAMA_TIMEOUT`. |
| Research `escalated` after 9 attempts and later steps `SKIPPED` | The model failed every time. The error in the audit log / dashboard says why. No deck or email is produced. |
| Slides contain `[MOCK]` text | You ran with `--llm mock` (or in tests). Run without it for real analysis. |
| Older decks in `output/reports/` | They come from earlier runs; each run overwrites only its own topic's file. Delete `output/` for a clean start. |

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
| **2** Workflow with forced failure | `research-01` (the dispatcher's first pick) is "down". AgentOS retries it twice, the dispatcher reassigns the task to compatible `research-02`, and the workflow still succeeds. |
| **3** 100-agent benchmark | Runs the same seeded workload under FIFO, Priority and Aging; prints a comparison table and saves `output/benchmark.png`. Takes ~5 s (the very first run is slower while matplotlib builds its font cache). |
| **4** Audit log + agents-used table | Full event log and per-task table of the last workflow from option 1 or 2; exported to `output/audit_log.json`. |
| **5** Runtime smoke demo | Five dummy agents covering every path: success, retry, reassign, escalate, permission denied. |
| **6** Registry & dispatcher demo | Uses an explicit **mock LLM**, so it runs in seconds. Shows the 9 registered instances, an eligibility report, scheduler-vs-dispatcher decisions under Priority and FIFO, the "no suitable instance" case, failover to a compatible instance, and escalation when none is left. |

Option 1's workflow is dispatched by capability: each step lists the instance that ran it.
Each option can also be run directly, e.g. `python main.py 3`.

### Other commands

```bash
python -m pytest -v                                # all tests (112 + 1 opt-in live test skipped, ~20 s, no Ollama needed)
python -m demo.registry_demo                       # same as menu option 6 (uses an explicit mock LLM)
python -m demo.workflow --llm mock                 # workflow with an explicit mock LLM (no Ollama needed)
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

Delete the `output` folder at any time for a clean start. (Menu option 6 also writes two small demo
decks to `output/registry_demo/`.)

### What the benchmark measures (and what it does not)

- It **measures scheduling**: queue wait, turnaround, completion, retries and escalations under FIFO,
  Priority and Aging, for the same seeded workload (100 tasks, 4 workers, seed 42, Poisson arrivals).
- It does **not** measure the dispatcher. Every synthetic task is pinned to its own `SyntheticAgent`,
  exactly as before Stage 6, so instance selection cannot affect the comparison. Each task now also
  emits `scheduled` and `dispatched` audit events. That is negligible overhead, and the results
  stayed within run-to-run timing noise.
- Instance utilization (`AgentOS.instance_usage()`) is shown in the registry demo instead. It would
  be meaningless here, because each benchmark agent runs exactly one task.

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
  runtime/   agent.py acb.py scheduler.py registry.py dispatcher.py permissions.py recovery.py
             audit.py sandbox.py kernel.py
  agents/    research.py docgen.py filemanager.py email_agent.py synthetic.py
  llm/       ollama.py (local Ollama client) mock.py (explicit test double)
  demo/      workflow.py benchmark.py fleet.py (demo instances) registry_demo.py
  dashboard/ app.py (entry) views.py (pages) jobs.py approval.py files.py ui.py
  tests/     test_runtime.py test_workflow.py test_benchmark.py test_dashboard.py
             test_registry_dispatch.py test_llm.py test_ollama_live.py (opt-in) conftest.py
  .streamlit/config.toml
  main.py  smoke_demo.py  requirements.txt  pytest.ini
```

## Extending AgentOS

**Add an agent**: subclass `Agent`, declare its capabilities and permissions, and implement `run()`.
No OS logic goes inside:

```python
from runtime import Agent, AgentOS

class SummariseAgent(Agent):
    def __init__(self, agent_id):
        super().__init__(agent_id, agent_id=agent_id,
                         capabilities=["summarise"], permissions=["read_file"])

    def run(self, task_input):
        path = self.sandbox_path(task_input["file"], "read_file")   # system call
        return path.read_text()[:200]

aos = AgentOS(confirm="interactive")
aos.register(SummariseAgent("summarise-01"))
aos.register(SummariseAgent("summarise-02"))          # more instances, no kernel change
acb = aos.run_task(None, {"file": "notes.txt"}, priority=2, required_capabilities=["summarise"])
print(acb.agent_id, acb.outcome, acb.result)          # summarise-01 completed ...
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
| ✓ | **Real LLM research** | Done in Stage 7: ResearchAgent uses local Ollama (`llama3:latest`). |
| 3 | **More LLM agents + web search** | Real web search/retrieval for research, LLM-written slide text and emails, token/time budgets as a scheduled resource. |
| ✓ | **Agent registry + capability dispatcher** | Done in Stage 6 (see above). Next: load-aware selection (skip busy instances), per-task permission policies. |
| ✓ | **Streamlit dashboard** | Done in Stage 5 (see *Web dashboard*). Next: stream events from the Event Bus instead of polling. |

## Known limitations (Phase 1)

- Research quality is that of an 8B local model with no web access. Findings can be outdated or
  wrong and are not verified.
- All research instances call the same local Ollama server. Reassigning after a model failure
  usually hits the same problem: it shows recovery working, but it doesn't add model redundancy.
  Worst case with the defaults: 3 instances × 3 attempts × `OLLAMA_TIMEOUT`, if every attempt times out.
- One generation at a time is fast enough for the demo workflow. Running many research tasks in
  parallel will queue inside Ollama.
- The slide text is the model's own wording, clipped to fit. Long answers are shortened on the slides
  (the full text stays in the task result as `raw_text`).

- Dispatcher selection is least-recently-dispatched only. It does not consider how busy an instance
  currently is, its speed, or past failures. Availability is an explicit on/off flag (`set_available`),
  not a health check.
- The dispatcher filters by the permissions a task **declares** (`required_permissions`). Permissions
  an agent requests only at runtime (such as the workflow's `send_email`) are checked when it runs,
  so an instance whose grant was revoked can still be selected and is then denied.
- Instance selection happens when the task starts, not when it is submitted, so the ACB shows
  `agent_id=""` while the task is queued.
- Stage numbering: the web dashboard was built as Stage 5, so this registry work is documented as
  Stage 6. The dashboard was only adjusted to keep working with dispatched steps; no new UI was added.

- Retries run inline on the same worker (no back-off queue); `retry_delay` is available but defaults to 0.
- `Agent.state` reflects the most recent task only; the per-task truth is in the ACB.
- Permissions are granted from the agent's declaration at registration; there is no policy file yet.
- The audit log is in memory and exported to JSON on demand.
