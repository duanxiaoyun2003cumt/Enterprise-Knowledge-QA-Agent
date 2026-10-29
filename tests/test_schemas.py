"""Schema-only tests. No API key, network, or provider access is required."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from knowledge_agent.schemas import (
    AskUserAction,
    Citation,
    FinalResult,
    FinishAction,
    ReadDocumentAction,
    SearchDocumentsAction,
    TaskStatus,
    parse_agent_action,
)


def _search_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "search_documents",
        "query": "年假申请流程",
        "top_k": 3,
        "decision_note": "需要检索制度文档",
    }
    payload.update(overrides)
    return payload


def _read_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "read_document",
        "doc_id": "hr-policy-001",
        "decision_note": "读取候选文档",
    }
    payload.update(overrides)
    return payload


def _ask_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "ask_user",
        "question": "你咨询的是哪种假期？",
        "reason": "问题存在歧义",
        "decision_note": "需要先澄清适用范围",
    }
    payload.update(overrides)
    return payload


def _citation_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "doc_id": "hr-policy-001",
        "quote": "试用期员工依法享有年休假。",
    }
    payload.update(overrides)
    return payload


def _finish_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": "finish",
        "status": "answered",
        "answer": "试用期员工可以依法休年假。",
        "citations": [_citation_payload()],
        "gaps": ["缺少司龄认定细则"],
        "decision_note": "已有制度原文支持结论",
    }
    payload.update(overrides)
    return payload


VALID_ACTIONS = [
    pytest.param(_search_payload(), SearchDocumentsAction, id="search_documents"),
    pytest.param(_read_payload(), ReadDocumentAction, id="read_document"),
    pytest.param(_ask_payload(), AskUserAction, id="ask_user"),
    pytest.param(_finish_payload(), FinishAction, id="finish"),
]


# --- Valid actions ---------------------------------------------------------


@pytest.mark.parametrize(("payload", "expected_type"), VALID_ACTIONS)
def test_valid_actions_parse(payload: dict[str, Any], expected_type: type) -> None:
    action = parse_agent_action(payload)

    assert isinstance(action, expected_type)
    assert action.action == payload["action"]
    assert action.decision_note


def test_all_task_statuses_accepted() -> None:
    for status in TaskStatus:
        action = parse_agent_action(_finish_payload(status=status.value))
        assert isinstance(action, FinishAction)
        assert action.status is status


# --- Invalid action names --------------------------------------------------


@pytest.mark.parametrize(
    "action_name",
    ["search", "read", "answer", "SEARCH_DOCUMENTS", "noop", "", None, 1],
)
def test_invalid_action_name_rejected(action_name: Any) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(action=action_name))


def test_missing_action_name_rejected() -> None:
    payload = _search_payload()
    payload.pop("action")

    with pytest.raises(ValidationError):
        parse_agent_action(payload)


# --- Missing required parameters ------------------------------------------


@pytest.mark.parametrize(
    ("payload", "field"),
    [
        pytest.param(_search_payload(), "query", id="search-query"),
        pytest.param(_search_payload(), "top_k", id="search-top_k"),
        pytest.param(_search_payload(), "decision_note", id="search-decision_note"),
        pytest.param(_read_payload(), "doc_id", id="read-doc_id"),
        pytest.param(_read_payload(), "decision_note", id="read-decision_note"),
        pytest.param(_ask_payload(), "question", id="ask-question"),
        pytest.param(_ask_payload(), "reason", id="ask-reason"),
        pytest.param(_ask_payload(), "decision_note", id="ask-decision_note"),
        pytest.param(_finish_payload(), "status", id="finish-status"),
        pytest.param(_finish_payload(), "answer", id="finish-answer"),
        pytest.param(_finish_payload(), "citations", id="finish-citations"),
        pytest.param(_finish_payload(), "gaps", id="finish-gaps"),
        pytest.param(_finish_payload(), "decision_note", id="finish-decision_note"),
    ],
)
def test_missing_required_parameter_rejected(
    payload: dict[str, Any], field: str
) -> None:
    payload = dict(payload)
    payload.pop(field)

    with pytest.raises(ValidationError):
        parse_agent_action(payload)


# --- Empty strings, wrong types, top_k range -------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\n\t"])
def test_empty_or_whitespace_query_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(query=value))


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_doc_id_question_reason_and_note_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_read_payload(doc_id=value))
    with pytest.raises(ValidationError):
        parse_agent_action(_ask_payload(question=value))
    with pytest.raises(ValidationError):
        parse_agent_action(_ask_payload(reason=value))
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(decision_note=value))


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_answer_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(answer=value))


@pytest.mark.parametrize("value", [0, 6, -1, 100])
def test_top_k_out_of_range_rejected(value: int) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(top_k=value))


@pytest.mark.parametrize("value", [1, 5])
def test_top_k_boundaries_accepted(value: int) -> None:
    action = parse_agent_action(_search_payload(top_k=value))

    assert isinstance(action, SearchDocumentsAction)
    assert action.top_k == value


@pytest.mark.parametrize("value", ["3", 2.5, True, None, [3]])
def test_top_k_wrong_type_rejected(value: Any) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(top_k=value))


@pytest.mark.parametrize(
    ("payload", "field", "bad_value"),
    [
        pytest.param(_search_payload(), "query", 123, id="query-int"),
        pytest.param(_search_payload(), "decision_note", 123, id="note-int"),
        pytest.param(_read_payload(), "doc_id", ["hr-policy-001"], id="doc_id-list"),
        pytest.param(_ask_payload(), "question", {"text": "?"}, id="question-dict"),
        pytest.param(_finish_payload(), "answer", 123, id="answer-int"),
    ],
)
def test_wrong_parameter_type_rejected(
    payload: dict[str, Any], field: str, bad_value: Any
) -> None:
    payload = dict(payload)
    payload[field] = bad_value

    with pytest.raises(ValidationError):
        parse_agent_action(payload)


def test_extra_action_field_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_search_payload(unexpected="value"))


# --- Invalid task status ---------------------------------------------------


@pytest.mark.parametrize("status", ["success", "unknown", "", None, 1])
def test_invalid_status_rejected(status: Any) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(status=status))


@pytest.mark.parametrize("status", ["success", "unknown", "", None])
def test_final_result_invalid_status_rejected(status: Any) -> None:
    with pytest.raises(ValidationError):
        FinalResult.model_validate(
            {
                "status": status,
                "answer": "回答",
                "citations": [],
                "gaps": [],
            }
        )


# --- Citations -------------------------------------------------------------


@pytest.mark.parametrize(
    "citation",
    [
        pytest.param({"doc_id": "hr-policy-001"}, id="missing-quote"),
        pytest.param({"quote": "原文"}, id="missing-doc_id"),
        pytest.param({"doc_id": "", "quote": "原文"}, id="empty-doc_id"),
        pytest.param({"doc_id": "   ", "quote": "原文"}, id="blank-doc_id"),
        pytest.param({"doc_id": "hr-policy-001", "quote": ""}, id="empty-quote"),
        pytest.param({"doc_id": "hr-policy-001", "quote": "   "}, id="blank-quote"),
        pytest.param(
            {"doc_id": "hr-policy-001", "quote": "原文", "source": "extra"},
            id="extra-field",
        ),
        pytest.param({"doc_id": 1, "quote": "原文"}, id="doc_id-int"),
        pytest.param({"doc_id": "hr-policy-001", "quote": ["原文"]}, id="quote-list"),
    ],
)
def test_invalid_citation_rejected(citation: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Citation.model_validate(citation)
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(citations=[citation]))


@pytest.mark.parametrize("citations", ["not-a-list", {"doc_id": "x"}, [42]])
def test_invalid_citations_collection_rejected(citations: Any) -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(citations=citations))


def test_citation_optional_metadata() -> None:
    citation = Citation(
        doc_id="hr-policy-001",
        quote="原文",
        version="v2",
        effective_date="2026-01-01",
    )

    assert citation.version == "v2"
    assert citation.effective_date == "2026-01-01"
    assert Citation(doc_id="hr-policy-001", quote="原文").version is None
    with pytest.raises(ValidationError):
        Citation(doc_id="hr-policy-001", quote="原文", version="")


def test_invalid_gaps_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(gaps=["有效缺口", ""]))
    with pytest.raises(ValidationError):
        parse_agent_action(_finish_payload(gaps="不是列表"))


# --- FinalResult -----------------------------------------------------------


def test_final_result_valid() -> None:
    result = FinalResult(
        status=TaskStatus.ANSWERED,
        answer="试用期员工可以依法休年假。",
        citations=[Citation(**_citation_payload())],
        gaps=[],
    )

    assert result.status is TaskStatus.ANSWERED
    assert len(result.citations) == 1


@pytest.mark.parametrize(
    "missing", ["status", "answer", "citations", "gaps"]
)
def test_final_result_missing_field_rejected(missing: str) -> None:
    payload: dict[str, Any] = {
        "status": "answered",
        "answer": "回答",
        "citations": [],
        "gaps": [],
    }
    payload.pop(missing)

    with pytest.raises(ValidationError):
        FinalResult.model_validate(payload)


def test_final_result_extra_field_rejected() -> None:
    with pytest.raises(ValidationError):
        FinalResult.model_validate(
            {
                "status": "answered",
                "answer": "回答",
                "citations": [],
                "gaps": [],
                "debug": "extra",
            }
        )


# --- JSON serialization round trips ---------------------------------------


@pytest.mark.parametrize(("payload", "expected_type"), VALID_ACTIONS)
def test_action_json_round_trip(
    payload: dict[str, Any], expected_type: type
) -> None:
    action = parse_agent_action(payload)

    dumped = action.model_dump_json()
    raw = json.loads(dumped)
    restored = parse_agent_action(raw)

    assert raw["action"] == payload["action"]
    assert isinstance(restored, expected_type)
    assert restored == action
    assert restored.model_dump() == action.model_dump()


def test_final_result_json_round_trip() -> None:
    result = FinalResult(
        status=TaskStatus.INSUFFICIENT_EVIDENCE,
        answer="现有资料不足，无法确认。",
        citations=[
            Citation(
                doc_id="hr-policy-001",
                quote="原文片段",
                version="v2",
                effective_date="2026-01-01",
            )
        ],
        gaps=["缺少报销标准", "缺少司龄认定细则"],
    )

    dumped = result.model_dump_json()
    raw = json.loads(dumped)
    restored = FinalResult.model_validate_json(dumped)

    assert raw["status"] == "insufficient_evidence"
    assert raw["citations"][0]["doc_id"] == "hr-policy-001"
    assert restored == result
