"""Language-model clients used by agents (local Ollama, plus an explicit mock for tests)."""
from typing import Any, Optional, Protocol

from llm.mock import MockLLMClient
from llm.ollama import (
    LLMError,
    LLMResponse,
    OllamaClient,
    OllamaConfig,
    OllamaConnectionError,
    OllamaHTTPError,
    OllamaModelNotFoundError,
    OllamaResponseError,
    OllamaTimeoutError,
)


class LLMClient(Protocol):
    """What agents need from a language-model client."""

    backend: str
    model: str

    def chat(self, messages: list[dict[str, str]], options: Optional[dict[str, Any]] = None) -> LLMResponse: ...

    def describe(self) -> str: ...

    def health(self, timeout: float = ...) -> tuple[bool, str]: ...


__all__ = [
    "LLMClient", "LLMError", "LLMResponse", "MockLLMClient", "OllamaClient", "OllamaConfig",
    "OllamaConnectionError", "OllamaHTTPError", "OllamaModelNotFoundError",
    "OllamaResponseError", "OllamaTimeoutError",
]
