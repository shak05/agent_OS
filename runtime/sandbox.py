"""Filesystem sandbox: every agent file path must stay inside one root folder."""
from __future__ import annotations

from pathlib import Path
from typing import Union

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


class SandboxViolationError(ValueError):
    """A path resolved to somewhere outside the sandbox root."""


class Sandbox:
    """Maps relative paths into a root folder and rejects escapes (../, absolute paths)."""

    def __init__(self, root: Union[str, Path] = DEFAULT_OUTPUT_DIR) -> None:
        self.root = Path(root).resolve()

    def resolve(self, relative: Union[str, Path]) -> Path:
        """Return the absolute path for `relative`, or raise SandboxViolationError."""
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root):
            raise SandboxViolationError(f"{relative!s} is outside the sandbox {self.root}")
        return path

    def relative(self, path: Union[str, Path]) -> str:
        """Display form of a sandbox path, e.g. 'reports/ai.pptx'."""
        return Path(path).resolve().relative_to(self.root).as_posix()
