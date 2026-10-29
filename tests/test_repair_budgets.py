"""Independent format-repair and final-validation retry budgets."""

from __future__ import annotations

import pytest

from knowledge_agent import agent as agent_module
from knowledge_agent.schemas import TaskStatus
from knowledge_agent.tools import DocumentDataError
from tests.agent_helpers import (
    cite,
    doc_content,
    finish_action,
    last_observation,
    make_agent,
    read_action,
    search_action,
)


def test_format_repair_succeeds_only_after_full_schema_validation() -> None:
    content = doc_content("D03")
    agent, fake = make_agent(
        [
            "not json at all",
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
    assert len(fake.calls) == 4
    assert agent.last_state.tool_calls == 2
    assert agent.last_state.format_repairs == 1
    assert agent.last_state.final_validation_retries == 0
    assert "required_action_schema" in last_observation(fake, 1)
    trace = agent.trace.to_dict()
    assert trace["format_repairs"] == 1
    assert trace["final_validation_retries"] == 0
    assert trace["steps"][0]["repair_kind"] == "format"
    assert trace["steps"][1]["repair_kind"] is None
    assert trace["stop_reason"] == "finish"


def test_format_repair_failure_stops_without_executing_tool() -> None:
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
    assert agent.trace.to_dict()["stop_reason"] == "format_budget"
    assert agent.trace.to_dict()["steps"][0]["tool_name"] is None
    assert agent.trace.to_dict()["steps"][1]["tool_name"] is None


def test_format_and_final_validation_budgets_are_independent() -> None:
    content = doc_content("D03")
    agent, fake = make_agent(
        [
            "bad format",
            read_action("D03"),
            finish_action(
                "answered",
                "需要提供有效发票。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                content,
                citations=[cite("D03")],
            ),
        ],
        max_steps=6,
        max_format_repairs=1,
        max_final_validation_retries=1,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.ANSWERED
    assert agent.last_state.format_repairs == 1
    assert agent.last_state.final_validation_retries == 1
    assert agent.last_state.tool_calls == 1
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "finish"
    assert [step["repair_kind"] for step in trace["steps"]] == [
        "format",
        None,
        "final_validation",
        None,
    ]
    assert trace["format_repairs"] == 1
    assert trace["final_validation_retries"] == 1


def test_final_validation_retry_limit_stops_before_another_action() -> None:
    agent, fake = make_agent(
        [
            read_action("D03"),
            finish_action(
                "answered",
                "需要提供发票。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                "仍然引用错误。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                doc_content("D03"),
                citations=[cite("D03")],
            ),
        ],
        max_steps=6,
        max_format_repairs=2,
        max_final_validation_retries=1,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 3
    assert agent.last_state.tool_calls == 1
    assert agent.last_state.format_repairs == 0
    assert agent.last_state.final_validation_retries == 1
    assert agent.last_state.final_validation_failures == 2
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "final_validation_budget"
    assert trace["final_validation_retries"] == 1
    assert trace["final_validation_failures"] == 2
    assert trace["format_repairs"] == 0


def test_final_validation_budget_counts_retries_not_terminal_failure() -> None:
    """Three failed results with MAX=2 mean two retries, not three.

    The third failure is the event that exhausts the budget; it must be
    reported as a failure but must not be counted as another retry.
    """

    agent, fake = make_agent(
        [
            read_action("D03"),
            finish_action(
                "answered",
                "错误引用一。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                "错误引用二。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                "错误引用三。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
        ],
        max_steps=6,
        max_format_repairs=2,
        max_final_validation_retries=2,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 4
    assert agent.last_state.final_validation_failures == 3
    assert agent.last_state.final_validation_retries == 2
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "final_validation_budget"
    assert trace["steps_used"] == 4
    assert trace["final_validation_failures"] == 3
    assert trace["final_validation_retries"] == 2
    assert [
        step["repair_kind"]
        for step in trace["steps"]
    ] == [None, "final_validation", "final_validation", "final_validation"]


def test_format_budget_counts_repairs_not_terminal_failure() -> None:
    """Three malformed outputs with MAX=2 mean two repairs, not three."""

    agent, fake = make_agent(
        ["bad output one", "bad output two", "bad output three"],
        max_steps=6,
        max_format_repairs=2,
        max_final_validation_retries=2,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 3
    assert agent.last_state.format_failures == 3
    assert agent.last_state.format_repairs == 2
    trace = agent.trace.to_dict()
    assert trace["stop_reason"] == "format_budget"
    assert trace["steps_used"] == 3
    assert trace["format_failures"] == 3
    assert trace["format_repairs"] == 2


def test_final_validation_failure_does_not_consume_format_budget() -> None:
    content = doc_content("D03")
    agent, fake = make_agent(
        [
            read_action("D03"),
            finish_action(
                "answered",
                "错误引用。",
                citations=[{"doc_id": "D99", "quote": "伪造引用"}],
            ),
            finish_action(
                "answered",
                content,
                citations=[cite("D03")],
            ),
        ],
        max_final_validation_retries=1,
        max_format_repairs=1,
    )

    result = agent.run("差旅住宿费报销需要哪些材料？")

    assert result.status is TaskStatus.ANSWERED
    assert agent.last_state.format_repairs == 0
    assert agent.last_state.final_validation_retries == 1
    assert agent.trace.to_dict()["stop_reason"] == "finish"


def test_repair_requests_still_count_against_max_steps() -> None:
    agent, fake = make_agent(
        ["bad"] * 5,
        max_steps=3,
        max_tool_calls=5,
        max_format_repairs=10,
        max_final_validation_retries=10,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INCOMPLETE
    assert len(fake.calls) == 3
    assert agent.last_state.format_repairs == 2
    assert agent.last_state.format_failures == 3
    assert agent.trace.to_dict()["stop_reason"] == "max_steps"
    assert agent.trace.to_dict()["steps_used"] == 3


def test_tool_errors_do_not_consume_repair_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_search(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise DocumentDataError("document data is broken")

    monkeypatch.setattr(agent_module, "search_documents", broken_search)
    agent, fake = make_agent(
        [
            search_action("报销期限"),
            finish_action(
                "tool_error",
                "数据文件不可用。",
                gaps=["数据错误"],
            ),
        ],
        max_format_repairs=2,
        max_final_validation_retries=2,
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.TOOL_ERROR
    assert agent.last_state.format_repairs == 0
    assert agent.last_state.final_validation_retries == 0
    assert len(agent.last_state.tool_errors) == 1
    assert agent.trace.to_dict()["format_repairs"] == 0
    assert agent.trace.to_dict()["final_validation_retries"] == 0


def test_format_repair_error_observation_is_not_a_tool_result() -> None:
    agent, fake = make_agent(
        [
            '{"action":"search_documents","query":"报销期限","top_k":3,'
            '"decision_note":"extra data"}{"extra":true}',
            finish_action(
                "insufficient_evidence",
                "无法解析动作，未执行检索。",
                gaps=["动作格式错误"],
            ),
        ]
    )

    result = agent.run("报销期限是多久？")

    assert result.status is TaskStatus.INSUFFICIENT_EVIDENCE
    assert agent.last_state.tool_calls == 0
    assert agent.last_state.tool_results == []
    assert "previous_output_preview" in last_observation(fake, 1)
    assert agent.trace.to_dict()["format_repairs"] == 1
