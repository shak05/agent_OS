"""AgentOS demo menu.

Run from the agentos/ folder:
    python main.py        # interactive menu
    python main.py 2      # run one option directly (1-6), then exit
"""
from __future__ import annotations

import sys
from typing import Callable, Optional

from demo.benchmark import format_results, run_benchmark, save_chart, save_results
from demo.workflow import run_demo
from runtime import AgentOS

BANNER = r"""
    _                    _    ___  ____
   / \   __ _  ___ _ __ | |_ / _ \/ ___|
  / _ \ / _` |/ _ \ '_ \| __| | | \___ \
 / ___ \ (_| |  __/ | | | |_| |_| |___) |
/_/   \_\__, |\___|_| |_|\__|\___/|____/
        |___/   a runtime layer for AI agents
"""

MENU = """
  1) Run the 4-agent workflow (Research -> DocGen -> FileManager -> Email)
  2) Run the workflow with a forced failure (shows retry + reassignment)
  3) Run the 100-agent scheduling benchmark (FIFO vs Priority vs Aging)
  4) Show the audit log and agents-used table of the last workflow
  5) Run the runtime smoke demo (5 dummy agents, every recovery path)
  6) Registry & dispatcher demo (9 agent instances, capability matching)
  0) Exit
"""

last_aos: Optional[AgentOS] = None


def ask_yes_no(question: str, default: bool = True) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    answer = input(f"{question} {hint} ").strip().lower()
    return default if not answer else answer in {"y", "yes"}


def option_workflow() -> None:
    global last_aos
    interactive = ask_yes_no("Approve the email yourself (human-in-the-loop)?")
    last_aos, _ = run_demo(confirm="interactive" if interactive else "auto-approve")


def option_forced_failure() -> None:
    global last_aos
    print("research-01 (the first research instance the dispatcher picks) is DOWN. AgentOS will retry it,\n"
          "then the dispatcher reassigns the task to another compatible research instance.")
    last_aos, _ = run_demo(fail="reassign", confirm="auto-approve")


def option_benchmark() -> None:
    print("=== Benchmark: 100 tasks, 4 workers, seed 42 (takes a few seconds) ===")
    results = run_benchmark()
    print()
    print(format_results(results))
    print("\nTimes in seconds. HighWait = priority 1-2, LowWait = priority 9-10.")
    print(f"Metrics saved to {save_results(results, tasks=100, workers=4, seed=42)}")
    print(f"Chart saved to {save_chart(results)}")


def option_show_log() -> None:
    if last_aos is None:
        print("No workflow has run in this session yet. Run option 1 or 2 first.")
        return
    print("=== Audit log ===")
    last_aos.audit.print_log()
    print("\n=== Agents used ===")
    last_aos.print_agents_used()
    print(f"\nExported to {last_aos.export_log()}")


def option_smoke() -> None:
    import smoke_demo

    smoke_demo.main()


def option_registry() -> None:
    from demo import registry_demo

    registry_demo.main()


OPTIONS: dict[str, Callable[[], None]] = {
    "1": option_workflow,
    "2": option_forced_failure,
    "3": option_benchmark,
    "4": option_show_log,
    "5": option_smoke,
    "6": option_registry,
}


def main(argv: list[str]) -> None:
    if argv:  # one-shot mode, e.g. `python main.py 3`
        if argv[0] not in OPTIONS:
            sys.exit(f"Unknown option {argv[0]!r}; choose 1-6.")
        OPTIONS[argv[0]]()
        return

    print(BANNER)
    while True:
        print(MENU)
        try:
            choice = input("Select an option: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice == "0":
            return
        action = OPTIONS.get(choice)
        if action is None:
            print("Please choose 0-6.")
            continue
        print()
        try:
            action()
            input("\nPress Enter to return to the menu...")
        except (EOFError, KeyboardInterrupt):
            print("\nCancelled.")


if __name__ == "__main__":
    main(sys.argv[1:])
