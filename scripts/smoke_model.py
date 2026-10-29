#!/usr/bin/env python3
"""Manual smoke test for real-model connectivity and action-schema compliance.

Checks are reported separately:
  1. API request succeeded
  2. Response content is valid JSON
  3. Response validates against the AgentAction schema

A full pass requires all three *and* a raw (non-Markdown-wrapped) JSON response
from the requested JSON mode. Exit codes: 0 = full pass, 1 = failure or
compatibility warning, 2 = missing configuration (no request sent).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from pydantic import ValidationError  # noqa: E402

from knowledge_agent.config import ConfigurationError, Settings  # noqa: E402
from knowledge_agent.llm_client import (  # noqa: E402
    LLMAPIError,
    LLMClient,
    LLMClientError,
)
from knowledge_agent.schemas import FinishAction, parse_agent_action  # noqa: E402

SYSTEM_PROMPT = """你是企业知识问答 Agent 的动作规划器。你的输出必须是一个 JSON 对象，表示下一步动作。
可用动作（只能选一个）：
1. search_documents: {"action": "search_documents", "query": "非空字符串", "top_k": 1到5的整数, "decision_note": "简短依据"}
2. read_document: {"action": "read_document", "doc_id": "非空字符串", "decision_note": "简短依据"}
3. ask_user: {"action": "ask_user", "question": "非空字符串", "reason": "简短原因", "decision_note": "简短依据"}
4. finish: {"action": "finish", "status": "answered|needs_clarification|insufficient_evidence|tool_error|incomplete", "answer": "非空字符串", "citations": [{"doc_id": "非空字符串", "quote": "非空字符串"}], "gaps": ["非空字符串"], "decision_note": "简短依据"}
要求：
- 只输出一个 json 对象，不要 Markdown 代码块，不要额外解释。
- 所有字符串必须非空；top_k 必须在 1 到 5 之间。
- decision_note 只记录可审查的简短动作依据，不要输出详细思维过程。"""

USER_PROMPT = (
    "员工提问：试用期员工可以休年假吗？当前尚未检索任何文档，"
    "请判断下一步动作并只输出一个 JSON 对象。"
)

_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify real-model connectivity and AgentAction JSON compatibility."
    )
    parser.add_argument(
        "--no-json-mode",
        action="store_true",
        help=(
            "Do not send response_format={'type': 'json_object'}. Use only to "
            "diagnose providers that reject JSON mode; a run with this flag "
            "cannot fully verify structured-output compatibility."
        ),
    )
    return parser.parse_args()


def _preview(text: str, limit: int = 800) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "…"


def _decode_response_json(text: str) -> tuple[Any, str | None]:
    """Decode raw JSON, falling back to a single Markdown fenced block.

    The fallback is reported as a compatibility caveat; it is not treated as
    raw structured output.
    """
    try:
        return json.loads(text), None
    except json.JSONDecodeError as original_error:
        match = _FENCED_JSON_RE.search(text)
        if not match:
            raise original_error
        return (
            json.loads(match.group(1)),
            "Response JSON was wrapped in a Markdown code fence, not returned as raw JSON.",
        )


def _format_validation_errors(exc: ValidationError) -> list[str]:
    lines: list[str] = []
    for error in exc.errors(include_url=False)[:8]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        lines.append(f"{location}: {error.get('msg')} [{error.get('type')}]")
    return lines


def _summary(api: str, json_check: str, schema: str) -> None:
    print(f"Summary: API={api} | JSON={json_check} | SCHEMA={schema}")


def main() -> int:
    args = _parse_args()

    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        print(f"[CONFIG ERROR] {exc}")
        print("No API request was sent.")
        return 2

    print(f"Configured model: {settings.model}")
    print(f"Request timeout: {settings.timeout:g}s")

    client = LLMClient(settings)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": USER_PROMPT},
    ]

    compatibility_notes: list[str] = []
    json_mode_requested = not args.no_json_mode

    try:
        if json_mode_requested:
            try:
                response = client.chat(
                    messages, response_format={"type": "json_object"}
                )
            except LLMAPIError as exc:
                if exc.status_code not in (400, 422):
                    raise
                compatibility_notes.append(
                    "Provider rejected response_format={'type': 'json_object'} "
                    f"with HTTP {exc.status_code}; retried once without JSON mode."
                )
                print(f"[WARN] {compatibility_notes[-1]}")
                response = client.chat(messages)
        else:
            compatibility_notes.append(
                "JSON mode was disabled by --no-json-mode, so structured-output "
                "compatibility was not verified."
            )
            response = client.chat(messages)
    except LLMClientError as exc:
        print(f"[FAIL] API request failed: {exc}")
        _summary("FAIL", "NOT CHECKED", "NOT CHECKED")
        return 1

    print(
        f"[PASS] API request succeeded "
        f"(model={response.model}, {response.latency_ms:.0f} ms)"
    )

    try:
        payload, fence_note = _decode_response_json(response.text)
    except json.JSONDecodeError as exc:
        print(
            f"[FAIL] Response is not valid JSON: {exc.msg} "
            f"at line {exc.lineno}, column {exc.colno}."
        )
        print(f"Raw response preview: {_preview(response.text)}")
        _summary("PASS", "FAIL", "NOT CHECKED")
        return 1

    if fence_note is not None:
        compatibility_notes.append(fence_note)
        print(f"[WARN] {fence_note}")
        json_check = "PASS (markdown-wrapped)"
    else:
        print("[PASS] Response content is raw JSON.")
        json_check = "PASS"

    try:
        action = parse_agent_action(payload)
    except ValidationError as exc:
        print("[FAIL] Response does not match the AgentAction schema:")
        for line in _format_validation_errors(exc):
            print(f"  - {line}")
        print(
            "Action payload preview: "
            f"{_preview(json.dumps(payload, ensure_ascii=False))}"
        )
        _summary("PASS", json_check, "FAIL")
        return 1

    print(f"[PASS] Response matches the AgentAction schema (action={action.action}).")
    if isinstance(action, FinishAction):
        print(f"       finish status: {action.status.value}")
    _summary("PASS", json_check, "PASS")

    if compatibility_notes:
        print(
            "Overall: PASS WITH COMPATIBILITY WARNINGS — all three content "
            "checks passed, but structured-output compatibility is not fully "
            "verified:"
        )
        for note in compatibility_notes:
            print(f"  - {note}")
        return 1

    print(
        "Overall: PASS — API request, valid JSON, and AgentAction schema "
        "checks all passed in JSON mode."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
