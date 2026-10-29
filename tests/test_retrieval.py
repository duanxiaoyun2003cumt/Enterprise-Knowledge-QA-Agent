"""Phase 2 tests: synthetic data, retrieval, tools, and error contracts.

No model API key, network access, or Agent Loop is involved.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from knowledge_agent import retrieval
from knowledge_agent.retrieval import (
    DocumentDataError,
    DocumentNotFoundError,
    InvalidArgumentError,
    expand_query,
    get_document,
    load_documents,
    search_documents,
)
from knowledge_agent.schemas import ReadDocumentAction, SearchDocumentsAction
from knowledge_agent.tools import (
    read_document,
    search_documents as tool_search_documents,
)

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "documents.json"


def _sample_document(
    doc_id: str = "D01", **overrides: Any
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "doc_id": doc_id,
        "title": "测试制度",
        "version_date": "2025-01-01",
        "content": "测试文档内容。",
    }
    document.update(overrides)
    return document


def _write_json(tmp_path: Path, payload: Any) -> Path:
    path = tmp_path / "documents.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return path


@pytest.fixture(scope="module")
def raw_documents() -> list[dict[str, Any]]:
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def raw_by_id(
    raw_documents: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {document["doc_id"]: document for document in raw_documents}


# --- A. Data loading -------------------------------------------------------


def test_real_data_has_four_documents_with_unique_ids(
    raw_documents: list[dict[str, Any]],
) -> None:
    assert len(raw_documents) == 4
    ids = [document["doc_id"] for document in raw_documents]
    assert ids == ["D01", "D02", "D03", "D04"]
    assert len(ids) == len(set(ids))
    for document in raw_documents:
        assert set(document) == {
            "doc_id",
            "title",
            "version_date",
            "content",
        }
        assert document["content"].strip()


def test_load_documents_returns_validated_records() -> None:
    documents = load_documents()

    assert len(documents) == 4
    assert {document["doc_id"] for document in documents} == {
        "D01",
        "D02",
        "D03",
        "D04",
    }


def test_real_data_preserves_required_business_facts(
    raw_by_id: dict[str, dict[str, Any]],
) -> None:
    d01 = raw_by_id["D01"]
    d02 = raw_by_id["D02"]
    d03 = raw_by_id["D03"]
    d04 = raw_by_id["D04"]

    assert "30日" in d01["content"]
    assert "15日" in d02["content"]
    assert d01["content"] != d02["content"]
    for policy in (d01, d02):
        assert "适用" not in policy["content"]
        assert "优先" not in policy["content"]
    assert all(term in d03["content"] for term in ("发票", "订单", "入住凭证"))
    assert "SAML 2.0" in d04["content"]
    assert "单点登录" in d04["content"]


def test_missing_data_file_raises_clear_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing.json"

    with pytest.raises(DocumentDataError, match="not found"):
        load_documents(missing)


def test_invalid_json_raises_clear_error(tmp_path: Path) -> None:
    path = tmp_path / "documents.json"
    path.write_text("{not valid json", encoding="utf-8")

    with pytest.raises(DocumentDataError, match="Invalid JSON"):
        load_documents(path)


def test_non_array_root_is_rejected(tmp_path: Path) -> None:
    path = _write_json(tmp_path, {"documents": []})

    with pytest.raises(DocumentDataError, match="JSON array"):
        load_documents(path)


def test_non_object_document_is_rejected(tmp_path: Path) -> None:
    path = _write_json(tmp_path, [42])

    with pytest.raises(DocumentDataError, match="JSON object"):
        load_documents(path)


def test_duplicate_doc_id_is_rejected(tmp_path: Path) -> None:
    document = _sample_document()
    path = _write_json(tmp_path, [document, dict(document)])

    with pytest.raises(DocumentDataError, match="Duplicate doc_id"):
        load_documents(path)


@pytest.mark.parametrize(
    "field", ["doc_id", "title", "version_date", "content"]
)
def test_missing_required_field_is_rejected(
    tmp_path: Path, field: str
) -> None:
    document = _sample_document()
    document.pop(field)
    path = _write_json(tmp_path, [document])

    with pytest.raises(DocumentDataError, match=field):
        load_documents(path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("doc_id", "", id="empty-doc-id"),
        pytest.param("title", "   ", id="blank-title"),
        pytest.param("content", "", id="empty-content"),
        pytest.param("version_date", "2025/01/01", id="wrong-date-format"),
        pytest.param("version_date", "2025-02-30", id="impossible-date"),
    ],
)
def test_invalid_field_values_are_rejected(
    tmp_path: Path, field: str, value: str
) -> None:
    document = _sample_document(**{field: value})
    path = _write_json(tmp_path, [document])

    with pytest.raises(DocumentDataError, match=field):
        load_documents(path)


def test_wrong_field_type_is_rejected(tmp_path: Path) -> None:
    document = _sample_document(content=123)
    path = _write_json(tmp_path, [document])

    with pytest.raises(DocumentDataError, match="must be a string"):
        load_documents(path)


def test_extra_fields_are_not_exposed(tmp_path: Path) -> None:
    document = _sample_document(extra="ignored")
    path = _write_json(tmp_path, [document])

    loaded = load_documents(path)

    assert loaded == [_sample_document()]


# --- B. Basic retrieval ----------------------------------------------------


def test_reimbursement_deadline_recalls_both_policy_documents() -> None:
    results = search_documents("报销期限", top_k=5)
    ids = {result["doc_id"] for result in results}

    assert ids == {"D01", "D02"}


def test_travel_material_query_recalls_d03() -> None:
    results = search_documents(
        "差旅住宿费报销需要哪些材料", top_k=5
    )

    assert [result["doc_id"] for result in results] == ["D03"]


def test_product_sso_query_recalls_d04() -> None:
    results = search_documents(
        "产品 X 企业版支持什么单点登录协议", top_k=5
    )

    assert [result["doc_id"] for result in results] == ["D04"]


def test_unrelated_query_returns_empty_list() -> None:
    assert search_documents("公司年会在哪里举办？", top_k=5) == []


def test_top_k_limits_results() -> None:
    all_results = search_documents("报销", top_k=5)
    assert len(all_results) == 3

    for top_k in (1, 2, 3):
        results = search_documents("报销", top_k=top_k)
        assert len(results) == top_k


def test_same_input_gives_stable_order_and_scores() -> None:
    first = search_documents("报销期限", top_k=5)
    second = search_documents("报销期限", top_k=5)

    assert first == second


def test_results_do_not_contain_duplicate_documents() -> None:
    results = search_documents("报销", top_k=5)
    ids = [result["doc_id"] for result in results]

    assert len(ids) == len(set(ids))


def test_results_are_sorted_by_descending_score() -> None:
    results = search_documents("报销", top_k=5)
    scores = [result["score"] for result in results]

    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)


def test_result_shape_and_snippet_are_verbatim(
    raw_by_id: dict[str, dict[str, Any]],
) -> None:
    results = search_documents("报销期限", top_k=5)

    assert results
    for result in results:
        assert set(result) == {
            "doc_id",
            "title",
            "version_date",
            "snippet",
            "score",
        }
        raw = raw_by_id[result["doc_id"]]
        assert result["title"] == raw["title"]
        assert result["version_date"] == raw["version_date"]
        assert result["snippet"] in raw["content"]
        assert result["snippet"] == raw["content"]
        assert isinstance(result["score"], float)
        assert result["score"] > 0


def test_query_whitespace_is_trimmed() -> None:
    assert search_documents("  报销期限  ", top_k=5) == search_documents(
        "报销期限", top_k=5
    )


def test_default_top_k_is_three() -> None:
    assert len(search_documents("报销")) == 3


def test_equal_scores_are_broken_by_doc_id(tmp_path: Path) -> None:
    identical_content = "费用发生后30日内提交报销申请。"
    first = _sample_document(
        doc_id="D02", title="制度B", content=identical_content
    )
    second = _sample_document(
        doc_id="D01", title="制度A", content=identical_content
    )
    path = _write_json(tmp_path, [first, second])

    results = search_documents("报销期限", top_k=5, path=path)

    assert [result["doc_id"] for result in results] == ["D01", "D02"]
    assert results[0]["score"] == results[1]["score"]


def test_title_match_outranks_content_match(tmp_path: Path) -> None:
    title_match = _sample_document(
        doc_id="D01", title="报销期限", content="无关内容。"
    )
    content_match = _sample_document(
        doc_id="D02", title="无关标题", content="报销期限。"
    )
    path = _write_json(tmp_path, [title_match, content_match])

    results = search_documents("报销期限", top_k=5, path=path)

    assert [result["doc_id"] for result in results] == ["D01", "D02"]
    assert results[0]["score"] > results[1]["score"]


# --- C. Query expansion ----------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_ids"),
    [
        pytest.param(
            "报销最晚几天提交", {"D01", "D02"}, id="reimbursement-deadline"
        ),
        pytest.param(
            "差旅住宿的票据要准备啥", {"D03"}, id="travel-materials"
        ),
        pytest.param("酒店费用要提供什么", {"D03"}, id="hotel-materials"),
        pytest.param(
            "产品 X 怎么做单点登录", {"D04"}, id="product-sso"
        ),
    ],
)
def test_query_expansion_recalls_expected_documents(
    query: str, expected_ids: set[str]
) -> None:
    ids = {
        result["doc_id"]
        for result in search_documents(query, top_k=5)
    }

    assert ids == expected_ids


def test_expand_query_returns_synonyms_without_changing_query() -> None:
    query = "差旅住宿的票据要准备啥"
    original = query

    expanded = expand_query(query)

    assert query == original
    assert "发票" in expanded
    assert "入住凭证" in expanded
    assert "报销期限" not in expanded

    deadline_terms = expand_query("报销最晚几天提交")
    assert "期限" in deadline_terms
    assert "多少天" in deadline_terms


# --- D. Reading raw documents ---------------------------------------------


def test_read_document_returns_exact_raw_record(
    raw_by_id: dict[str, dict[str, Any]],
) -> None:
    for doc_id, raw in raw_by_id.items():
        assert read_document(doc_id) == raw


def test_read_document_missing_id_raises_clear_error() -> None:
    with pytest.raises(DocumentNotFoundError, match="D99"):
        read_document("D99")


@pytest.mark.parametrize(
    "doc_id", ["", "   ", 123, None, ["D01"]]
)
def test_read_document_invalid_id_is_rejected(doc_id: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        read_document(doc_id)


def test_get_document_returns_an_independent_copy() -> None:
    first = get_document("D01")
    first["content"] = "被调用方修改"

    second = get_document("D01")

    assert second["content"] != "被调用方修改"


# --- E. Conflict data retention -------------------------------------------


def test_reimbursement_conflict_candidates_are_both_preserved(
    raw_by_id: dict[str, dict[str, Any]],
) -> None:
    results = search_documents("报销期限", top_k=5)
    ids = [result["doc_id"] for result in results]

    assert set(ids) == {"D01", "D02"}
    assert "D01" in ids and "D02" in ids

    d01 = read_document("D01")
    d02 = read_document("D02")

    assert d01["content"] == raw_by_id["D01"]["content"]
    assert d02["content"] == raw_by_id["D02"]["content"]
    assert "30日" in d01["content"]
    assert "15日" in d02["content"]
    assert d01["content"] != d02["content"]

    # The older document is not filtered, and retrieval returns no final
    # answer/status fields that would imply a policy decision.
    assert d01["version_date"] == "2025-01-15"
    assert d02["version_date"] == "2025-04-01"
    for result in results:
        assert "status" not in result
        assert "answer" not in result


# --- F. Arguments and error contracts -------------------------------------


@pytest.mark.parametrize(
    "query", ["", "   ", None, 123, ["报销期限"]]
)
def test_invalid_query_is_rejected(query: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        search_documents(query)


@pytest.mark.parametrize(
    "top_k", [0, -1, 6, "3", 2.5, True, None]
)
def test_invalid_top_k_is_rejected(top_k: Any) -> None:
    with pytest.raises(InvalidArgumentError):
        search_documents("报销期限", top_k=top_k)


@pytest.mark.parametrize("top_k", [1, 5])
def test_top_k_boundaries_are_accepted(top_k: int) -> None:
    results = search_documents("报销", top_k=top_k)

    assert len(results) <= top_k


def test_empty_results_are_not_execution_errors() -> None:
    assert search_documents("完全无关的海洋生物学问题", top_k=5) == []


def test_tool_results_and_documents_are_json_serializable() -> None:
    results = tool_search_documents("报销期限", top_k=5)
    encoded_results = json.dumps(results, ensure_ascii=False)
    assert "D01" in encoded_results and "D02" in encoded_results

    document = read_document("D01")
    json.dumps(document, ensure_ascii=False)


def test_tool_search_distinguishes_empty_result_from_data_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert tool_search_documents("完全无关的海洋生物学问题") == []

    monkeypatch.setattr(
        retrieval, "DEFAULT_DATA_PATH", tmp_path / "missing.json"
    )

    with pytest.raises(DocumentDataError):
        tool_search_documents("报销期限")


def test_tool_read_distinguishes_data_error_from_missing_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with pytest.raises(DocumentNotFoundError):
        read_document("D99")

    monkeypatch.setattr(
        retrieval, "DEFAULT_DATA_PATH", tmp_path / "missing.json"
    )

    with pytest.raises(DocumentDataError):
        read_document("D01")


def test_tool_invalid_arguments_are_value_errors() -> None:
    with pytest.raises(ValueError):
        tool_search_documents("", top_k=3)


# --- Phase 1 schema compatibility -----------------------------------------


def test_search_tool_accepts_phase_one_action_arguments() -> None:
    action = SearchDocumentsAction(
        action="search_documents",
        query="报销期限",
        top_k=5,
        decision_note="测试阶段一动作参数兼容性",
    )

    results = tool_search_documents(action.query, action.top_k)

    assert {"D01", "D02"} <= {
        result["doc_id"] for result in results
    }


def test_read_tool_accepts_phase_one_action_doc_id() -> None:
    action = ReadDocumentAction(
        action="read_document",
        doc_id="D01",
        decision_note="测试阶段一动作参数兼容性",
    )

    document = read_document(action.doc_id)

    assert document["doc_id"] == "D01"
    assert document["content"].strip()
