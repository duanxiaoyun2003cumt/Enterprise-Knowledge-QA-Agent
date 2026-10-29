"""Agent Loop mechanics: action mapping, observations, errors, and limits."""

from __future__ import annotations

import pytest

from knowledge_agent import agent as agent_module
from knowledge_agent.llm_client import LLMTimeoutError
from knowledge_agent.schemas import TaskStatus
from tests.agent_helpers import (
    ask_action,
    call_text,
    cite,
    doc_content,
    finish_action,
    last_observation,
    make_agent,
    read_action,
    search_action,
)


def test_search_action_calls_tool_and_observation_reaches_next_call() -> None:
    agent, fake = make_agent(
        [
            search_action("差旅住宿费报销需要哪些材料", top_k=3),
            finish_action(
                "insufficient_evidence",
                "当前候选不足以形成结论。",
                gaps=["需要进一步核实"],
            ),
        ]
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert len(fake.calls) == 2
    observation = last_observation(fake, 1)
    assert "Observation" in observation
    assert "D03" in observation
    assert "search_documents" in observation

    trace = agent.trace.to_dict()
    step = trace["steps"][0]
    assert step["action_name"] == "search_documents"
    assert step["action_params"]["query"] == "差旅住宿费报销需要哪些材料"
    assert step["tool_name"] == "search_documents"
    assert step["observation_ok"] is True
    assert step["next_action"] == "finish"
    assert step["decision_note"] == "测试决策依据"
    assert agent.last_state.tool_calls == 1
    assert agent.last_state.tool_results[0]["tool"] == "search_documents"
    assert agent.last_state.tool_results[0]["ok"] is True


def test_read_action_calls_tool_and_full_content_enters_context() -> None:
    content = doc_content("D03")
    agent, fake = make_agent(
        [
            search_action("差旅住宿费报销需要哪些材料"),
            read_action("D03"),
            finish_action(
                "answered",
                content,
                citations=[cite("D03")],
            ),
        ]
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.ANSWERED
    assert len(fake.calls) == 3
    assert content in call_text(fake, 2)
    assert agent.last_state.read_documents["D03"]["content"] == content
    assert agent.last_state.executed_tools == [
        "search_documents",
        "read_document",
    ]
    assert agent.last_state.tool_calls == 2


def test_invalid_json_is_not_executed_and_is_fed_back() -> None:
    agent, fake = make_agent(
        [
            "this is not json",
            finish_action(
                "insufficient_evidence",
                "无法获得可用证据。",
                gaps=["检索失败"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert agent.last_state.tool_calls == 0
    observation = last_observation(fake, 1)
    assert "JSON 解析失败" in observation
    assert "InvalidAction" in observation
    step = agent.trace.to_dict()["steps"][0]
    assert step["action_validation"] == "invalid_action"
    assert step["tool_name"] is None


def test_unknown_action_name_is_rejected_without_tool_execution() -> None:
    agent, fake = make_agent(
        [
            {"action": "noop", "decision_note": "测试"},
            finish_action(
                "insufficient_evidence",
                "无法获得可用证据。",
                gaps=["动作不可用"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert agent.last_state.tool_calls == 0
    assert "动作校验失败" in last_observation(fake, 1)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "action": "search_documents",
                "top_k": 3,
                "decision_note": "缺少 query",
            },
            id="missing-query",
        ),
        pytest.param(
            {
                "action": "read_document",
                "doc_id": 123,
                "decision_note": "doc_id 类型错误",
            },
            id="wrong-doc-id-type",
        ),
        pytest.param(
            {
                "action": "finish",
                "status": "answered",
                "answer": "缺少 decision_note",
                "citations": [],
                "gaps": [],
            },
            id="missing-decision-note",
        ),
        pytest.param(
            {
                "action": "search_documents",
                "query": "报销期限",
                "top_k": 6,
                "decision_note": "top_k 越界",
            },
            id="top-k-out-of-range",
        ),
        pytest.param(
            {
                "action": "search_documents",
                "query": "报销期限",
                "top_k": 3,
                "max_steps": 100,
                "decision_note": "试图修改预算",
            },
            id="extra-field-cannot-change-budget",
        ),
    ],
)
def test_malformed_action_payloads_do_not_execute_tools(
    payload: dict[str, object],
) -> None:
    agent, fake = make_agent(
        [
            payload,
            finish_action(
                "insufficient_evidence",
                "动作格式不可用，无法继续。",
                gaps=["模型动作格式错误"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert agent.last_state.tool_calls == 0
    assert "动作校验失败" in last_observation(fake, 1)


def test_format_repair_budget_stops_with_incomplete() -> None:
    agent, fake = make_agent(
        ["bad output", "still bad output"],
        max_steps=5,
        max_format_repairs=1,
        max_final_validation_retries=2,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 2
    assert agent.last_state.tool_calls == 0
    assert agent.last_state.format_repairs == 1
    assert agent.last_state.format_failures == 2
    assert agent.last_state.final_validation_retries == 0
    assert agent.last_state.validation_failures == 2
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "format_budget"
    assert trace["format_repairs"] == 1
    assert trace["format_failures"] == 2
    assert "JSON 动作" in result.answer


def test_max_steps_limit_returns_incomplete() -> None:
    agent, fake = make_agent(
        [search_action("差旅住宿费报销需要哪些材料")] * 5,
        max_steps=3,
        max_tool_calls=10,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 3
    assert agent.last_state.step == 3
    assert agent.last_state.tool_calls == 3
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "max_steps"
    assert trace["final_result"]["status"] == "incomplete"
    assert "最大执行步数" in result.answer


def test_max_tool_calls_limit_stops_immediately() -> None:
    agent, fake = make_agent(
        [search_action("差旅住宿费报销需要哪些材料")] * 5,
        max_steps=10,
        max_tool_calls=2,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 2
    assert agent.last_state.tool_calls == 2
    assert "最大工具调用次数" in result.answer

    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "tool_limit"
    assert trace["tool_calls_made"] == 2
    assert trace["steps_used"] == 2
    assert trace["final_result"]["status"] == "incomplete"
    assert not [
        step
        for step in trace["steps"]
        if step["tool_error_type"] == "ToolBudgetExhausted"
    ]


def test_zero_tool_budget_blocks_tool_action_without_second_model_call() -> None:
    agent, fake = make_agent(
        [search_action("差旅住宿费报销需要哪些材料"), finish_action(
            "insufficient_evidence",
            "无法检索。",
            gaps=["工具预算为零"],
        )],
        max_steps=3,
        max_tool_calls=0,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 1
    assert agent.last_state.tool_calls == 0
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "tool_limit"
    assert trace["steps"][0]["tool_error_type"] == "ToolBudgetExhausted"


def test_tool_retries_consume_the_tool_budget() -> None:
    agent, fake = make_agent(
        [read_action("D99"), read_action("D99"), finish_action(
            "tool_error",
            "文档不存在。",
            gaps=["文档不存在"],
        )],
        max_steps=5,
        max_tool_calls=2,
    )

    result = agent.run("读取 D99")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 2
    assert agent.last_state.tool_calls == 2
    assert len(agent.last_state.tool_errors) == 2
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "tool_limit"
    assert trace["tool_calls_made"] == 2
    assert [
        step["tool_error_type"]
        for step in trace["steps"]
    ] == ["DocumentNotFoundError", "DocumentNotFoundError"]


def test_step_limit_with_zero_tool_budget_keeps_max_steps_reason() -> None:
    agent, fake = make_agent(
        ["not json"] * 3,
        max_steps=2,
        max_tool_calls=0,
        max_format_repairs=10,
        max_final_validation_retries=10,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 2
    assert agent.last_state.tool_calls == 0
    assert "最大执行步数" in result.answer
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "max_steps"
    assert trace["final_result"]["status"] == "incomplete"


def test_format_errors_count_against_step_limit() -> None:
    agent, fake = make_agent(
        ["not json"] * 5,
        max_steps=3,
        max_tool_calls=5,
        max_format_repairs=10,
        max_final_validation_retries=10,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 3
    assert agent.last_state.step == 3
    assert agent.last_state.tool_calls == 0
    assert agent.last_state.validation_failures == 3
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "max_steps"
    assert trace["steps_used"] == 3


def test_model_api_error_returns_tool_error_without_retry_loop() -> None:
    agent, fake = make_agent([LLMTimeoutError("timed out")])

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.TOOL_ERROR
    assert len(fake.calls) == 1
    assert "模型或系统调用失败" in result.answer
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "model_error"
    assert trace["final_result"]["status"] == result.status.value
    assert trace["tool_calls_made"] == 0


def test_unknown_tool_exception_is_sanitized_and_observed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("secret boom detail")

    monkeypatch.setattr(agent_module, "search_documents", boom)
    agent, fake = make_agent(
        [
            search_action("差旅住宿费报销需要哪些材料"),
            finish_action(
                "tool_error",
                "工具执行异常，无法完成检索。",
                gaps=["工具故障"],
            ),
        ]
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.TOOL_ERROR
    observation = last_observation(fake, 1)
    assert "UnexpectedToolError" in observation
    assert "secret boom detail" not in observation
    assert "Traceback" not in observation
    assert agent.last_state.tool_errors[0]["error_type"] == "UnexpectedToolError"
    assert agent.trace.to_dict()["steps"][0]["tool_error_type"] == (
        "UnexpectedToolError"
    )


def test_ask_user_action_terminates_with_clarification() -> None:
    agent, fake = make_agent(
        [ask_action("请问具体是哪一类费用？", "问题存在歧义")]
    )

    result = agent.run("费用怎么报？")

    assert result.status is TaskStatus.NEEDS_CLARIFICATION
    assert result.answer == "请问具体是哪一类费用？"
    assert result.gaps == ["问题存在歧义"]
    assert agent.trace.to_dict()["stop_reason"] == "ask_user"
    assert agent.last_state.tool_calls == 0
