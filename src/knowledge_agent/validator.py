"""Deterministic safety checks for the agent's final result.

The model decides what to do; this module only enforces safety invariants such
as real citations, actual tool-error evidence, and unresolved deadline
conflicts. String checks cannot prove semantic support for an answer.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .retrieval import normalize_text
from .schemas import Citation, FinalResult, TaskStatus
from .semantic import assess_citation_support, find_unsupported_claims

_DURATION_RE = re.compile(r"(\d+)\s*(个工作日|工作日|天|日)")
_DEADLINE_CONTEXT_TERMS = (
    "期限",
    "时限",
    "最晚",
    "截止",
    "提交",
    "几天",
    "多少天",
    "多久",
    "日内",
    "什么时候",
    "何时",
    "到期",
)
_PRIORITY_TERMS = (
    "优先",
    "为准",
    "废止",
    "替代",
    "适用范围",
    "适用对象",
    "效力",
    "特别规定",
)


@dataclass(frozen=True)
class ValidationContext:
    """All runtime evidence needed to validate a proposed final result."""

    question: str
    read_documents: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    known_documents: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    tool_errors: Sequence[Mapping[str, Any]] = ()
    system_errors: Sequence[str] = ()
    tool_calls_made: int = 0
    step_limit_reached: bool = False
    tool_limit_reached: bool = False


@dataclass(frozen=True)
class DeadlineConflict:
    """Actual deadline values observed in known documents."""

    doc_values: Mapping[str, frozenset[tuple[int, str]]]
    has_priority_evidence: bool

    @property
    def distinct_values(self) -> frozenset[tuple[int, str]]:
        if not self.doc_values:
            return frozenset()
        result: set[tuple[int, str]] = set()
        for values in self.doc_values.values():
            result.update(values)
        return frozenset(result)

    @property
    def exists(self) -> bool:
        return len(self.distinct_values) >= 2


def _duration_entries(text: str) -> set[tuple[int, str]]:
    entries: set[tuple[int, str]] = set()
    for number, unit in _DURATION_RE.findall(text):
        normalized_unit = "工作日" if "工作日" in unit else "日"
        entries.add((int(number), normalized_unit))
    return entries


def _format_duration(value: tuple[int, str]) -> str:
    return f"{value[0]}{value[1]}"


def find_deadline_conflict(context: ValidationContext) -> DeadlineConflict:
    """Find conflicting deadline values using actual known document text.

    Detection is content-based, not hardcoded to document IDs or topics.
    """
    documents = context.known_documents or context.read_documents
    doc_values: dict[str, frozenset[tuple[int, str]]] = {}
    contents: list[str] = []

    for doc_id, document in documents.items():
        content = str(document.get("content", ""))
        contents.append(content)
        values = _duration_entries(content)
        if values:
            doc_values[doc_id] = frozenset(values)

    combined = normalize_text(" ".join(contents))
    has_priority = any(term in combined for term in _PRIORITY_TERMS)
    return DeadlineConflict(
        doc_values=doc_values,
        has_priority_evidence=has_priority,
    )


def _is_deadline_context(
    context: ValidationContext, result: FinalResult
) -> bool:
    text = normalize_text(context.question) + normalize_text(result.answer)
    if any(term in text for term in _DEADLINE_CONTEXT_TERMS):
        return True
    return bool(_duration_entries(result.answer))


def validate_citations(
    citations: Sequence[Citation],
    read_documents: Mapping[str, Mapping[str, str]],
) -> list[str]:
    """Check that every citation comes from a document read in this run."""
    errors: list[str] = []

    for citation in citations:
        document = read_documents.get(citation.doc_id)
        if document is None:
            errors.append(
                f"引用文档 {citation.doc_id} 未在当前运行中读取（或不存在）。"
            )
            continue

        content = str(document.get("content", ""))
        if citation.quote in content:
            continue

        other_sources = [
            doc_id
            for doc_id, other in read_documents.items()
            if doc_id != citation.doc_id
            and citation.quote in str(other.get("content", ""))
        ]
        if other_sources:
            errors.append(
                f"引用 {citation.doc_id} 的 quote 实际来自其他已读文档 "
                f"{', '.join(sorted(other_sources))}，不能冒充来源。"
            )
        else:
            errors.append(
                f"引用 {citation.doc_id} 的 quote 不是该文档已读原文的真实子串。"
            )

    return errors


def _conflict_citation_errors(
    result: FinalResult, conflict: DeadlineConflict
) -> list[str]:
    errors: list[str] = []
    quotes_by_doc: dict[str, list[str]] = {}
    for citation in result.citations:
        quotes_by_doc.setdefault(citation.doc_id, []).append(citation.quote)

    for doc_id, values in conflict.doc_values.items():
        quotes = quotes_by_doc.get(doc_id, [])
        if not quotes:
            errors.append(
                f"存在未解决期限冲突，必须引用冲突文档 {doc_id} 的原文。"
            )
            continue

        quoted_values: set[tuple[int, str]] = set()
        for quote in quotes:
            quoted_values.update(_duration_entries(quote))
        if not (quoted_values & set(values)):
            errors.append(
                f"引用 {doc_id} 的 quote 未包含该文档的期限原文。"
            )

    cited_conflict_docs = set(quotes_by_doc) & set(conflict.doc_values)
    if len(cited_conflict_docs) < 2:
        errors.append(
            "存在期限冲突，needs_clarification 至少需要引用两份冲突文档。"
        )
    return errors


def validate_final_result(
    result: FinalResult, context: ValidationContext
) -> list[str]:
    """Return validation errors; an empty list means the result is acceptable."""
    errors = validate_citations(result.citations, context.read_documents)

    # Authenticity and semantic support are separate checks. Only authentic
    # citations are semantically assessed; unread/tampered quotes are already
    # rejected by validate_citations.
    authentic_citations = [
        citation
        for citation in result.citations
        if citation.doc_id in context.read_documents
        and citation.quote
        in str(context.read_documents[citation.doc_id].get("content", ""))
    ]
    try:
        supports = assess_citation_support(
            result.answer,
            authentic_citations,
            context.read_documents,
        )
        # An answered result should have every specific claim covered by an
        # authentic citation. Other statuses may cite partial evidence while
        # explaining a gap, so claim coverage is only enforced for answered.
        unsupported_claims = (
            find_unsupported_claims(
                result.answer,
                authentic_citations,
                context.read_documents,
            )
            if result.status is TaskStatus.ANSWERED
            else []
        )
    except Exception:
        # A semantic-check failure (including an unavailable verifier) must
        # never be treated as support. Fail closed when a citation exists.
        supports = []
        unsupported_claims = []
        if authentic_citations:
            errors.append(
                "引用语义支持校验不可用，按保守策略拒绝该结果。"
            )

    for support in supports:
        if support.status == "unsupported":
            errors.append(
                f"引用 {support.doc_id} 的原文与答案关键事实不相关"
                "（语义支持失败）。"
            )
        elif support.status == "uncertain":
            errors.append(
                f"无法确定引用 {support.doc_id} 是否支持答案"
                "（语义支持不确定），按保守策略拒绝。"
            )

    for claim in unsupported_claims:
        preview = claim.claim[:60]
        if claim.status == "unsupported":
            errors.append(
                f"答案关键事实“{preview}”没有获得任何已读引用的"
                "语义支持（语义支持失败）。"
            )
        else:
            errors.append(
                f"无法确定答案关键事实“{preview}”是否被引用支持"
                "（语义支持不确定），按保守策略拒绝。"
            )

    conflict = find_deadline_conflict(context)
    deadline_context = _is_deadline_context(context, result)
    unresolved_conflict = (
        conflict.exists
        and not conflict.has_priority_evidence
        and deadline_context
    )

    if result.status is TaskStatus.ANSWERED:
        if not result.citations:
            errors.append("answered 必须包含至少一条有效引用。")
        if unresolved_conflict:
            errors.append(
                "存在未解决的期限冲突，不能以 answered 结束；"
                "请引用冲突双方并请求用户澄清。"
            )
        if deadline_context:
            known_values: set[tuple[int, str]] = set()
            for values in conflict.doc_values.values():
                known_values.update(values)
            for value in sorted(_duration_entries(result.answer)):
                if value not in known_values:
                    errors.append(
                        f"回答中的期限 {_format_duration(value)} "
                        "未在已读/已知文档中出现，不能编造。"
                    )

    elif result.status is TaskStatus.NEEDS_CLARIFICATION:
        if not result.gaps:
            errors.append(
                "needs_clarification 必须给出具体的资料缺口或澄清原因。"
            )
        if unresolved_conflict:
            errors.extend(_conflict_citation_errors(result, conflict))

    elif result.status is TaskStatus.INSUFFICIENT_EVIDENCE:
        if not result.gaps:
            errors.append("insufficient_evidence 必须说明资料缺口。")
        if unresolved_conflict:
            errors.append(
                "已发现期限冲突，应请求用户澄清，而不是声明资料不足。"
            )

    elif result.status is TaskStatus.TOOL_ERROR:
        if not context.tool_errors and not context.system_errors:
            errors.append(
                "tool_error 必须基于真实的工具或系统故障；"
                "空检索结果不构成错误。"
            )

    elif result.status is TaskStatus.INCOMPLETE:
        if not result.gaps:
            errors.append("incomplete 必须说明未完成原因。")
        if not (
            context.step_limit_reached
            or context.tool_limit_reached
            or context.tool_errors
            or context.system_errors
            or context.tool_calls_made > 0
        ):
            errors.append("incomplete 缺少实际未完成原因。")

    return errors


__all__ = [
    "DeadlineConflict",
    "ValidationContext",
    "find_deadline_conflict",
    "validate_citations",
    "validate_final_result",
]
