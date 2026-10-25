"""Deterministic stand-in for an LLM client, for tests and dispatch-only demos.

It never contacts a model. Every response is labelled backend="mock" so it
can never be mistaken for real Ollama output. Use it only when you pass it
in explicitly; AgentOS never falls back to it on its own.
"""
from __future__ import annotations

import threading
from typing import Any, Callable, Optional, Union

from llm.ollama import LLMResponse

MOCK_MODEL = "mock-llm"


def default_mock_answer(prompt: str) -> str:
    topic = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                  if line.lower().startswith("topic:")), "the topic")
    return f"""SUMMARY:
[MOCK LLM OUTPUT] Deterministic placeholder analysis of {topic}, produced without a language model.

KEY FINDINGS:
- [MOCK] {topic}: first placeholder finding.
- [MOCK] {topic}: second placeholder finding.

REASONING:
- [MOCK] Placeholder reasoning.

CONCLUSIONS:
- [MOCK] Placeholder conclusion.

ASSUMPTIONS AND UNCERTAINTIES:
- [MOCK] Not real analysis; used for tests and dispatch demos only."""


class MockLLMClient:
    """Returns a fixed or generated answer, or raises a configured error.

    response: a string, or a function(prompt) -> string. Defaults to a
              structured placeholder that echoes the topic.
    error:    raise this exception instead of answering.
    fail_times: raise `error` only for the first N calls, then answer.
    """

    backend = "mock"

    def __init__(
        self,
        response: Union[str, Callable[[str], str], None] = None,
        error: Optional[Exception] = None,
        fail_times: Optional[int] = None,
        model: str = MOCK_MODEL,
    ) -> None:
        self.response = response if response is not None else default_mock_answer
        self.error = error
        self.fail_times = fail_times
        self.model = model
        self.calls: list[list[dict[str, str]]] = []
        self._lock = threading.Lock()

    def describe(self) -> str:
        return f"mock LLM ({self.model}, no model is called)"

    def chat(self, messages: list[dict[str, str]], options: Optional[dict[str, Any]] = None) -> LLMResponse:
        with self._lock:
            self.calls.append(messages)
            n = len(self.calls)
        if self.error is not None and (self.fail_times is None or n <= self.fail_times):
            raise self.error
        prompt = messages[-1]["content"] if messages else ""
        content = self.response(prompt) if callable(self.response) else self.response
        return LLMResponse(content=content, model=self.model, backend=self.backend, duration_s=0.0)

    def health(self, timeout: float = 0.0) -> tuple[bool, str]:
        return True, self.describe()
