"""Shared FakeLLM and payload builders for phase 3 tests.

ScriptedLLM is a test double only. It does not provide evidence that a real
model chooses actions dynamically; real-model runs are recorded separately.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from knowledge_agent.agent import KnowledgeAgent
from knowledge_agent.llm_client import LLMResponse
from knowledge_agent.tools import read_document


class ScriptedLLM:
    """Return pre-scripted responses and record every model call."""

    model = "fake-scripted-llm"

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(
            {
                "messages": copy.deepcopy(messages),
                "response_format": copy.deepcopy(response_format),
                "temperature": temperature,
            }
        )
        if not self.responses:
            raise AssertionError("ScriptedLLM has no remaining responses")

        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        return LLMResponse(text=text, model=self.model, latency_ms=0.0)


def action(name: str, **fields: Any) -> dict[str, Any]:
    return {
        "action": name,
        "decision_note": "测试决策依据",
        **fields,
    }


def search_action(query: str, top_k: int = 3) -> dict[str, Any]:
    return action("search_documents", query=query, top_k=top_k)


def read_action(doc_id: str) -> dict[str, Any]:
    return action("read_document", doc_id=doc_id)


def ask_action(
    question: str, reason: str = "需要用户澄清"
) -> dict[str, Any]:
    return action("ask_user", question=question, reason=reason)


def finish_action(
    status: str,
    answer: str,
    citations: list[dict[str, str]] | None = None,
    gaps: list[str] | None = None,
) -> dict[str, Any]:
    return action(
        "finish",
        status=status,
        answer=answer,
        citations=citations if citations is not None else [],
        gaps=gaps if gaps is not None else [],
    )


def doc_content(doc_id: str) -> str:
    return read_document(doc_id)["content"]


def cite(doc_id: str) -> dict[str, str]:
    return {"doc_id": doc_id, "quote": doc_content(doc_id)}


def make_agent(
    responses: list[Any], **kwargs: Any
) -> tuple[KnowledgeAgent, ScriptedLLM]:
    fake = ScriptedLLM(responses)
    kwargs.setdefault("trace_mode", "fake")
    return KnowledgeAgent(client=fake, **kwargs), fake


def last_observation(fake: ScriptedLLM, call_index: int = -1) -> str:
    return fake.calls[call_index]["messages"][-1]["content"]


def call_text(fake: ScriptedLLM, call_index: int) -> str:
    return json.dumps(
        fake.calls[call_index]["messages"], ensure_ascii=False
    )
