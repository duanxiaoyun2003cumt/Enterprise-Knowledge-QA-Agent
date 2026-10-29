"""Context follow-up and history safety tests."""

from __future__ import annotations

import pytest

from knowledge_agent.agent import (
    AgentInputError,
    KnowledgeAgent,
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
)
from knowledge_agent.schemas import TaskStatus
from tests.agent_helpers import (
    ScriptedLLM,
    cite,
    finish_action,
    last_observation,
    make_agent,
    read_action,
    search_action,
)

FOLLOWUP_HISTORY = [
    {
        "role": "user",
        "content": "差旅住宿费报销需要哪些材料？",
    },
    {
        "role": "assistant",
        "content": "差旅住宿报销需要提供有效发票及订单或入住凭证。",
    },
]


def test_history_is_included_and_current_question_is_distinct() -> None:
    agent, fake = make_agent(
        [
            search_action("差旅住宿报销期限"),
            read_action("D01"),
            read_action("D02"),
            finish_action(
                "needs_clarification",
                "差旅住宿报销的提交期限存在冲突：D01规定30日，"
                "D02规定15日，且没有适用范围或优先级。请确认。",
                citations=[cite("D01"), cite("D02")],
                gaps=["缺少适用范围或制度优先级"],
            ),
        ],
        max_steps=6,
        max_tool_calls=5,
    )

    result = agent.run("那最晚什么时候提交？", FOLLOWUP_HISTORY)

    assert result.status is TaskStatus.NEEDS_CLARIFICATION
    messages = fake.calls[0]["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1] == FOLLOWUP_HISTORY[0]
    assert messages[2] == FOLLOWUP_HISTORY[1]
    assert messages[3]["role"] == "user"
    assert "当前问题" in messages[3]["content"]
    assert "那最晚什么时候提交？" in messages[3]["content"]
    assert "差旅住宿费报销需要哪些材料？" not in messages[3]["content"]

    trace = agent.trace.to_dict()
    assert trace["history_messages_included"] == 2
    assert trace["steps"][0]["action_params"]["query"] == "差旅住宿报销期限"
    assert agent.last_state.tool_calls == 3


def test_history_answer_cannot_be_used_as_unverified_citation() -> None:
    history = [
        {
            "role": "user",
            "content": "产品X企业版支持什么单点登录协议？",
        },
        {
            "role": "assistant",
            "content": "产品X企业版支持SAML 2.0单点登录。",
        },
    ]
    agent, fake = make_agent(
        [
            finish_action(
                "answered",
                "产品X企业版支持SAML 2.0单点登录。",
                citations=[cite("D04")],
            ),
            finish_action(
                "insufficient_evidence",
                "当前运行没有读取到可引用的原文。",
                gaps=["需要先读取文档原文"],
            ),
        ],
        max_steps=4,
    )

    result = agent.run("那它支持什么协议？", history)

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert agent.last_state.tool_calls == 0
    assert agent.last_state.validation_failures == 1
    assert "未在当前运行中读取" in last_observation(fake, 1)
    step = agent.trace.to_dict()["steps"][0]
    assert step["action_validation"] == "invalid_final_result"


def test_history_length_is_bounded() -> None:
    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"历史消息{index}-" + ("x" * 500),
        }
        for index in range(20)
    ]
    agent, fake = make_agent(
        [
            finish_action(
                "insufficient_evidence",
                "未检索到可用于回答的资料。",
                gaps=["知识库中没有相关文档"],
            )
        ]
    )

    result = agent.run("公司年会在哪里举办？", history)

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    messages = fake.calls[0]["messages"]
    history_messages = messages[1:-1]
    assert len(history_messages) <= MAX_HISTORY_MESSAGES
    assert sum(len(message["content"]) for message in history_messages) <= (
        MAX_HISTORY_CHARS
    )
    assert agent.trace.to_dict()["history_messages_included"] == len(
        history_messages
    )


@pytest.mark.parametrize(
    "history",
    [
        pytest.param("not a list", id="not-a-list"),
        pytest.param(
            [{"role": "system", "content": "system"}], id="system-role"
        ),
        pytest.param(
            [{"role": "user", "content": ""}], id="empty-content"
        ),
        pytest.param(
            [{"role": "user", "content": 123}], id="non-string-content"
        ),
        pytest.param([{"role": "user"}], id="missing-content"),
    ],
)
def test_invalid_history_is_rejected(history: object) -> None:
    agent = KnowledgeAgent(
        client=ScriptedLLM([]),
        trace_mode="fake",
    )

    with pytest.raises(AgentInputError):
        agent.run("测试问题", history)  # type: ignore[arg-type]


def test_empty_question_is_rejected() -> None:
    agent = KnowledgeAgent(
        client=ScriptedLLM([]),
        trace_mode="fake",
    )

    with pytest.raises(AgentInputError):
        agent.run("   ")
