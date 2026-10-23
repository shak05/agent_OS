"""Small presentation helpers for the dashboard (no AgentOS logic here)."""
from __future__ import annotations

from typing import Any, Iterable, Optional

import pandas as pd
import streamlit as st

# status -> (badge color, Material icon). Icon + label, never color alone.
STATUS_STYLE: dict[str, tuple[str, str]] = {
    "completed": ("green", ":material/check_circle:"),
    "finished": ("green", ":material/check_circle:"),
    "running": ("blue", ":material/progress_activity:"),
    "awaiting approval": ("orange", ":material/front_hand:"),
    "queued": ("gray", ":material/schedule:"),
    "not started": ("gray", ":material/radio_button_unchecked:"),
    "pending": ("gray", ":material/radio_button_unchecked:"),
    "skipped": ("gray", ":material/skip_next:"),
    "escalated": ("red", ":material/error:"),
    "denied": ("red", ":material/block:"),
    "rejected": ("red", ":material/cancel:"),
    "failed": ("red", ":material/error:"),
    "incomplete": ("orange", ":material/warning:"),
}

EVENT_CATEGORY = {
    "submitted": "Lifecycle", "started": "Lifecycle", "completed": "Lifecycle",
    "failed": "Recovery", "retried": "Recovery", "reassigned": "Recovery", "escalated": "Recovery",
    "permission_allowed": "Permission", "permission_denied": "Permission",
    "permission_confirmed": "Permission", "permission_rejected": "Permission",
}

CSS = """
<style>
.block-container { padding-top: 2.2rem; max-width: 1280px; }
.aos-hero h1 { margin-bottom: 0.1rem; padding-bottom: 0.25rem; }
.aos-hero p { font-size: 1.05rem; opacity: 0.8; margin-top: 0; }
.aos-step-arrow { text-align: center; font-size: 1.4rem; opacity: 0.45; padding-top: 2.2rem; }
div[data-testid="stMetricValue"] { font-size: 1.6rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def hero(title: str, subtitle: str) -> None:
    st.markdown(f'<div class="aos-hero"><h1>{title}</h1><p>{subtitle}</p></div>', unsafe_allow_html=True)


def status_badge(status: str) -> None:
    color, icon = STATUS_STYLE.get(status, ("gray", ":material/help:"))
    st.badge(status.upper(), icon=icon, color=color)


def fmt_seconds(value: Optional[float], digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f} s"


def events_dataframe(events: Iterable[dict[str, Any]]) -> pd.DataFrame:
    """Audit events (as dicts) -> display table."""
    rows = []
    for e in events:
        details = e.get("details") or {}
        rows.append({
            "Time": str(e.get("timestamp", ""))[11:23],
            "Category": EVENT_CATEGORY.get(e.get("event", ""), "Other"),
            "Event": e.get("event", ""),
            "Task": e.get("task_id") or "-",
            "Agent": e.get("agent") or "-",
            "Details": "  ".join(f"{k}={v}" for k, v in details.items()),
        })
    return pd.DataFrame(rows, columns=["Time", "Category", "Event", "Task", "Agent", "Details"])
