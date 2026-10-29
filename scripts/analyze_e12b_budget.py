#!/usr/bin/env python3
"""Combine E12B budget batches without mixing their statistics.

The experiment compares the default 6/5 budget with the candidate 8/7 budget.
Each batch keeps its own source file; this script only aggregates the E12B runs
and computes descriptive statistics. It does not claim statistical significance
and does not change the production default.
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from knowledge_agent.agent import SYSTEM_PROMPT  # noqa: E402
from knowledge_agent.retrieval import normalize_text  # noqa: E402

SOURCES = (
    ("baseline", "examples/repeated_sampling.json"),
    ("baseline", "examples/repeated_sampling_e12b_baseline_followup.json"),
    ("candidate", "examples/repeated_sampling_e12b_candidate.json"),
    ("candidate", "examples/repeated_sampling_e12b_candidate_followup.json"),
)
DATA_PATH = PROJECT_ROOT / "examples" / "evaluation_data" / (
    "no_reimbursement_deadline_docs.json"
)
OUTPUT_PATH = PROJECT_ROOT / "examples" / "e12b_budget_comparison.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_runs() -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group, relative in SOURCES:
        path = PROJECT_ROOT / relative
        payload = json.loads(path.read_text(encoding="utf-8"))
        for case in payload.get("per_case", []):
            if case.get("case_id") != "E12B":
                continue
            for run in case.get("runs", []):
                run_id = str(run.get("run_id"))
                if run_id in seen:
                    continue
                seen.add(run_id)
                runs.append({"batch_group": group, "source": relative, **run})
    return runs


def _run_actions(run: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    trace_path = PROJECT_ROOT / str(run["trace_path"])
    trace = json.loads(trace_path.read_text(encoding="utf-8"))
    searches: list[dict[str, Any]] = []
    reads: list[str] = []
    for step in trace.get("steps", []):
        params = step.get("action_params") or {}
        if step.get("action_name") == "search_documents":
            searches.append(
                {
                    "query": normalize_text(str(params.get("query", ""))),
                    "top_k": params.get("top_k"),
                }
            )
        elif (
            step.get("action_name") == "read_document"
            and step.get("observation_ok") is True
        ):
            reads.append(str(params.get("doc_id", "")))
    return searches, reads


def _stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [
        run
        for run in runs
        if run.get("automated_status") in {"pass", "fail"}
    ]
    binary = [
        1 if run["automated_status"] == "pass" else 0 for run in evaluated
    ]
    passed = sum(binary)
    n = len(binary)
    pass_rate = passed / n if n else None
    variance = None
    if n >= 2:
        variance = sum((value - pass_rate) ** 2 for value in binary) / (n - 1)

    statuses: Counter[str] = Counter()
    stop_reasons: Counter[str] = Counter()
    failure_types: Counter[str] = Counter()
    tool_limit = 0
    incomplete = 0
    repeated_search_runs = 0
    repeated_search_actions = 0
    repeated_read_runs = 0
    traces: list[str] = []

    for run in runs:
        status = str(run.get("actual_status"))
        statuses[status] += 1
        stop_reason = str(run.get("stop_reason"))
        stop_reasons[stop_reason] += 1
        if stop_reason == "tool_limit":
            tool_limit += 1
        if status == "incomplete":
            incomplete += 1
        if run.get("automated_status") == "fail":
            for reason in run.get("failure_reasons") or ["unknown"]:
                failure_types[reason] += 1
        searches, reads = _run_actions(run)
        keys = [(item["query"], item["top_k"]) for item in searches]
        duplicates = len(keys) - len(set(keys))
        if duplicates:
            repeated_search_runs += 1
            repeated_search_actions += duplicates
        if len(reads) != len(set(reads)):
            repeated_read_runs += 1
        traces.append(str(run.get("trace_path")))

    steps = [int(run.get("steps_used", 0)) for run in runs]
    tools = [int(run.get("tool_calls", 0)) for run in runs]
    return {
        "runs": len(runs),
        "evaluated": n,
        "passed": passed,
        "failed": n - passed,
        "automated_pass_rate": pass_rate,
        "business_pass_rate": None,
        "manual_review_pending": len(runs),
        "sample_variance": variance,
        "status_counts": dict(statuses),
        "stop_reason_counts": dict(stop_reasons),
        "insufficient_evidence_count": statuses.get("insufficient_evidence", 0),
        "incomplete_count": incomplete,
        "tool_limit_count": tool_limit,
        "failure_types": dict(failure_types),
        "avg_steps": sum(steps) / len(steps) if steps else None,
        "avg_tool_calls": sum(tools) / len(tools) if tools else None,
        "total_steps": sum(steps),
        "total_tool_calls": sum(tools),
        "repeated_search_runs": repeated_search_runs,
        "repeated_search_run_ratio": (
            repeated_search_runs / len(runs) if runs else None
        ),
        "repeated_search_actions": repeated_search_actions,
        "repeated_read_runs": repeated_read_runs,
        "format_repairs_total": sum(
            int(run.get("format_repairs", 0)) for run in runs
        ),
        "final_validation_retries_total": sum(
            int(run.get("final_validation_retries", 0)) for run in runs
        ),
        "trace_paths": traces,
    }


def main() -> int:
    runs = _load_runs()
    groups = {
        group: [run for run in runs if run["batch_group"] == group]
        for group in ("baseline", "candidate")
    }
    budgets = {}
    for group, group_runs in groups.items():
        if group_runs:
            budgets[group] = group_runs[0].get("budget")

    payload = {
        "metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "experiment": "E12B budget comparison",
            "model": "deepseek-flash",
            "prompt_sha256": hashlib.sha256(
                SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
            "document_data": "examples/evaluation_data/"
            "no_reimbursement_deadline_docs.json",
            "document_data_sha256": _sha256(DATA_PATH),
            "sources": [relative for _, relative in SOURCES],
            "budgets": budgets,
            "pass_rate_basis": "automated_assertions",
            "caveat": (
                "Eight runs per group is a small descriptive sample. "
                "It is not evidence of statistical significance. The default "
                "budget stays 6/5 until larger evidence supports a change. "
                "Business pass rate remains undefined until human semantic "
                "review is completed."
            ),
        },
        "groups": {
            group: {
                "statistics": _stats(group_runs),
                "runs": group_runs,
            }
            for group, group_runs in groups.items()
        },
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(
        {
            group: payload["groups"][group]["statistics"]
            for group in payload["groups"]
        },
        ensure_ascii=False,
        indent=2,
    ))
    print(f"comparison: {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
