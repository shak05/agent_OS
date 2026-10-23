"""FileManagerAgent: copies or moves files, only within the sandbox."""
from __future__ import annotations

import os
import shutil
from typing import Any, Optional

from runtime.agent import Agent


class FileManagerAgent(Agent):
    """Input: {"src": "drafts/a.pptx", "dest": "reports/a.pptx", "mode": "move" | "copy"}."""

    def __init__(self, name: str = "FileManagerAgent", agent_id: Optional[str] = None) -> None:
        super().__init__(name, permissions=["read_file", "write_file"], agent_id=agent_id,
                         capabilities=["file_read", "file_write"])

    def run(self, task_input: dict[str, Any]) -> dict[str, Any]:
        mode = task_input.get("mode", "move")
        if mode not in {"move", "copy"}:
            raise ValueError(f"Unknown mode {mode!r}")
        src = self.sandbox_path(task_input["src"], "read_file")
        dest = self.sandbox_path(task_input["dest"], "write_file")
        if not src.is_file():
            raise FileNotFoundError(task_input["src"])

        dest.parent.mkdir(parents=True, exist_ok=True)
        if mode == "copy":
            shutil.copy2(src, dest)
        else:
            os.replace(src, dest)
        return {"path": task_input["dest"], "mode": mode, "bytes": dest.stat().st_size}
