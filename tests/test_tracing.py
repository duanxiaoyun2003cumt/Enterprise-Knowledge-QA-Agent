"""Audit trace structure, persistence, and fake-run labeling."""

from __future__ import annotations

import json
from pathlib import Path

from knowledge_agent.schemas import TaskStatus
from tests.agent_helpers import (
    cite,
    doc_content,
    finish_action,
    make_agent,
    read_action,
    search_action,
)


def test_trace_records_steps_and_is_saved_as_json(tmp_path: Path) -> None:
    content = doc_content("D03")
    agent, _ = make_agent(
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
    trace = agent.trace.to_dict()
    path = agent.save_trace(tmp_path)

    assert result.status is TaskStatus.ANSWERED
    assert path == tmp_path / f"{trace['run_id']}.json"
    assert path.exists()
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded == trace

    assert trace["mode"] == "fake"
    assert trace["model"] == "fake-scripted-llm"
    assert trace["stop_reason"] == "finish"
    assert trace["final_result"]["status"] == "answered"
    assert trace["steps_used"] == 3
    assert trace["tool_calls_made"] == 2
    assert trace["format_repairs"] == 0
    assert trace["final_validation_retries"] == 0
    assert all(step["repair_kind"] is None for step in trace["steps"])
    assert len(agent.last_state.tool_results) == 2

    steps = trace["steps"]
    assert [step["action_name"] for step in steps] == [
        "search_documents",
        "read_document",
        "finish",
    ]
    assert steps[0]["next_action"] == "read_document"
    assert steps[1]["next_action"] == "finish"
    assert steps[0]["decision_note"] == "测试决策依据"
    assert steps[0]["tool_name"] == "search_documents"
    assert steps[1]["tool_name"] == "read_document"
    assert steps[2]["tool_name"] is None

    serialized = path.read_text(encoding="utf-8")
    assert "Authorization" not in serialized
    assert "api_key" not in serialized.lower()

    tool_steps = json.dumps(steps[:2], ensure_ascii=False)
    assert content not in tool_steps
