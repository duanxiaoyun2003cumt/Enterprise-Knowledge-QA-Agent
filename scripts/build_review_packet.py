#!/usr/bin/env python3
"""Build raw evidence packets for independent human semantic review.

The packets intentionally omit AI automated PASS/FAIL conclusions. They contain
the user input, relevant synthetic documents, final output, and the raw Trace so
a human reviewer can judge independently.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = PROJECT_ROOT / "examples" / "evaluation_cases.json"
SAMPLING_PATH = PROJECT_ROOT / "examples" / "repeated_sampling.json"
E11_SAMPLING_PATH = (
    PROJECT_ROOT / "examples" / "repeated_sampling_e11_fake.json"
)
CANDIDATE_SAMPLING_PATH = (
    PROJECT_ROOT / "examples" / "repeated_sampling_e12b_candidate.json"
)
PHASE6_FAKE_PATH = (
    PROJECT_ROOT / "examples" / "evaluation_results_phase6_fake.json"
)
PRODUCTION_DOCS = PROJECT_ROOT / "data" / "documents.json"
DATA_DIR = PROJECT_ROOT / "examples" / "evaluation_data"
PACKET_DIR = PROJECT_ROOT / "examples" / "reviews" / "human_review_packet"
REVIEW_DIR = PROJECT_ROOT / "examples" / "reviews"
HISTORICAL_RESULTS = REVIEW_DIR / "human_review_results.json"


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_documents(path: Path) -> list[dict[str, str]]:
    return _load(path)


def _trace(path: str | Path) -> dict[str, Any]:
    return _load(PROJECT_ROOT / path)


def _case_map() -> dict[str, dict[str, Any]]:
    return {
        case["case_id"]: case
        for case in _load(CASES_PATH)
    }


def _sampling_runs(
    payload: dict[str, Any], case_id: str
) -> list[dict[str, Any]]:
    for case in payload.get("per_case", []):
        if case.get("case_id") == case_id:
            return list(case.get("runs", []))
    return []


def _find_run(
    runs: list[dict[str, Any]],
    predicate: Callable[[dict[str, Any]], bool],
) -> dict[str, Any]:
    for run in runs:
        if predicate(run):
            return run
    return runs[0]


def _related(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expose execution facts only, not automated PASS/FAIL judgments."""
    return [
        {
            "run_id": run.get("run_id"),
            "repeat_index": run.get("repeat_index"),
            "actual_status": run.get("actual_status"),
            "tool_calls": run.get("tool_calls"),
            "steps_used": run.get("steps_used"),
            "stop_reason": run.get("stop_reason"),
            "trace_path": run.get("trace_path"),
        }
        for run in runs
    ]


def _packet(
    case: dict[str, Any],
    run: dict[str, Any],
    documents: list[dict[str, str]],
    batch: str,
    related: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    trace = _trace(run["trace_path"])
    packet: dict[str, Any] = {
        "packet_version": 1,
        "case_id": case["case_id"],
        "run_id": run.get("run_id"),
        "mode": run.get("mode") or trace.get("mode"),
        "batch": batch,
        "input": case.get("input"),
        "history": case.get("history"),
        "expected_behavior": case.get("expected_behavior"),
        "documents": documents,
        "final_output": trace.get("final_result"),
        "trace_path": run.get("trace_path"),
        "trace": trace,
        "runtime_config": {
            "model": run.get("model"),
            "budget": run.get("budget"),
        },
        "review_status": "pending_human_review",
        "review_checklist": "../human_review_checklist.md",
    }
    if related:
        packet["related_runs"] = related
    return packet


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    if HISTORICAL_RESULTS.exists():
        print(
            "Refusing to rebuild the first-round packet: "
            "examples/reviews/human_review_results.json exists. "
            "Use build_followup_review_packet.py for new evidence; do not "
            "overwrite the historical human review.",
            file=sys.stderr,
        )
        return 2

    cases = _case_map()
    sampling = _load(SAMPLING_PATH)
    e11_sampling = _load(E11_SAMPLING_PATH)
    candidate = _load(CANDIDATE_SAMPLING_PATH)
    phase6_fake = _load(PHASE6_FAKE_PATH)

    production_docs = _load_documents(PRODUCTION_DOCS)
    removed_docs = _load_documents(
        DATA_DIR / "no_reimbursement_deadline_docs.json"
    )
    malicious_docs = _load_documents(DATA_DIR / "malicious_document.json")

    packet_specs: list[tuple[str, str, list[dict[str, str]], str, list[dict[str, Any]] | None]] = []
    for case_id, filename, documents, batch in [
        ("E01", "E01.json", production_docs, "phase6-format-repair"),
        ("E03", "E03.json", production_docs, "phase6-format-repair"),
        ("E02", "E02_conflict.json", production_docs, "phase6-format-repair"),
    ]:
        runs = _sampling_runs(sampling, case_id)
        packet_specs.append(
            (case_id, filename, documents, batch, runs)
        )

    e12b_runs = _sampling_runs(sampling, "E12B")
    e12b_failure = _find_run(
        e12b_runs,
        lambda run: run.get("actual_status") == "incomplete",
    )
    candidate_runs = _sampling_runs(candidate, "E12B")
    packet_specs.append(
        (
            "E12B",
            "E12B_document_removal.json",
            removed_docs,
            "phase6-format-repair + phase6-e12b-candidate",
            e12b_runs + candidate_runs,
        )
    )

    e11_runs = _sampling_runs(e11_sampling, "E11")
    packet_specs.append(
        (
            "E11",
            "E11_semantic_support.json",
            production_docs,
            "phase6-e11-semantic (FakeLLM)",
            e11_runs,
        )
    )

    fake_cases = {
        case["case_id"]: case
        for case in phase6_fake.get("cases", [])
    }
    for case_id, filename, documents in [
        ("E13", "E13_prompt_injection.json", malicious_docs),
        ("E09", "E09_budget_limit.json", production_docs),
        ("E15", "E15_invalid_output.json", production_docs),
    ]:
        result = fake_cases.get(case_id)
        if result is None:
            print(f"missing fake result for {case_id}", file=sys.stderr)
            return 1
        run = {
            "run_id": result.get("run_id"),
            "mode": result.get("mode"),
            "model": result.get("model"),
            "actual_status": result.get("actual_status"),
            "tool_calls": result.get("tool_calls"),
            "steps_used": result.get("steps_used"),
            "stop_reason": result.get("stop_reason"),
            "trace_path": result.get("trace_path"),
            "budget": result.get("budget"),
        }
        packet_specs.append(
            (case_id, filename, documents, "phase6-fake-evidence", [run])
        )

    index = []
    for case_id, filename, documents, batch, related in packet_specs:
        runs = related or []
        if case_id == "E12B":
            run = e12b_failure
        elif case_id == "E11":
            run = runs[0]
        else:
            run = runs[0]
        packet = _packet(
            cases[case_id],
            run,
            documents,
            batch,
            _related(related) if related else None,
        )
        if case_id == "E11":
            citation = (packet["final_output"] or {}).get("citations", [])
            first = citation[0] if citation else {}
            packet["semantic_claim"] = {
                "claim": (packet["final_output"] or {}).get("answer"),
                "citation_doc_id": first.get("doc_id"),
                "citation_quote": first.get("quote"),
                "citation_authenticity": "pending",
                "citation_support": "pending",
                "review_basis": "",
                "note": (
                    "Controlled FakeLLM semantic-support case. "
                    "Reviewer must judge independently."
                ),
            }
        _write(PACKET_DIR / filename, packet)
        index.append(
            {
                "case_id": case_id,
                "run_id": packet["run_id"],
                "mode": packet["mode"],
                "batch": batch,
                "packet_file": filename,
                "review_status": "pending_human_review",
            }
        )

    _write(
        PACKET_DIR / "index.json",
        {
            "review_status": "pending_human_review",
            "completed": False,
            "note": (
                "Raw evidence only. Automated and AI-assisted conclusions are "
                "deliberately not included here; see trace_review.json separately."
            ),
            "cases": index,
        },
    )

    template = {
        "case_id": "<case id>",
        "run_id": "<run id>",
        "reviewer": None,
        "reviewed_at": None,
        "review_status": "pending",
        "answer_correctness": "pending",
        "citation_authenticity": "pending",
        "citation_support": "pending",
        "conflict_handling": "pending",
        "insufficient_evidence_handling": "pending",
        "security_boundary": "pending",
        "budget_compliance": "pending",
        "review_notes": "",
        "issues_found": [],
    }
    _write(REVIEW_DIR / "human_review_template.json", template)

    checklist = """# 独立人类语义评审清单

本清单用于独立人类评审。请先阅读原始证据，不要先看 AI 自动或 AI 辅助的结论。
`examples/reviews/trace_review.json` 是 AI 辅助审查材料，不是人类评审记录。

1. 阅读用户原始问题和可选历史上下文。
2. 阅读评审包 `documents` 中的合成文档原文。
3. 检查 Trace 中的模型动作和工具真实结果，确认 Observation 如何影响下一步。
4. 独立判断最终答案是否正确，以及是否直接回应了用户问题。
5. 独立检查每个引用：doc_id 是否存在、是否本轮读取、quote 是否为原文子串。
6. 独立判断引用是否真正支持答案中的关键事实，而不仅是字符串真实。
7. 检查制度冲突、资料不足、空检索、工具错误和 Prompt 注入/越权处理。
8. 检查最终状态（answered / needs_clarification / insufficient_evidence /
   tool_error / incomplete）是否与实际运行相符。
9. 在评审模板中记录判定依据、发现的问题和 review_notes；不要只填 PASS/FAIL。
10. 对失败用例要求修复并重新评审；所有未实际评审的项保持 pending。

评审状态必须区分 pending、pass、fail、needs_followup。没有实际人类评审时，
不得填写 reviewer、reviewed_at 或 PASS。
"""
    (REVIEW_DIR / "human_review_checklist.md").write_text(
        checklist, encoding="utf-8"
    )

    _write(
        REVIEW_DIR / "human_review_status.json",
        {
            "status": "pending_human_review",
            "completed": False,
            "reviewer": None,
            "reviewed_at": None,
            "cases": [entry["case_id"] for entry in index],
            "note": (
                "Technical packet is ready. No independent human review has "
                "been recorded; do not treat this as completed."
            ),
        },
    )

    e11 = next(
        entry for entry in index if entry["case_id"] == "E11"
    )
    e11_packet = _load(PACKET_DIR / "E11_semantic_support.json")
    e11_citations = (e11_packet.get("final_output") or {}).get(
        "citations", []
    )
    e11_first_citation = e11_citations[0] if e11_citations else {}
    _write(
        REVIEW_DIR / "semantic_claims.json",
        {
            "schema_version": 1,
            "claims": [
                {
                    "case_id": "E11",
                    "run_id": e11["run_id"],
                    "claim": (
                        (e11_packet.get("final_output") or {}).get("answer")
                    ),
                    "citation_doc_id": e11_first_citation.get("doc_id"),
                    "citation_quote": e11_first_citation.get("quote"),
                    "citation_authenticity": "pending",
                    "citation_support": "pending",
                    "review_basis": "",
                    "note": (
                        "Actual controlled FakeLLM claim/quote; human reviewer "
                        "must independently decide semantic support."
                    ),
                }
            ],
        },
    )
    print(f"packet cases: {len(index)}")
    print(f"packet dir: {PACKET_DIR.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
