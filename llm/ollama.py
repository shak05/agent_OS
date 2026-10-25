"""Minimal client for a local Ollama server (https://ollama.com).

Uses only the Python standard library and Ollama's local HTTP API
(POST /api/chat, non-streaming). No cloud service or API key is involved.

Configuration (environment variables, all optional):
    OLLAMA_BASE_URL   default http://localhost:11434
    OLLAMA_MODEL      default llama3:latest
    OLLAMA_TIMEOUT    seconds per request, default 300
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

DEFAULT_BASE_URL = "http://localhost:11434"
DEFAULT_MODEL = "llama3:latest"
DEFAULT_TIMEOUT = 300.0  # a full analysis on a laptop CPU/GPU can take minutes


# ------------------------------------------------------------------ errors
class LLMError(RuntimeError):
    """Base class for language-model failures (handled by AgentOS recovery)."""


class OllamaConnectionError(LLMError):
    """The Ollama server could not be reached."""


class OllamaTimeoutError(LLMError):
    """Ollama did not answer within the configured timeout."""


class OllamaHTTPError(LLMError):
    """Ollama answered with an HTTP error status."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class OllamaModelNotFoundError(OllamaHTTPError):
    """The requested model is not installed in Ollama."""


class OllamaResponseError(LLMError):
    """Ollama answered, but the response was malformed or empty."""


# ------------------------------------------------------------------ data
@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> "OllamaConfig":
        """Read OLLAMA_BASE_URL / OLLAMA_MODEL / OLLAMA_TIMEOUT, falling back to defaults."""
        timeout_raw = os.environ.get("OLLAMA_TIMEOUT", "").strip()
        try:
            timeout = float(timeout_raw) if timeout_raw else DEFAULT_TIMEOUT
        except ValueError:
            raise ValueError(f"OLLAMA_TIMEOUT must be a number of seconds, got {timeout_raw!r}") from None
        return cls(
            base_url=(os.environ.get("OLLAMA_BASE_URL") or DEFAULT_BASE_URL).rstrip("/"),
            model=os.environ.get("OLLAMA_MODEL") or DEFAULT_MODEL,
            timeout=timeout,
        )


@dataclass(frozen=True)
class LLMResponse:
    """One generated answer and where it came from."""

    content: str
    model: str
    backend: str  # "ollama" for real inference, "mock" for test doubles
    duration_s: float
    eval_count: Optional[int] = None  # tokens generated, if reported


# ------------------------------------------------------------------ client
class OllamaClient:
    """Thread-safe, stateless client; one instance can be shared by many agents."""

    backend = "ollama"

    def __init__(self, config: Optional[OllamaConfig] = None) -> None:
        self.config = config or OllamaConfig.from_env()

    @property
    def model(self) -> str:
        return self.config.model

    def describe(self) -> str:
        return f"Ollama {self.config.model} at {self.config.base_url}"

    def chat(self, messages: list[dict[str, str]], options: Optional[dict[str, Any]] = None) -> LLMResponse:
        """Send a non-streaming chat request and return the assistant's message."""
        payload: dict[str, Any] = {"model": self.config.model, "messages": messages, "stream": False}
        if options:
            payload["options"] = options
        started = time.perf_counter()
        data = self._post("/api/chat", payload, self.config.timeout)
        if "error" in data:
            raise OllamaResponseError(f"Ollama returned an error: {data['error']}")
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise OllamaResponseError(
                f"Malformed Ollama response: expected message.content, got keys {sorted(data)}")
        if not content.strip():
            raise OllamaResponseError(f"Ollama model {self.config.model} returned an empty response")
        return LLMResponse(
            content=content.strip(),
            model=str(data.get("model") or self.config.model),
            backend=self.backend,
            duration_s=time.perf_counter() - started,
            eval_count=data.get("eval_count"),
        )

    def list_models(self, timeout: float = 5.0) -> list[str]:
        """Names of installed models (GET /api/tags)."""
        data = self._request("GET", "/api/tags", None, timeout)
        models = data.get("models")
        if not isinstance(models, list):
            raise OllamaResponseError("Malformed /api/tags response from Ollama")
        return [m.get("name", "") for m in models if isinstance(m, dict)]

    def health(self, timeout: float = 3.0) -> tuple[bool, str]:
        """(ok, message): is the server reachable and the model installed? Never raises."""
        try:
            names = self.list_models(timeout)
        except LLMError as exc:
            return False, str(exc)
        if self.config.model not in names:
            return False, (f"Ollama is running at {self.config.base_url}, but model "
                           f"'{self.config.model}' is not installed. Run: ollama pull {self.config.model}")
        return True, f"{self.describe()} is reachable"

    # -------------------------------------------------------------- HTTP
    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self._request("POST", path, payload, timeout)

    def _request(self, method: str, path: str, payload: Optional[dict[str, Any]], timeout: float) -> dict[str, Any]:
        url = self.config.base_url + path
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(url, data=body, method=method,
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._http_error(exc) from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, (socket.timeout, TimeoutError)):
                raise self._timeout(timeout) from None
            raise OllamaConnectionError(
                f"Cannot reach Ollama at {self.config.base_url} ({exc.reason}). "
                "Is Ollama running? Start the Ollama app or run `ollama serve`."
            ) from None
        except (socket.timeout, TimeoutError):
            raise self._timeout(timeout) from None
        except (ConnectionError, OSError) as exc:  # e.g. connection reset mid-response
            raise OllamaConnectionError(f"Connection to Ollama at {self.config.base_url} failed: {exc}") from None
        try:
            data = json.loads(raw)
        except ValueError:
            raise OllamaResponseError(f"Ollama returned non-JSON data from {path}: {raw[:120]!r}") from None
        if not isinstance(data, dict):
            raise OllamaResponseError(f"Ollama returned unexpected JSON from {path}: {type(data).__name__}")
        return data

    def _timeout(self, timeout: float) -> OllamaTimeoutError:
        return OllamaTimeoutError(
            f"Ollama ({self.config.model}) did not answer within {timeout:g} s. The model may still be "
            "loading or the machine is busy; try again or raise OLLAMA_TIMEOUT."
        )

    def _http_error(self, exc: urllib.error.HTTPError) -> OllamaHTTPError:
        try:
            detail = json.loads(exc.read() or b"{}").get("error", "")
        except (ValueError, AttributeError, OSError):
            detail = ""
        if exc.code == 404 and "not found" in str(detail).lower():
            return OllamaModelNotFoundError(
                f"Model '{self.config.model}' is not installed in Ollama. "
                f"Run: ollama pull {self.config.model}", exc.code)
        return OllamaHTTPError(f"Ollama HTTP {exc.code}: {detail or exc.reason}", exc.code)
