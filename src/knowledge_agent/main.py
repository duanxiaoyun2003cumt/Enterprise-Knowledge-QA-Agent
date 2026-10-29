"""Minimal CLI for the Enterprise Knowledge QA Agent.

Uses the real configured model only. There is no silent fallback to a fake
model, and API keys are never printed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .agent import (
    MAX_STEPS,
    MAX_TOOL_CALLS,
    AgentInputError,
    KnowledgeAgent,
)
from .config import ConfigurationError
from .schemas import FinalResult, TaskStatus

DEFAULT_TRACE_DIR = "examples/traces"

_SUCCESS_STATUSES = {
    TaskStatus.ANSWERED,
    TaskStatus.NEEDS_CLARIFICATION,
    TaskStatus.INSUFFICIENT_EVIDENCE,
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Enterprise Knowledge QA Agent against the local knowledge base."
    )
    parser.add_argument(
        "question",
        nargs="?",
        help="User question. If omitted, the CLI prompts for one.",
    )
    parser.add_argument(
        "--history-file",
        help="Optional JSON file containing a list of {role, content} messages.",
    )
    parser.add_argument(
        "--trace-dir",
        default=DEFAULT_TRACE_DIR,
        help=f"Directory for the JSON trace (default: {DEFAULT_TRACE_DIR}).",
    )
    parser.add_argument(
        "--no-save-trace",
        action="store_true",
        help="Do not write a trace file.",
    )
    parser.add_argument(
        "--show-trace",
        action="store_true",
        help="Print the trace JSON after the result.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help=f"Maximum model decision steps (default: {MAX_STEPS}).",
    )
    parser.add_argument(
        "--max-tool-calls",
        type=int,
        default=MAX_TOOL_CALLS,
        help=f"Maximum tool executions (default: {MAX_TOOL_CALLS}).",
    )
    return parser


def _load_history(path: str | None) -> list[dict[str, Any]] | None:
    if path is None:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise AgentInputError(f"无法读取历史文件 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise AgentInputError(
            f"历史文件 {path} 不是合法 JSON：{exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    if not isinstance(payload, list):
        raise AgentInputError("历史文件必须包含一个 JSON 数组。")
    return payload


def _print_result(result: FinalResult) -> None:
    print(f"status: {result.status.value}")
    print(f"answer: {result.answer}")

    if result.citations:
        print("citations:")
        for citation in result.citations:
            print(f"  - {citation.doc_id}: {citation.quote}")
    else:
        print("citations: []")

    if result.gaps:
        print("gaps:")
        for gap in result.gaps:
            print(f"  - {gap}")
    else:
        print("gaps: []")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    question = args.question
    if question is None:
        try:
            question = input("问题: ").strip()
        except EOFError:
            question = ""

    if not question or not question.strip():
        print("[INPUT ERROR] question must be a non-empty string.", file=sys.stderr)
        return 2

    try:
        history = _load_history(args.history_file)
        agent = KnowledgeAgent(
            max_steps=args.max_steps,
            max_tool_calls=args.max_tool_calls,
        )
        result = agent.run(question, history)
    except ConfigurationError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2
    except (AgentInputError, ValueError) as exc:
        print(f"[INPUT ERROR] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("[INTERRUPTED]", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary must stay sanitized
        print(
            f"[ERROR] {type(exc).__name__}: 未预期错误，运行已终止。",
            file=sys.stderr,
        )
        return 1

    trace_path: Path | None = None
    if not args.no_save_trace:
        try:
            trace_path = agent.save_trace(args.trace_dir)
        except OSError as exc:
            print(f"[TRACE ERROR] 无法保存 Trace：{exc}", file=sys.stderr)

    _print_result(result)
    if trace_path is not None:
        print(f"trace: {trace_path}")
        if args.show_trace:
            print(trace_path.read_text(encoding="utf-8"))

    return 0 if result.status in _SUCCESS_STATUSES else 1


if __name__ == "__main__":
    raise SystemExit(main())
