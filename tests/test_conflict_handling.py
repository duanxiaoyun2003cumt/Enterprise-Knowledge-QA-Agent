"""Deadline conflict safety invariants and citation requirements."""

from __future__ import annotations

import json

from knowledge_agent.schemas import Citation, FinalResult, TaskStatus
from knowledge_agent.tools import read_document
from knowledge_agent.validator import (
    ValidationContext,
    find_deadline_conflict,
    validate_final_result,
)
from tests.agent_helpers import (
    cite,
    finish_action,
    make_agent,
    read_action,
    search_action,
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


def test_single_deadline_answer_is_rejected_then_clarification_passes() -> None:
    agent, fake = make_agent(
        [
            search_action("报销期限"),
            read_action("D01"),
            read_action("D02"),
            finish_action(
                "answered",
                "应适用30日内提交。",
                citations=[cite("D01")],
            ),
            finish_action(
                "needs_clarification",
                "现有资料存在冲突：D01规定30日，D02规定15日，"
                "且没有说明适用范围或优先级。请确认应适用哪份制度。",
                citations=[cite("D01"), cite("D02")],
                gaps=["缺少适用范围或制度优先级"],
            ),
        ],
        max_steps=6,
        max_tool_calls=5,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.NEEDS_CLARIFICATION
    assert len(fake.calls) == 5
    assert agent.last_state.validation_failures == 1
    assert agent.last_state.tool_calls == 3
    assert {citation.doc_id for citation in result.citations} == {
        "D01",
        "D02",
    }

    trace = agent.trace.to_dict()
    assert trace["final_result"]["status"] == "needs_clarification"
    assert trace["stop_reason"] == "finish"
    invalid_step = trace["steps"][3]
    assert invalid_step["action_validation"] == "invalid_final_result"
    assert "冲突" in json.dumps(
        invalid_step["validation_errors"], ensure_ascii=False
    )
    assert invalid_step["next_action"] == "finish"


def test_newer_version_is_not_treated_as_priority() -> None:
    docs = _docs("D01", "D02")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="D02的版本日期更新，应适用15日内提交。",
        citations=[
            Citation(doc_id="D02", quote=docs["D02"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("冲突" in error for error in errors)


def test_conflict_detection_is_content_based_not_doc_id_based() -> None:
    docs = {
        "X1": {
            "doc_id": "X1",
            "title": "制度一",
            "version_date": "2025-01-01",
            "content": "费用发生后40日内提交申请。",
        },
        "X2": {
            "doc_id": "X2",
            "title": "制度二",
            "version_date": "2025-02-01",
            "content": "费用发生后20日内提交申请。",
        },
    }
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="应适用40日内提交。",
        citations=[Citation(doc_id="X1", quote=docs["X1"]["content"])],
        gaps=[],
    )

    conflict = find_deadline_conflict(_context(docs, "报销期限是多久？"))
    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert conflict.exists is True
    assert conflict.doc_values["X1"] == frozenset({(40, "日")})
    assert any("冲突" in error for error in errors)


def test_clarification_must_cite_both_conflict_documents() -> None:
    docs = _docs("D01", "D02")
    result = FinalResult(
        status=TaskStatus.NEEDS_CLARIFICATION,
        answer="存在冲突，请确认适用哪份制度。",
        citations=[
            Citation(doc_id="D01", quote=docs["D01"]["content"])
        ],
        gaps=["缺少适用范围或制度优先级"],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("D02" in error for error in errors)


def test_conflict_quotes_must_include_the_deadline_text() -> None:
    docs = _docs("D01", "D02")
    result = FinalResult(
        status=TaskStatus.NEEDS_CLARIFICATION,
        answer="存在冲突，请确认适用哪份制度。",
        citations=[
            Citation(doc_id="D01", quote="提交报销申请。"),
            Citation(doc_id="D02", quote="提交报销申请。"),
        ],
        gaps=["缺少适用范围或制度优先级"],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("未包含该文档的期限原文" in error for error in errors)


def test_product_answer_is_not_blocked_by_unrelated_deadline_docs() -> None:
    docs = _docs("D01", "D02", "D04")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="产品X企业版支持SAML 2.0单点登录。",
        citations=[
            Citation(doc_id="D04", quote=docs["D04"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "产品X支持什么单点登录协议？")
    )

    assert errors == []


def test_single_deadline_document_allows_answered() -> None:
    docs = _docs("D01")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="应30日内提交。",
        citations=[
            Citation(doc_id="D01", quote=docs["D01"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert errors == []
