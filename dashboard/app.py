"""AgentOS Streamlit dashboard (entry point).

Launch from the agentos/ folder:
    streamlit run dashboard/app.py

The dashboard is a view + controls only. Scheduling, permissions, recovery
and auditing all happen inside the existing AgentOS runtime; workflows and
benchmarks run on background threads (dashboard/jobs.py) so the page can
show real progress and collect human approvals while agents are paused.
Pages live in dashboard/views.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:  # `streamlit run` only adds dashboard/ to the path
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

st.set_page_config(page_title="AgentOS Dashboard", page_icon=":material/memory:", layout="wide")

from dashboard import views  # noqa: E402
from dashboard.ui import inject_css  # noqa: E402

inject_css()
nav = st.navigation(list(views.PAGES.values()))
with st.sidebar:
    st.markdown("### :material/memory: AgentOS")
views.sidebar()
nav.run()
