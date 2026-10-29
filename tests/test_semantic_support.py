"""Semantic-support separation from citation authenticity."""

from __future__ import annotations

import pytest

from knowledge_agent.schemas import Citation, FinalResult, TaskStatus
from knowledge_agent.tools import read_document
from knowledge_agent.validator import ValidationContext, validate_final_result
from tests.agent_helpers import (
    cite,
    finish_action,
    make_agent,
    read_action,
)


def _docs(*doc_ids: str) -> dict[str, dict[str, str]]:
    return {doc_id: read_document(doc_id) for doc_id in doc_ids}


def _context(
    docs: dict[str, dict[str, str]], question: str
) -> ValidationContext:
    return ValidationContext(
        question=question,
        read_documents=docs,
        known_documents=docs,
        tool_calls_made=1,
    )


def test_material_answer_with_deadline_quote_fails_semantic_support() -> None:
    docs = _docs("D01")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="差旅住宿报销需要提供有效发票。",
        citations=[
            Citation(doc_id="D01", quote=docs["D01"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("语义支持失败" in error for error in errors)
    assert not any("真实子串" in error for error in errors)
    assert not any("未在当前运行中读取" in error for error in errors)


def test_supported_material_citation_passes() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供有效发票及订单或入住凭证。",
        citations=[
            Citation(doc_id="D03", quote=docs["D03"]["content"])
        ],
        gaps=[],
    )

    assert (
        validate_final_result(
            result, _context(docs, "差旅住宿费报销需要哪些材料？")
        )
        == []
    )


def test_generic_uncertain_citation_is_rejected() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="请按公司制度办理。",
        citations=[
            Citation(doc_id="D03", quote=docs["D03"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("语义支持不确定" in error for error in errors)


def test_conflict_clarification_remains_semantically_supported() -> None:
    docs = _docs("D01", "D02")
    result = FinalResult(
        status=TaskStatus.NEEDS_CLARIFICATION,
        answer=(
            "D01规定30日内提交，D02规定15日内提交；"
            "原文未说明适用范围或优先级，请确认。"
        ),
        citations=[
            Citation(doc_id="D01", quote=docs["D01"]["content"]),
            Citation(doc_id="D02", quote=docs["D02"]["content"]),
        ],
        gaps=["缺少适用范围或优先级"],
    )

    assert (
        validate_final_result(result, _context(docs, "报销期限是多久？"))
        == []
    )


def test_e01_style_material_answer_with_d03_passes() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="差旅住宿费报销需要提供有效发票，以及订单或入住凭证。",
        citations=[Citation(doc_id="D03", quote=docs["D03"]["content"])],
        gaps=[],
    )

    assert (
        validate_final_result(
            result, _context(docs, "差旅住宿费报销需要哪些材料？")
        )
        == []
    )


def test_deadline_value_that_quote_does_not_contain_fails() -> None:
    docs = _docs("D01")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="应在45日内提交报销申请。",
        citations=[Citation(doc_id="D01", quote=docs["D01"]["content"])],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("语义支持失败" in error for error in errors)


def test_answered_claim_without_citation_coverage_fails() -> None:
    docs = _docs("D01", "D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="差旅住宿报销需要提供有效发票；应在30日内提交。",
        citations=[
            Citation(doc_id="D03", quote=docs["D03"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    claim_errors = [error for error in errors if "答案关键事实" in error]
    assert claim_errors
    assert any("语义支持失败" in error for error in claim_errors)


def test_insufficient_evidence_with_partial_citation_is_not_over_rejected() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.INSUFFICIENT_EVIDENCE,
        answer="没有找到报销期限的规定；D03 只说明住宿材料。",
        citations=[
            Citation(doc_id="D03", quote=docs["D03"]["content"])
        ],
        gaps=["知识库缺少报销期限制度"],
    )

    assert (
        validate_final_result(
            result, _context(docs, "报销期限是多久？")
        )
        == []
    )


def test_semantic_assessment_exception_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import knowledge_agent.validator as validator_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("semantic verifier unavailable")

    monkeypatch.setattr(
        validator_module, "assess_citation_support", _boom
    )
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供有效发票及订单或入住凭证。",
        citations=[Citation(doc_id="D03", quote=docs["D03"]["content"])],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("语义支持校验不可用" in error for error in errors)


def test_agent_fails_closed_when_semantic_check_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import knowledge_agent.validator as validator_module

    def _boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("semantic verifier unavailable")

    monkeypatch.setattr(
        validator_module, "assess_citation_support", _boom
    )
    agent, fake = make_agent(
        [
            read_action("D03"),
            finish_action(
                "answered",
                "需要提供有效发票及订单或入住凭证。",
                citations=[cite("D03")],
            ),
            finish_action(
                "insufficient_evidence",
                "语义校验不可用，无法确认引用是否支持答案。",
                gaps=["引用语义校验不可用"],
            ),
        ]
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert len(fake.calls) == 3
    trace = agent.trace.to_dict()
    invalid_step = trace["steps"][1]
    assert invalid_step["action_validation"] == "invalid_final_result"
    assert any(
        "语义支持校验不可用" in error
        for error in invalid_step["validation_errors"]
    )
    assert trace["final_result"]["status"] == "insufficient_evidence"


def test_agent_rejects_unsupported_citation_and_recovers_with_budget() -> None:
    agent, fake = make_agent(
        [
            read_action("D01"),
            finish_action(
                "answered",
                "差旅住宿报销需要提供有效发票。",
                citations=[cite("D01")],
            ),
            finish_action(
                "insufficient_evidence",
                "该引用只能支持报销期限，不能支持住宿材料结论。",
                gaps=["引用语义不支持"],
            ),
        ]
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert len(fake.calls) == 3
    assert agent.last_state.final_validation_retries == 1
    assert agent.last_state.format_repairs == 0
    assert agent.last_state.tool_calls == 1
    trace = agent.trace.to_dict()
    invalid_step = trace["steps"][1]
    assert invalid_step["action_validation"] == "invalid_final_result"
    assert invalid_step["repair_kind"] == "final_validation"
    assert invalid_step["tool_error_type"] == "FinalResultValidationError"
    assert trace["final_result"]["status"] == "insufficient_evidence"


def test_semantic_validation_adds_no_model_calls_beyond_max_steps() -> None:
    agent, fake = make_agent(
        [
            read_action("D01"),
            *[
                finish_action(
                    "answered",
                    "差旅住宿报销需要提供有效发票。",
                    citations=[cite("D01")],
                )
                for _ in range(5)
            ],
        ],
        max_final_validation_retries=10,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    trace = agent.trace.to_dict()
    assert result.status is TaskStatus.INCOMPLETE
    assert trace["stop_reason"] == "max_steps"
    assert len(fake.calls) == 6
    assert trace["steps_used"] == 6
    assert agent.max_steps == 6
