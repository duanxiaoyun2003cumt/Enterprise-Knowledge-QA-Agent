"""Auditable execution traces for agent runs.

Traces contain action metadata and short observation summaries only. They never
contain API keys, request headers, or full document contents.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .schemas import FinalResult

TRACE_SCHEMA_VERSION = 1


@dataclass
class TraceRecorder:
    """Collect one run's decision steps and final result."""

    run_id: str
    model: str
    mode: str
    question: str
    max_steps: int
    max_tool_calls: int
    history_messages_included: int = 0
    _steps: list[dict[str, Any]] = field(default_factory=list, repr=False)
    _final: dict[str, Any] | None = field(default=None, repr=False)
    _stop_reason: str | None = field(default=None, repr=False)
    _tool_calls_made: int = field(default=0, repr=False)
    _format_repairs: int = field(default=0, repr=False)
    _final_validation_retries: int = field(default=0, repr=False)
    _format_failures: int = field(default=0, repr=False)
    _final_validation_failures: int = field(default=0, repr=False)

    def record_step(
        self,
        *,
        step: int,
        action_name: str | None,
        action_params: dict[str, Any] | None,
        action_validation: str,
        tool_name: str | None = None,
        tool_result_summary: str | None = None,
        tool_error_type: str | None = None,
        observation_ok: bool | None = None,
        validation_errors: list[str] | None = None,
        decision_note: str | None = None,
        repair_kind: str | None = None,
    ) -> None:
        """Append one actual decision/tool step to the trace."""
        self._steps.append(
            {
                "run_id": self.run_id,
                "step": step,
                "model": self.model,
                "action_name": action_name,
                "action_params": copy.deepcopy(action_params),
                "action_validation": action_validation,
                "tool_name": tool_name,
                "tool_result_summary": tool_result_summary,
                "tool_error_type": tool_error_type,
                "observation_ok": observation_ok,
                "validation_errors": list(validation_errors or []),
                "repair_kind": repair_kind,
                "next_action": None,
                "decision_note": decision_note,
            }
        )

    def finish(
        self,
        result: FinalResult,
        stop_reason: str,
        tool_calls_made: int = 0,
        format_repairs: int = 0,
        final_validation_retries: int = 0,
        format_failures: int = 0,
        final_validation_failures: int = 0,
    ) -> None:
        self._final = result.model_dump(mode="json")
        self._stop_reason = stop_reason
        self._tool_calls_made = tool_calls_made
        self._format_repairs = format_repairs
        self._final_validation_retries = final_validation_retries
        self._format_failures = format_failures
        self._final_validation_failures = final_validation_failures

    def to_dict(self) -> dict[str, Any]:
        steps = copy.deepcopy(self._steps)
        for index, event in enumerate(steps):
            if index + 1 < len(steps):
                event["next_action"] = (
                    steps[index + 1].get("action_name") or "invalid_action"
                )
            else:
                event["next_action"] = self._stop_reason

        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "run_id": self.run_id,
            "mode": self.mode,
            "model": self.model,
            "question": self.question,
            "history_messages_included": self.history_messages_included,
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "steps_used": len(steps),
            "tool_calls_made": self._tool_calls_made,
            "format_repairs": self._format_repairs,
            "format_failures": self._format_failures,
            "final_validation_retries": self._final_validation_retries,
            "final_validation_failures": self._final_validation_failures,
            "steps": steps,
            "final_result": copy.deepcopy(self._final),
            "stop_reason": self._stop_reason,
        }

    def save(self, directory: str | Path) -> Path:
        """Write the trace as UTF-8 JSON and return its path."""
        target_dir = Path(directory)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{self.run_id}.json"
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


__all__ = ["TRACE_SCHEMA_VERSION", "TraceRecorder"]
