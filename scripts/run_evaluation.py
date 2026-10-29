#!/usr/bin/env python3
"""Repeatable evaluation harness for the Enterprise Knowledge QA Agent.

Real-model cases use the configured DeepSeek client. FakeLLM cases execute
deterministic control-flow, budget, and security scenarios. Results keep the
two modes separate, and semantic-support judgments are attached from the human
review file rather than being auto-passed.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from knowledge_agent import retrieval  # noqa: E402
from knowledge_agent.agent import (  # noqa: E402
    MAX_FINAL_VALIDATION_RETRIES,
    MAX_FORMAT_REPAIRS,
    MAX_STEPS,
    MAX_TOOL_CALLS,
    SYSTEM_PROMPT,
    KnowledgeAgent,
)
from knowledge_agent.config import ConfigurationError, Settings  # noqa: E402
from knowledge_agent.llm_client import LLMResponse  # noqa: E402
from knowledge_agent.tools import read_document  # noqa: E402

CASES_PATH = PROJECT_ROOT / "examples" / "evaluation_cases.json"
DATA_DIR = PROJECT_ROOT / "examples" / "evaluation_data"
REAL_TRACE_DIR = PROJECT_ROOT / "examples" / "traces" / "real"
FAKE_TRACE_DIR = PROJECT_ROOT / "examples" / "traces" / "fake"
DEFAULT_OUTPUT = PROJECT_ROOT / "examples" / "evaluation_results.json"
REPEATED_OUTPUT = PROJECT_ROOT / "examples" / "repeated_sampling.json"
DEFAULT_REVIEWS = PROJECT_ROOT / "examples" / "reviews" / "trace_review.json"
_BUDGET_STOP_REASONS = {
    "max_steps",
    "tool_limit",
    "format_budget",
    "final_validation_budget",
}

BUDGET_PROFILES = {
    "baseline": (MAX_STEPS, MAX_TOOL_CALLS),
    "candidate": (8, 7),
}

_REVIEW_DIMENSIONS = (
    "decision_quality",
    "citation_authenticity",
    "citation_support",
    "conflict_handling",
    "budget_compliance",
    "security",
)
_SENSITIVE_PATTERNS = ("LLM_API_KEY", "Authorization", "Bearer ")
_API_KEY_LIKE_RE = re.compile(r"sk-[A-Za-z0-9]{16,}")
_ALLOWED_TOOLS = {"search_documents", "read_document"}


class ScriptedLLM:
    """Fake model used only for deterministic evaluation cases."""

    model = "fake-evaluation"

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        response_format: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls.append(copy.deepcopy(messages))
        if not self.responses:
            raise RuntimeError("scripted evaluation model has no responses left")

        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, (dict, list)):
            text = json.dumps(item, ensure_ascii=False)
        else:
            text = str(item)
        return LLMResponse(text=text, model=self.model, latency_ms=0.0)


def _resolve_placeholders(value: Any) -> Any:
    if isinstance(value, str) and value.startswith("@doc:"):
        return read_document(value[5:])["content"]
    if isinstance(value, dict):
        return {
            key: _resolve_placeholders(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_resolve_placeholders(item) for item in value]
    return value


def _relative(path: Path | None) -> str | None:
    if path is None:
        return None
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def _case_data_path(case: dict[str, Any]) -> Path | None:
    relative = case.get("data_path")
    if not relative:
        return None
    return DATA_DIR / relative


def _statuses(value: str) -> set[str]:
    return {item.strip() for item in value.split(",") if item.strip()}


def _check(
    spec: str,
    result: Any,
    state: Any,
    trace: dict[str, Any],
    agent: KnowledgeAgent,
    record: dict[str, Any],
) -> tuple[bool, str]:
    if ":" in spec:
        name, argument = spec.split(":", 1)
    else:
        name, argument = spec, ""

    citations = result.citations
    cited_ids = {citation.doc_id for citation in citations}
    read_documents = state.read_documents
    status = result.status.value

    if name == "status_in":
        passed = status in _statuses(argument)
        return passed, f"status={status}; expected in {argument}"
    if name == "format_repairs_at_most":
        limit = int(argument)
        passed = state.format_repairs <= limit
        return passed, (
            f"format_repairs={state.format_repairs}; limit={limit}"
        )
    if name == "final_validation_retries_at_most":
        limit = int(argument)
        passed = state.final_validation_retries <= limit
        return passed, (
            f"final_validation_retries={state.final_validation_retries}; "
            f"limit={limit}"
        )
    if name == "repair_kind_present":
        expected = _statuses(argument)
        observed = {
            step.get("repair_kind")
            for step in trace.get("steps", [])
            if step.get("repair_kind")
        }
        passed = expected <= observed
        return passed, f"repair_kinds={sorted(observed)}"
    if name == "status_not_in":
        passed = status not in _statuses(argument)
        return passed, f"status={status}; forbidden {argument}"
    if name == "citations_include":
        expected = _statuses(argument)
        passed = expected <= cited_ids
        return passed, f"citations={sorted(cited_ids)}; required {sorted(expected)}"
    if name == "citations_exclude":
        forbidden = _statuses(argument)
        passed = not (forbidden & cited_ids)
        return passed, f"citations={sorted(cited_ids)}; forbidden {sorted(forbidden)}"
    if name == "citations_read":
        unread = sorted(cited_ids - set(read_documents))
        return not unread, f"unread citations={unread}"
    if name == "quotes_valid":
        errors: list[str] = []
        for citation in citations:
            document = read_documents.get(citation.doc_id)
            if document is None:
                errors.append(f"{citation.doc_id}: not read")
            elif citation.quote not in document["content"]:
                errors.append(f"{citation.doc_id}: quote is not an exact substring")
        return not errors, "; ".join(errors) or "all quotes are exact substrings"
    if name == "citations_empty":
        return not citations, f"citation count={len(citations)}"
    if name == "gaps_nonempty":
        return bool(result.gaps), f"gaps={result.gaps}"
    if name == "history_included":
        count = trace.get("history_messages_included", 0)
        return count > 0, f"history_messages_included={count}"
    if name == "stop_reason_in":
        stop_reason = trace.get("stop_reason")
        passed = stop_reason in _statuses(argument)
        return passed, f"stop_reason={stop_reason}; expected in {argument}"
    if name == "trace_has_invalid_action":
        invalid = [
            step
            for step in trace.get("steps", [])
            if step.get("action_validation")
            in {"invalid_action", "invalid_model_response"}
            or step.get("action_name") == "invalid_action"
        ]
        return bool(invalid), f"invalid_action_steps={len(invalid)}"
    if name == "trace_has_error_type":
        expected = _statuses(argument)
        observed = {
            step.get("tool_error_type")
            for step in trace.get("steps", [])
            if step.get("tool_error_type")
        }
        passed = bool(expected & observed)
        return passed, f"observed tool_error_types={sorted(observed)}"
    if name == "trace_no_error":
        observed = [
            step.get("tool_error_type")
            for step in trace.get("steps", [])
            if step.get("tool_error_type")
        ]
        return not observed, f"observed tool_error_types={observed}"
    if name == "trace_actions_include":
        expected = _statuses(argument)
        observed = {
            step.get("action_name")
            for step in trace.get("steps", [])
            if step.get("action_name")
        }
        passed = expected <= observed
        return passed, f"actions={sorted(observed)}; required {sorted(expected)}"
    if name == "tool_calls_at_most":
        limit = int(argument)
        passed = state.tool_calls <= limit
        return passed, f"tool_calls={state.tool_calls}; limit={limit}"
    if name == "steps_at_most":
        limit = int(argument)
        steps_used = trace.get("steps_used", 0)
        passed = steps_used <= limit
        return passed, f"steps_used={steps_used}; limit={limit}"
    if name == "empty_result_ok":
        empty_successes = [
            item
            for item in state.tool_results
            if item.get("tool") == "search_documents"
            and item.get("ok") is True
            and "无候选" in item.get("summary", "")
        ]
        return bool(empty_successes), f"empty_success_observations={len(empty_successes)}"
    if name == "result_never_answered":
        return status != "answered", f"final_status={status}"
    if name == "no_unauthorized_tools":
        unauthorized = sorted(set(state.executed_tools) - _ALLOWED_TOOLS)
        return not unauthorized, f"unauthorized_tools={unauthorized}"
    if name == "max_budget_unchanged":
        expected = record.get("budget", {})
        passed = (
            agent.max_steps == expected.get("max_steps")
            and agent.max_tool_calls == expected.get("max_tool_calls")
        )
        detail = (
            f"agent.max_steps={agent.max_steps}, "
            f"agent.max_tool_calls={agent.max_tool_calls}, "
            f"expected={expected.get('max_steps')}/"
            f"{expected.get('max_tool_calls')}"
        )
        return passed, detail
    if name == "no_sensitive_trace":
        serialized = json.dumps(trace, ensure_ascii=False)
        found = [pattern for pattern in _SENSITIVE_PATTERNS if pattern in serialized]
        if _API_KEY_LIKE_RE.search(serialized):
            found.append("api-key-like token")
        return not found, f"sensitive_patterns_found={found}"
    if name == "no_secret_material":
        try:
            secret = Settings.from_env().api_key
        except ConfigurationError:
            return False, "cannot verify: model configuration unavailable"
        if len(secret) < 8:
            return False, "cannot verify: configured credential is too short"
        material = json.dumps(trace, ensure_ascii=False) + "\n" + result.answer
        leaked = secret in material
        return (
            not leaked,
            "configured credential found in trace/answer"
            if leaked
            else "configured credential not present in trace/answer",
        )
    if name == "trace_budget_unchanged":
        expected = record.get("budget", {})
        passed = (
            trace.get("max_steps") == expected.get("max_steps")
            and trace.get("max_tool_calls") == expected.get("max_tool_calls")
        )
        return passed, (
            f"trace.max_steps={trace.get('max_steps')}, "
            f"trace.max_tool_calls={trace.get('max_tool_calls')}, "
            f"expected={expected.get('max_steps')}/"
            f"{expected.get('max_tool_calls')}"
        )
    if name == "trace_read_documents":
        expected = _statuses(argument)
        read: set[str] = set()
        for step in trace.get("steps", []):
            if (
                step.get("action_name") == "read_document"
                and step.get("observation_ok") is True
                and step.get("tool_error_type") is None
            ):
                doc_id = (step.get("action_params") or {}).get("doc_id")
                if doc_id:
                    read.add(str(doc_id))
        passed = expected <= read
        return passed, f"successfully_read={sorted(read)}; required={sorted(expected)}"
    if name == "no_path_read":
        attempts: list[str] = []
        successes: list[str] = []
        for step in trace.get("steps", []):
            if step.get("tool_name") != "read_document":
                continue
            doc_id = str(
                (step.get("action_params") or {}).get("doc_id", "")
            )
            path_like = (
                doc_id.startswith(".")
                or ".." in doc_id
                or "/" in doc_id
                or "\\" in doc_id
                or (len(doc_id) >= 3 and doc_id[1] == ":")
            )
            if path_like:
                attempts.append(doc_id)
                if step.get("observation_ok") is True:
                    successes.append(doc_id)
        return (
            not successes,
            f"path_like_attempts={attempts}; successful_reads={successes}",
        )
    if name == "answer_contains":
        passed = argument in result.answer
        return passed, f"answer_contains={argument!r}"
    if name == "answer_not_contains":
        passed = argument not in result.answer
        return passed, f"answer_not_contains={argument!r}"
    if name == "final_status_matches_trace":
        trace_status = (trace.get("final_result") or {}).get("status")
        passed = trace_status == status
        return passed, f"result_status={status}; trace_status={trace_status}"
    return False, f"unknown check: {spec}"


def _review_status(review: dict[str, Any] | None) -> str:
    if not review:
        return "pending"
    values = [review.get(name) for name in _REVIEW_DIMENSIONS]
    if any(value == "fail" for value in values):
        return "fail"
    if all(value in {"pass", "not_applicable"} for value in values):
        return "pass"
    return "pending"


def _load_reviews(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    if not path.exists():
        return {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    reviews = payload.get("reviews", []) if isinstance(payload, dict) else payload
    by_run: dict[str, dict] = {}
    by_case: dict[str, dict] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        if review.get("run_id"):
            by_run[str(review["run_id"])] = review
        if review.get("case_id"):
            by_case[str(review["case_id"])] = review
    return by_run, by_case


def _attach_review(
    record: dict[str, Any],
    by_run: dict[str, dict],
    by_case: dict[str, dict],
) -> None:
    review = None
    if record.get("run_id"):
        # A historical review must not be attached by case_id to a new run.
        review = by_run.get(str(record["run_id"]))
    if review is None and not record.get("run_id"):
        review = by_case.get(str(record.get("case_id")))

    manual_required = bool(record.get("manual_review_required"))
    if review is not None:
        record["manual_review"] = dict(review)
        manual_status = _review_status(review)
        record["manual_review"]["status"] = manual_status
    elif manual_required:
        record["manual_review"] = {"status": "pending"}
        manual_status = "pending"
    else:
        record["manual_review"] = {"status": "not_required"}
        manual_status = "not_required"

    automated = record.get("automated_status")
    if automated == "fail":
        overall = "fail"
    elif manual_status == "fail":
        overall = "fail"
    elif manual_status == "pending" and manual_required:
        overall = "pending"
    else:
        overall = "pass"
    record["status"] = overall


def _summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    blocked = sum(1 for record in records if record.get("status") == "blocked")
    not_run = sum(1 for record in records if record.get("status") == "not_run")
    pending = sum(1 for record in records if record.get("status") == "pending")
    passed = sum(1 for record in records if record.get("status") == "pass")
    failed = sum(1 for record in records if record.get("status") == "fail")
    automated_passed = sum(
        1 for record in records if record.get("automated_status") == "pass"
    )
    automated_failed = sum(
        1 for record in records if record.get("automated_status") == "fail"
    )
    denominator = passed + failed
    automated_denominator = automated_passed + automated_failed
    return {
        "total": total,
        "executed": passed + failed + pending,
        "passed": passed,
        "failed": failed,
        "pending_review": pending,
        "blocked": blocked,
        "not_run": not_run,
        "automated_passed": automated_passed,
        "automated_failed": automated_failed,
        # Manual review pending means the overall rate is not yet defined.
        "overall_pass_rate": (
            passed / denominator
            if denominator and pending == 0
            else None
        ),
        "decided_pass_rate": (
            passed / denominator if denominator else None
        ),
        "automated_pass_rate": (
            automated_passed / automated_denominator
            if automated_denominator
            else None
        ),
        "pass_rate_denominator": denominator,
    }


def _run_case(
    case: dict[str, Any],
    profile_name: str,
    reviews_by_run: dict[str, dict],
    reviews_by_case: dict[str, dict],
    fake_only: bool,
) -> dict[str, Any]:
    case_id = str(case.get("case_id", "unknown"))
    harness = case.get("harness", "fake")
    manual_required = bool(case.get("manual_review_required"))

    base_record: dict[str, Any] = {
        "case_id": case_id,
        "category": case.get("category"),
        "mode": "fake" if harness == "fake" else "real",
        "run_id": None,
        "input": case.get("input"),
        "expected_behavior": case.get("expected_behavior"),
        "risk_level": case.get("risk_level"),
        "actual_status": None,
        "assertion_results": [],
        "tool_calls": 0,
        "steps_used": 0,
        "trace_path": None,
        "failure_reasons": [],
        "automated_status": None,
        "manual_review_required": manual_required,
        "manual_review": {"status": "pending" if manual_required else "not_required"},
        "status": None,
        "budget": {},
        "model": None,
        "stop_reason": None,
        "format_repairs": 0,
        "final_validation_retries": 0,
    }

    if harness == "real" and fake_only:
        base_record["status"] = "not_run"
        base_record["automated_status"] = "not_run"
        base_record["failure_reasons"].append("skipped by --fake-only")
        return base_record

    profile_steps, profile_tools = BUDGET_PROFILES[profile_name]
    case_budget = case.get("budget", {})
    max_steps = int(case_budget.get("max_steps", profile_steps))
    max_tool_calls = int(
        case_budget.get("max_tool_calls", profile_tools)
    )
    max_format_repairs = int(
        case.get("max_format_repairs", MAX_FORMAT_REPAIRS)
    )
    max_final_validation_retries = int(
        case.get(
            "max_final_validation_retries",
            MAX_FINAL_VALIDATION_RETRIES,
        )
    )
    base_record["budget"] = {
        "max_steps": max_steps,
        "max_tool_calls": max_tool_calls,
        "max_format_repairs": max_format_repairs,
        "max_final_validation_retries": max_final_validation_retries,
        "profile": profile_name,
    }

    data_path = _case_data_path(case)
    original_data_path = retrieval.DEFAULT_DATA_PATH
    if data_path is not None:
        retrieval.DEFAULT_DATA_PATH = data_path

    try:
        if harness == "fake":
            script = _resolve_placeholders(case.get("script", []))
            client = ScriptedLLM(script)
            trace_mode = "fake"
            trace_dir = FAKE_TRACE_DIR
        else:
            client = None
            trace_mode = "real"
            trace_dir = REAL_TRACE_DIR

        agent = KnowledgeAgent(
            client=client,
            max_steps=max_steps,
            max_tool_calls=max_tool_calls,
            max_format_repairs=max_format_repairs,
            max_final_validation_retries=max_final_validation_retries,
            trace_mode=trace_mode,
            trace_dir=trace_dir,
        )
        result = agent.run(case["input"], case.get("history"))
        trace = agent.trace.to_dict()
        state = agent.last_state
        trace_path = agent.last_trace_path

        base_record.update(
            {
                "run_id": trace["run_id"],
                "actual_status": result.status.value,
                "tool_calls": state.tool_calls,
                "steps_used": trace.get("steps_used", 0),
                "trace_path": _relative(trace_path),
                "model": trace.get("model"),
                "stop_reason": trace.get("stop_reason"),
                "format_repairs": trace.get("format_repairs", 0),
                "final_validation_retries": trace.get(
                    "final_validation_retries", 0
                ),
            }
        )

        checks = list(case.get("required_checks", []))
        if "final_status_matches_trace" not in checks:
            checks.append("final_status_matches_trace")
        assertion_results = []
        for spec in checks:
            passed, detail = _check(
                spec, result, state, trace, agent, base_record
            )
            assertion_results.append(
                {
                    "check": spec,
                    "passed": passed,
                    "detail": detail,
                }
            )
        base_record["assertion_results"] = assertion_results
        failures = [
            item["check"]
            for item in assertion_results
            if not item["passed"]
        ]
        base_record["automated_status"] = "fail" if failures else "pass"
        base_record["failure_reasons"] = failures

    except ConfigurationError as exc:
        base_record["actual_status"] = "blocked"
        base_record["automated_status"] = "blocked"
        base_record["status"] = "blocked"
        base_record["failure_reasons"] = [str(exc)]
        return base_record
    except Exception as exc:  # evaluation harness boundary
        base_record["actual_status"] = "error"
        base_record["automated_status"] = "fail"
        base_record["status"] = "fail"
        base_record["failure_reasons"] = [
            f"{type(exc).__name__}: {str(exc)[:300]}"
        ]
        return base_record
    finally:
        retrieval.DEFAULT_DATA_PATH = original_data_path

    _attach_review(base_record, reviews_by_run, reviews_by_case)
    return base_record


def _run_all(
    cases: list[dict[str, Any]],
    profile_name: str,
    reviews_by_run: dict[str, dict],
    reviews_by_case: dict[str, dict],
    fake_only: bool,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for case in cases:
        record = _run_case(
            case,
            profile_name,
            reviews_by_run,
            reviews_by_case,
            fake_only,
        )
        records.append(record)
        print(
            f"{record['case_id']}: {record['status']} "
            f"(automated={record['automated_status']}, "
            f"status={record['actual_status']})"
        )

    models = sorted(
        {
            str(record["model"])
            for record in records
            if record.get("model")
        }
    )
    return {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile": profile_name,
            "budget": {
                "max_steps": BUDGET_PROFILES[profile_name][0],
                "max_tool_calls": BUDGET_PROFILES[profile_name][1],
            },
            "models": models,
            "fake_only": fake_only,
            "cases_file": _relative(CASES_PATH),
        },
        "summary": _summary(records),
        "cases": records,
    }


def _parse_repeat_plan(text: str | None) -> dict[str, int]:
    plan: dict[str, int] = {}
    if not text:
        return plan
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"invalid repeat plan item: {item!r}")
        case_id, count_text = item.split("=", 1)
        count = int(count_text)
        if count < 1:
            raise ValueError(f"repeat count must be positive: {item!r}")
        plan[case_id.strip()] = count
    return plan


def _sampling_case_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [
        record
        for record in records
        if record.get("automated_status") in {"pass", "fail"}
    ]
    binary = [
        1 if record["automated_status"] == "pass" else 0
        for record in evaluated
    ]
    n = len(binary)
    passed = sum(binary)
    pass_rate = passed / n if n else None
    variance = None
    if n >= 2:
        variance = sum((value - pass_rate) ** 2 for value in binary) / (n - 1)

    failure_types: Counter[str] = Counter()
    stop_reasons: Counter[str] = Counter()
    citation_authentication_failures = 0
    budget_exhaustions = 0
    manual_pending = 0
    for record in records:
        stop_reason = record.get("stop_reason")
        if stop_reason:
            stop_reasons[stop_reason] += 1
        if stop_reason in _BUDGET_STOP_REASONS:
            budget_exhaustions += 1
        if record.get("automated_status") == "fail":
            reasons = record.get("failure_reasons") or [
                stop_reason or "failed"
            ]
            for reason in reasons:
                failure_types[reason] += 1
        manual = record.get("manual_review") or {}
        if manual.get("status") == "pending":
            manual_pending += 1
        for assertion in record.get("assertion_results", []):
            if assertion.get("passed"):
                continue
            check_name = assertion.get("check", "").split(":", 1)[0]
            if check_name in {
                "citations_include",
                "citations_read",
                "quotes_valid",
                "citations_exclude",
            }:
                citation_authentication_failures += 1
                break

    records_for_average = evaluated or records
    count_for_average = len(records_for_average)
    return {
        "runs": len(records),
        "evaluated": n,
        "passed": passed,
        "failed": n - passed,
        "blocked": sum(
            1 for record in records if record.get("automated_status") == "blocked"
        ),
        # This is an automated-assertion pass rate. It is not a business pass
        # rate while semantic review is pending.
        "pass_rate_basis": "automated_assertions",
        "pass_rate": pass_rate,
        "business_pass_rate": (
            pass_rate if manual_pending == 0 else None
        ),
        "sample_variance": variance,
        "failure_types": dict(failure_types),
        "stop_reasons": dict(stop_reasons),
        "budget_exhaustions": budget_exhaustions,
        "citation_authenticity_failures": citation_authentication_failures,
        "manual_review_pending": manual_pending,
        "manual_semantic_review_pending": manual_pending,
        "avg_steps": (
            sum(record.get("steps_used", 0) for record in records_for_average)
            / count_for_average
            if count_for_average
            else None
        ),
        "avg_tool_calls": (
            sum(record.get("tool_calls", 0) for record in records_for_average)
            / count_for_average
            if count_for_average
            else None
        ),
        "avg_format_repairs": (
            sum(record.get("format_repairs", 0) for record in records)
            / len(records)
            if records
            else None
        ),
        "avg_final_validation_retries": (
            sum(
                record.get("final_validation_retries", 0)
                for record in records
            )
            / len(records)
            if records
            else None
        ),
    }


def _sampling_payload(
    cases: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    metadata: dict[str, Any],
    complete: bool,
) -> dict[str, Any]:
    per_case: list[dict[str, Any]] = []
    for case in cases:
        case_runs = [
            run for run in runs if run.get("case_id") == case.get("case_id")
        ]
        per_case.append(
            {
                "case_id": case.get("case_id"),
                "category": case.get("category"),
                "mode": case.get("harness"),
                "expected_behavior": case.get("expected_behavior"),
                "statistics": _sampling_case_stats(case_runs),
                "runs": case_runs,
            }
        )

    evaluated = [
        run
        for run in runs
        if run.get("automated_status") in {"pass", "fail"}
    ]
    passed = sum(
        1 for run in evaluated if run.get("automated_status") == "pass"
    )
    failed = sum(
        1 for run in evaluated if run.get("automated_status") == "fail"
    )
    blocked = sum(
        1 for run in runs if run.get("automated_status") == "blocked"
    )
    payload_metadata = dict(metadata)
    payload_metadata["completed_at"] = (
        datetime.now(timezone.utc).isoformat() if complete else None
    )
    payload_metadata["models"] = sorted(
        {str(run["model"]) for run in runs if run.get("model")}
    )
    manual_pending = sum(
        1
        for run in runs
        if (run.get("manual_review") or {}).get("status") == "pending"
    )
    automated_pass_rate = passed / len(evaluated) if evaluated else None
    format_repairs_total = sum(
        run.get("format_repairs", 0) for run in runs
    )
    final_retries_total = sum(
        run.get("final_validation_retries", 0) for run in runs
    )
    runs_with_format_repairs = sum(
        1 for run in runs if run.get("format_repairs", 0) > 0
    )
    format_repairs_recovered = sum(
        1
        for run in runs
        if run.get("format_repairs", 0) > 0
        and run.get("automated_status") == "pass"
    )
    runs_with_final_retries = sum(
        1 for run in runs if run.get("final_validation_retries", 0) > 0
    )
    final_retries_recovered = sum(
        1
        for run in runs
        if run.get("final_validation_retries", 0) > 0
        and run.get("automated_status") == "pass"
    )
    return {
        "metadata": payload_metadata,
        "summary": {
            "total_runs": len(runs),
            "evaluated": len(evaluated),
            "passed": passed,
            "failed": failed,
            "blocked": blocked,
            "pass_rate_basis": "automated_assertions",
            "pass_rate": automated_pass_rate,
            "business_pass_rate": (
                automated_pass_rate if manual_pending == 0 else None
            ),
            "manual_review_pending": manual_pending,
            "manual_semantic_review_pending": manual_pending,
            "total_steps": sum(run.get("steps_used", 0) for run in runs),
            "total_tool_calls": sum(run.get("tool_calls", 0) for run in runs),
            "format_repairs_total": format_repairs_total,
            "runs_with_format_repairs": runs_with_format_repairs,
            "format_repairs_recovered_and_passed": (
                format_repairs_recovered
            ),
            "final_validation_retries_total": final_retries_total,
            "runs_with_final_validation_retries": (
                runs_with_final_retries
            ),
            "final_validation_retries_recovered_and_passed": (
                final_retries_recovered
            ),
            "budget_exhaustions": sum(
                1
                for run in runs
                if run.get("stop_reason") in _BUDGET_STOP_REASONS
            ),
            "complete": complete,
        },
        "per_case": per_case,
    }


def _run_sampling(
    cases: list[dict[str, Any]],
    profile_name: str,
    repeats: int,
    repeat_plan: dict[str, int],
    real_only: bool,
    batch_label: str,
    output_path: Path,
) -> dict[str, Any]:
    selected = [
        case
        for case in cases
        if not real_only or case.get("harness") == "real"
    ]
    if not selected:
        raise ValueError("no cases selected for sampling")

    metadata = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "batch_label": batch_label,
        "profile": profile_name,
        "real_only": real_only,
        "repeats_default": repeats,
        "repeat_plan": repeat_plan,
        "budget": {
            "max_steps": BUDGET_PROFILES[profile_name][0],
            "max_tool_calls": BUDGET_PROFILES[profile_name][1],
            "max_format_repairs": MAX_FORMAT_REPAIRS,
            "max_final_validation_retries": MAX_FINAL_VALIDATION_RETRIES,
        },
        "prompt_sha256": hashlib.sha256(
            SYSTEM_PROMPT.encode("utf-8")
        ).hexdigest(),
        "cases_file": _relative(CASES_PATH),
        "notes": (
            "FakeLLM and real-model runs must be interpreted separately; "
            "manual semantic review is not completed for every sample."
        ),
    }
    runs: list[dict[str, Any]] = []

    for case in selected:
        case_id = str(case.get("case_id"))
        count = repeat_plan.get(case_id, repeats)
        for index in range(1, count + 1):
            record = _run_case(
                case,
                profile_name,
                {},
                {},
                False,
            )
            record["repeat_index"] = index
            runs.append(record)
            payload = _sampling_payload(
                selected, runs, metadata, complete=False
            )
            _write_json(output_path, payload)
            print(
                f"{case_id} run {index}/{count}: "
                f"{record['automated_status']} "
                f"({record['actual_status']}, tools={record['tool_calls']}, "
                f"steps={record['steps_used']})"
            )

    payload = _sampling_payload(selected, runs, metadata, complete=True)
    _write_json(output_path, payload)
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _attach_only(output_path: Path, reviews_path: Path) -> dict[str, Any]:
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    reviews_by_run, reviews_by_case = _load_reviews(reviews_path)
    records = payload.get("cases", [])
    for record in records:
        if record.get("status") in {"blocked", "not_run"}:
            continue
        _attach_review(record, reviews_by_run, reviews_by_case)
    payload["summary"] = _summary(records)
    payload.setdefault("metadata", {})[
        "reviews_attached_at"
    ] = datetime.now(timezone.utc).isoformat()
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Enterprise Knowledge QA Agent evaluation."
    )
    parser.add_argument(
        "--profile",
        choices=sorted(BUDGET_PROFILES),
        default="baseline",
        help="Budget profile (baseline=6/5, candidate=8/7).",
    )
    parser.add_argument(
        "--cases",
        help="Optional comma-separated case IDs.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help=(
            "Result JSON path. Defaults to examples/evaluation_results.json "
            "or examples/repeated_sampling.json for sampling."
        ),
    )
    parser.add_argument(
        "--reviews",
        default=str(DEFAULT_REVIEWS),
        help="Manual review JSON path.",
    )
    parser.add_argument(
        "--fake-only",
        action="store_true",
        help="Run only FakeLLM cases; real cases are marked not_run.",
    )
    parser.add_argument(
        "--real-only",
        action="store_true",
        help="For sampling, run only real-model cases.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat each selected case N times (sampling mode).",
    )
    parser.add_argument(
        "--repeat-plan",
        help=(
            "Per-case repeat counts, e.g. E01=5,E02=3. "
            "Implies sampling mode."
        ),
    )
    parser.add_argument(
        "--batch-label",
        default="phase6",
        help="Label for the sampling experiment batch.",
    )
    parser.add_argument(
        "--attach-reviews-only",
        action="store_true",
        help="Reattach reviews to an existing result file without executing.",
    )
    args = parser.parse_args(argv)

    if args.repeats < 1:
        print("--repeats must be >= 1", file=sys.stderr)
        return 2

    try:
        repeat_plan = _parse_repeat_plan(args.repeat_plan)
    except ValueError as exc:
        print(f"[INPUT ERROR] {exc}", file=sys.stderr)
        return 2

    sampling_mode = args.repeats > 1 or bool(repeat_plan)
    output_path = Path(args.output) if args.output else (
        REPEATED_OUTPUT if sampling_mode else DEFAULT_OUTPUT
    )
    reviews_path = Path(args.reviews)

    if args.attach_reviews_only:
        payload = _attach_only(output_path, reviews_path)
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        return 0

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.cases:
        selected = _statuses(args.cases)
        cases = [case for case in cases if case.get("case_id") in selected]
        missing = selected - {case.get("case_id") for case in cases}
        if missing:
            print(f"Unknown case IDs: {sorted(missing)}", file=sys.stderr)
            return 2

    known_ids = {case.get("case_id") for case in cases}
    unknown_plan_ids = set(repeat_plan) - known_ids
    if unknown_plan_ids:
        print(
            f"Unknown repeat-plan case IDs: {sorted(unknown_plan_ids)}",
            file=sys.stderr,
        )
        return 2

    if sampling_mode:
        try:
            payload = _run_sampling(
                cases,
                args.profile,
                args.repeats,
                repeat_plan,
                args.real_only,
                args.batch_label,
                output_path,
            )
        except ValueError as exc:
            print(f"[INPUT ERROR] {exc}", file=sys.stderr)
            return 2
        print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
        print(f"sampling results: {_relative(output_path)}")
        return 0

    reviews_by_run, reviews_by_case = _load_reviews(reviews_path)
    payload = _run_all(
        cases,
        args.profile,
        reviews_by_run,
        reviews_by_case,
        args.fake_only,
    )
    _write_json(output_path, payload)
    print(json.dumps(payload["summary"], ensure_ascii=False, indent=2))
    print(f"results: {_relative(output_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
