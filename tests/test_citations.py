"""Citation authenticity and final-result evidence checks."""

from __future__ import annotations

from knowledge_agent.schemas import Citation, FinalResult, TaskStatus
from knowledge_agent.tools import read_document
from knowledge_agent.validator import ValidationContext, validate_final_result


def _docs(*doc_ids: str) -> dict[str, dict[str, str]]:
    return {doc_id: read_document(doc_id) for doc_id in doc_ids}


def _citation(doc_id: str) -> Citation:
    return Citation(
        doc_id=doc_id, quote=read_document(doc_id)["content"]
    )


def _context(
    docs: dict[str, dict[str, str]],
    question: str,
    *,
    tool_errors: tuple[dict[str, object], ...] = (),
    tool_calls_made: int = 1,
) -> ValidationContext:
    return ValidationContext(
        question=question,
        read_documents=docs,
        known_documents=docs,
        tool_errors=tool_errors,
        tool_calls_made=tool_calls_made,
    )


def test_valid_citation_passes() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供有效发票及订单或入住凭证。",
        citations=[_citation("D03")],
        gaps=[],
    )

    assert (
        validate_final_result(
            result, _context(docs, "差旅住宿费报销需要哪些材料？")
        )
        == []
    )


def test_citation_to_unread_document_fails() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="根据报销制度，30日内提交。",
        citations=[_citation("D01")],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("未在当前运行中读取" in error for error in errors)


def test_tampered_quote_fails() -> None:
    docs = _docs("D03")
    quote = docs["D03"]["content"] + "（被篡改）"
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供发票。",
        citations=[Citation(doc_id="D03", quote=quote)],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("真实子串" in error for error in errors)


def test_quote_copied_from_another_document_fails() -> None:
    docs = _docs("D01", "D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供发票。",
        citations=[
            Citation(doc_id="D01", quote=docs["D03"]["content"])
        ],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("实际来自其他已读文档" in error for error in errors)


def test_answered_without_citations_fails() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="需要提供发票。",
        citations=[],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "差旅住宿费报销需要哪些材料？")
    )

    assert any("至少一条有效引用" in error for error in errors)


def test_insufficient_evidence_allows_empty_citations() -> None:
    docs = _docs("D03")
    result = FinalResult(
        status=TaskStatus.INSUFFICIENT_EVIDENCE,
        answer="现有资料不足以回答该问题。",
        citations=[],
        gaps=["知识库中没有相关规定"],
    )

    assert (
        validate_final_result(
            result, _context(docs, "公司年会在哪里举办？")
        )
        == []
    )


def test_insufficient_evidence_requires_gaps() -> None:
    result = FinalResult(
        status=TaskStatus.INSUFFICIENT_EVIDENCE,
        answer="现有资料不足以回答。",
        citations=[],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(_docs("D03"), "公司年会在哪里举办？")
    )

    assert any("必须说明资料缺口" in error for error in errors)


def test_tool_error_without_actual_error_fails() -> None:
    result = FinalResult(
        status=TaskStatus.TOOL_ERROR,
        answer="工具执行失败。",
        citations=[],
        gaps=["工具错误"],
    )

    errors = validate_final_result(
        result, _context(_docs("D03"), "差旅住宿费报销需要哪些材料？")
    )

    assert any("空检索结果不构成错误" in error for error in errors)


def test_tool_error_with_actual_error_passes() -> None:
    result = FinalResult(
        status=TaskStatus.TOOL_ERROR,
        answer="工具执行失败。",
        citations=[],
        gaps=["工具错误"],
    )
    context = _context(
        _docs("D03"),
        "差旅住宿费报销需要哪些材料？",
        tool_errors=({"error_type": "DocumentDataError"},),
    )

    assert validate_final_result(result, context) == []


def test_needs_clarification_requires_gaps() -> None:
    result = FinalResult(
        status=TaskStatus.NEEDS_CLARIFICATION,
        answer="请补充问题范围。",
        citations=[],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(_docs("D03"), "差旅住宿费报销需要哪些材料？")
    )

    assert any("必须给出具体" in error for error in errors)


def test_incomplete_without_real_reason_fails() -> None:
    result = FinalResult(
        status=TaskStatus.INCOMPLETE,
        answer="任务未完成。",
        citations=[],
        gaps=["需要进一步核实"],
    )

    errors = validate_final_result(
        result,
        _context(
            _docs("D03"),
            "差旅住宿费报销需要哪些材料？",
            tool_calls_made=0,
        ),
    )

    assert any("缺少实际未完成原因" in error for error in errors)

    context_with_tool = _context(
        _docs("D03"),
        "差旅住宿费报销需要哪些材料？",
        tool_calls_made=1,
    )
    assert validate_final_result(result, context_with_tool) == []


def test_answered_deadline_must_exist_in_documents() -> None:
    docs = _docs("D01")
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="应45日内提交。",
        citations=[_citation("D01")],
        gaps=[],
    )

    errors = validate_final_result(
        result, _context(docs, "报销期限是多久？")
    )

    assert any("未在已读/已知文档中出现" in error for error in errors)
