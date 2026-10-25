"""ResearchAgent: LLM-powered analysis of a topic using a local Ollama model.

This is analysis, not web research: the agent sends the topic (and any
source material supplied in the task) to the model. It does not search the
web, fetch pages or verify facts, and its output says so.
"""
from __future__ import annotations

import random
import re
import time
from typing import TYPE_CHECKING, Any, Optional

from runtime.agent import Agent

if TYPE_CHECKING:
    from llm import LLMClient

SYSTEM_PROMPT = """You are a careful research analyst inside an automated reporting pipeline.
You have NO internet access: you did not search the web and cannot verify facts.
Base your analysis only on your general knowledge and on any source material provided.
Clearly separate facts taken from the provided material, general background knowledge, and assumptions.
Never claim that anything was independently verified. If you are unsure, say so.
Answer in plain text using exactly these section headings, each on its own line:

SUMMARY:
(2-4 sentences)

KEY FINDINGS:
- (3-6 bullet points, one sentence each)

REASONING:
- (2-4 bullet points explaining how you reached the findings)

CONCLUSIONS:
- (2-3 bullet points)

ASSUMPTIONS AND UNCERTAINTIES:
- (2-4 bullet points: what you assumed, what may be outdated or wrong)"""

NO_SOURCES_NOTE = ("No external sources were consulted: this is local LLM analysis "
                   "without web access, and it has not been independently verified.")

SECTION_KEYS = {
    "summary": "summary",
    "key findings": "key_points",
    "findings": "key_points",
    "reasoning": "reasoning",
    "supporting reasoning": "reasoning",
    "conclusions": "conclusions",
    "conclusion": "conclusions",
    "assumptions and uncertainties": "assumptions",
    "assumptions & uncertainties": "assumptions",
    "assumptions": "assumptions",
    "uncertainties": "assumptions",
}
_HEADING = re.compile(r"^\s*(?:#+\s*)?\**\s*([A-Za-z &]+?)\s*\**\s*:?\s*\**\s*$")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def build_prompt(topic: str, question: Optional[str] = None, material: Any = None) -> str:
    """User message for the model: the task plus any supplied source material."""
    lines = [f"Topic: {topic}"]
    if question:
        lines.append(f"Question: {question}")
    if material:
        text = "\n\n".join(material) if isinstance(material, (list, tuple)) else str(material)
        lines += ["", "Source material supplied with the task (treat as the only external facts):",
                  "<<<", text.strip(), ">>>"]
    else:
        lines += ["", "No source material was supplied. Rely on general knowledge, label it as such, "
                      "and flag anything that may be outdated."]
    lines += ["", "Write the analysis now, using the required section headings."]
    return "\n".join(lines)


def parse_analysis(text: str) -> dict[str, Any]:
    """Split the model's answer into sections. Tolerates markdown and missing headings."""
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _HEADING.match(line)
        key = SECTION_KEYS.get(heading.group(1).strip().lower()) if heading else None
        if key is None:  # "SUMMARY: text on the same line"
            head, _, rest = line.partition(":")
            rest = rest.strip().lstrip("*").strip()  # "**Summary:** text"
            key = SECTION_KEYS.get(head.strip(" *#").lower()) if rest else None
            if key:
                sections.setdefault(key, []).append(rest)
                current = key
                continue
        if key:
            current = key
            sections.setdefault(key, [])
            continue
        sections.setdefault(current or "preamble", []).append(_BULLET.sub("", line).strip("* "))

    summary = " ".join(sections.get("summary", []))
    key_points = sections.get("key_points", [])
    if not summary and not key_points:  # model ignored the format: keep its text
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        summary = paragraphs[0] if paragraphs else text.strip()
        key_points = [_BULLET.sub("", p).strip() for p in paragraphs[1:6]]
    return {
        "summary": summary or (key_points[0] if key_points else ""),
        "key_points": key_points,
        "reasoning": sections.get("reasoning", []),
        "conclusions": sections.get("conclusions", []),
        "assumptions": sections.get("assumptions", []),
    }


class ResearchAgent(Agent):
    """Analyses a topic with a language model (local Ollama by default).

    Input: a topic string, or {"topic": ..., "question": ..., "material": ...}
    where `material` is optional source text the model should analyse.

    Fault injection for recovery demos (applied before the model is called):
      failure_rate: probability (0..1) that a call fails.
      fail_first:   the first N calls always fail.
    """

    def __init__(
        self,
        name: str = "ResearchAgent",
        delay: float = 0.0,
        failure_rate: float = 0.0,
        fail_first: int = 0,
        seed: Optional[int] = None,
        agent_id: Optional[str] = None,
        llm: Optional["LLMClient"] = None,
    ) -> None:
        super().__init__(name, permissions=["llm_inference"], agent_id=agent_id,
                         capabilities=["web_research"])
        if llm is None:
            from llm import OllamaClient

            llm = OllamaClient()
        self.llm = llm
        self.delay = delay  # extra simulated latency, normally 0
        self.failure_rate = failure_rate
        self.fail_first = fail_first
        self.calls = 0
        self._rng = random.Random(seed)

    def run(self, task_input: Any) -> dict[str, Any]:
        data = task_input if isinstance(task_input, dict) else {"topic": task_input}
        topic = str(data.get("topic") or "").strip()
        if not topic:
            raise ValueError("ResearchAgent needs a non-empty 'topic'")
        material = data.get("material")

        self.request_permission("llm_inference", f"model={self.llm.model} topic={topic!r}")
        self.annotate(llm_model=self.llm.model, llm_backend=self.llm.backend)
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls <= self.fail_first or self._rng.random() < self.failure_rate:
            raise ConnectionError("injected failure: research instance is down (demo fault)")

        prompt = build_prompt(topic, data.get("question"), material)
        reply = self.llm.chat([{"role": "system", "content": SYSTEM_PROMPT},
                               {"role": "user", "content": prompt}])
        analysis = parse_analysis(reply.content)
        if not analysis["summary"]:
            raise ValueError(f"{reply.backend} model {reply.model} returned no usable analysis")
        self.annotate(llm_model=reply.model, llm_backend=reply.backend)

        supplied = material if isinstance(material, (list, tuple)) else ([material] if material else [])
        return {
            "topic": topic,
            **analysis,
            "sources": [f"Supplied material #{i + 1}" for i in range(len(supplied))] or [NO_SOURCES_NOTE],
            "raw_text": reply.content,
            "model": reply.model,
            "backend": reply.backend,
            "duration_s": round(reply.duration_s, 2),
            "researched_by": self.name,
        }
