"""Dynamic decision paths and tool-error observations (FakeLLM flow tests)."""

from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_agent import agent as agent_module
from knowledge_agent import retrieval
from knowledge_agent.schemas import TaskStatus
from knowledge_agent.tools import InvalidArgumentError
from tests.agent_helpers import (
    cite,
    finish_action,
    last_observation,
    make_agent,
    read_action,
    search_action,
)


def test_empty_search_is_success_and_model_can_rewrite_query() -> None:
    agent, fake = make_agent(
        [
            search_action("完全无关的海洋生物学问题"),
            search_action("报销期限"),
            read_action("D01"),
            read_action("D02"),
            finish_action(
                "needs_clarification",
                "现有资料存在冲突：D01规定30日内提交，D02规定15日内提交，"
                "当前资料没有说明适用范围或优先级。请确认应适用哪份制度。",
                citations=[cite("D01"), cite("D02")],
                gaps=["缺少适用范围或制度优先级"],
            ),
        ],
        max_steps=6,
        max_tool_calls=5,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.NEEDS_CLARIFICATION
    assert {citation.doc_id for citation in result.citations} == {
        "D01",
        "D02",
    }
    assert agent.last_state.tool_calls == 4
    assert agent.last_state.tool_errors == []

    empty_observation = last_observation(fake, 1)
    assert '"ok": true' in empty_observation
    assert '"data": []' in empty_observation
    trace = agent.trace.to_dict()
    assert trace["final_result"]["status"] == "needs_clarification"
    assert trace["tool_calls_made"] == 4
    first_step = trace["steps"][0]
    assert first_step["tool_error_type"] is None
    assert first_step["observation_ok"] is True
    assert first_step["next_action"] == "search_documents"


def test_document_data_error_is_observed_then_model_chooses_tool_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    monkeypatch.setattr(retrieval, "DEFAULT_DATA_PATH", missing)

    agent, fake = make_agent(
        [
            search_action("报销期限"),
            finish_action(
                "tool_error",
                "知识库数据文件不可用，无法完成检索。",
                gaps=["数据文件缺失或损坏"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.TOOL_ERROR
    observation = last_observation(fake, 1)
    assert "DocumentDataError" in observation
    assert '"retryable": false' in observation
    assert agent.last_state.tool_errors[0]["error_type"] == "DocumentDataError"
    trace = agent.trace.to_dict()
    assert trace["steps"][0]["tool_error_type"] == "DocumentDataError"
    assert trace["final_result"]["status"] == "tool_error"
    assert trace["tool_calls_made"] == 1


def test_document_not_found_is_observed_then_tool_error() -> None:
    agent, fake = make_agent(
        [
            read_action("D99"),
            finish_action(
                "tool_error",
                "文档不存在，无法读取。",
                gaps=["文档 ID 不存在"],
            ),
        ]
    )

    result = agent.run("读取 D99")

    assert result.status is TaskStatus.TOOL_ERROR
    observation = last_observation(fake, 1)
    assert "DocumentNotFoundError" in observation
    assert '"retryable": true' in observation
    assert agent.last_state.tool_errors[0]["error_type"] == (
        "DocumentNotFoundError"
    )


def test_invalid_argument_error_is_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def bad_search(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise InvalidArgumentError("query rejected by tool")

    monkeypatch.setattr(agent_module, "search_documents", bad_search)

    agent, fake = make_agent(
        [
            search_action("报销期限"),
            finish_action(
                "tool_error",
                "工具参数错误，无法检索。",
                gaps=["检索参数无效"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.TOOL_ERROR
    observation = last_observation(fake, 1)
    assert "InvalidArgumentError" in observation
    assert '"retryable": true' in observation
    assert agent.last_state.tool_errors[0]["error_type"] == (
        "InvalidArgumentError"
    )


def test_empty_search_cannot_be_reported_as_tool_error() -> None:
    agent, fake = make_agent(
        [
            search_action("完全无关的海洋生物学问题"),
            finish_action(
                "tool_error",
                "工具执行失败。",
                gaps=["工具错误"],
            ),
            finish_action(
                "insufficient_evidence",
                "没有找到与问题相关的资料。",
                gaps=["知识库中没有相关文档"],
            ),
        ],
        max_steps=5,
        max_tool_calls=5,
    )

    result = agent.run("公司年会在哪里举办？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert len(fake.calls) == 3
    assert agent.last_state.tool_errors == []
    assert agent.last_state.validation_failures == 1
    validation_observation = last_observation(fake, 2)
    assert "tool_error 必须基于" in validation_observation
    step = agent.trace.to_dict()["steps"][1]
    assert step["action_validation"] == "invalid_final_result"
    assert step["validation_errors"]
