"""Safe, read-only access to generated files for download.

Only files inside the AgentOS output sandbox, with an allowed extension, can
be listed or read. Path checks reuse runtime.sandbox.Sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Union

from runtime.sandbox import DEFAULT_OUTPUT_DIR, Sandbox, SandboxViolationError

MIME_TYPES = {
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json",
    ".png": "image/png",
}
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


class UnsafeFileError(ValueError):
    """Requested file is outside the sandbox, the wrong type, or missing."""


@dataclass(frozen=True)
class OutputFile:
    relative: str  # e.g. "reports/ai-agents-in-healthcare.pptx"
    category: str  # "Presentation" | "Email outbox" | "Audit log" | "Benchmark"
    size: int
    modified: datetime

    @property
    def name(self) -> str:
        return Path(self.relative).name

    @property
    def mime(self) -> str:
        return MIME_TYPES[Path(self.relative).suffix.lower()]


def _category(relative: str) -> str:
    name = Path(relative).name
    if name.endswith(".pptx"):
        return "Presentation"
    if name == "sent_emails.json":
        return "Email outbox"
    if name.endswith("audit_log.json"):
        return "Audit log"
    if name.startswith("benchmark"):
        return "Benchmark"
    return "Other"


class OutputFiles:
    """Lists and reads downloadable files in the output sandbox."""

    def __init__(self, root: Union[str, Path] = DEFAULT_OUTPUT_DIR) -> None:
        self.sandbox = Sandbox(root)

    def list(self, category: str = "") -> list[OutputFile]:
        """All allowed files (newest first), optionally filtered by category."""
        if not self.sandbox.root.is_dir():
            return []
        items = []
        for path in self.sandbox.root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in MIME_TYPES:
                continue
            try:
                rel = self.sandbox.relative(self.sandbox.resolve(path.relative_to(self.sandbox.root)))
            except (SandboxViolationError, ValueError):
                continue  # e.g. a symlink pointing outside the sandbox
            cat = _category(rel)
            if cat == "Other" or (category and cat != category):
                continue
            stat = path.stat()
            items.append(OutputFile(rel, cat, stat.st_size, datetime.fromtimestamp(stat.st_mtime)))
        return sorted(items, key=lambda f: f.modified, reverse=True)

    def read(self, relative: str) -> bytes:
        """Return file bytes, or raise UnsafeFileError."""
        try:
            path = self.sandbox.resolve(relative)
        except SandboxViolationError as exc:
            raise UnsafeFileError(str(exc)) from None
        if path.suffix.lower() not in MIME_TYPES:
            raise UnsafeFileError(f"{relative}: file type not allowed")
        if not path.is_file():
            raise UnsafeFileError(f"{relative}: not found")
        if path.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise UnsafeFileError(f"{relative}: file too large")
        return path.read_bytes()
