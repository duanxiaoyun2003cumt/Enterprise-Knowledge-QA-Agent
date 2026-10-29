"""Local, explainable keyword retrieval over the synthetic knowledge base.

The knowledge base is loaded from ``data/documents.json``; this module never
duplicates document content. Retrieval is a deterministic keyword/synonym
baseline, not an embedding search and not an answer-quality judge.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from .schemas import NonEmptyStr, TopK

__all__ = [
    "DocumentDataError",
    "DocumentNotFoundError",
    "InvalidArgumentError",
    "RetrievalError",
    "SYNONYM_GROUPS",
    "expand_query",
    "get_document",
    "load_documents",
    "normalize_text",
    "search_documents",
]

DEFAULT_DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "documents.json"
DEFAULT_TOP_K = 3
SNIPPET_MAX_CHARS = 200

REQUIRED_DOCUMENT_FIELDS = ("doc_id", "title", "version_date", "content")

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_WHITESPACE_PATTERN = re.compile(r"\s+")
_LATIN_TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_CJK_RUN_PATTERN = re.compile("[一-鿿]+")

_NON_EMPTY_TEXT_ADAPTER = TypeAdapter(NonEmptyStr)
_TOP_K_ADAPTER = TypeAdapter(TopK)

# Curated synonym groups. A query activates a group when it contains any
# surface term; all terms in that group then become allowed retrieval terms.
# This is deliberately small and reviewable, not a general thesaurus.
SYNONYM_GROUPS: dict[str, tuple[str, ...]] = {
    "reimbursement": ("报销", "费用", "报销单", "申请"),
    "deadline": (
        "期限",
        "时限",
        "最晚",
        "截止",
        "几天",
        "多少天",
        "多久",
        "日内",
        "提交",
    ),
    "travel": ("差旅", "差旅住宿", "住宿", "酒店", "出差", "入住", "行程"),
    "materials": (
        "材料",
        "票据",
        "发票",
        "订单",
        "入住凭证",
        "凭证",
        "提供",
        "准备",
    ),
    "product_x": ("产品x", "企业版"),
    "sso": ("单点登录", "sso", "saml", "saml2.0"),
}

# Generic question words ignored by the no-concept fallback. Domain terms such
# as 报销/住宿 remain searchable and are handled by SYNONYM_GROUPS instead.
_FALLBACK_STOP_TERMS = frozenset(
    {
        "什么",
        "怎么",
        "如何",
        "哪些",
        "哪个",
        "哪里",
        "需要",
        "可以",
        "是否",
        "请问",
        "一下",
        "这个",
        "那个",
        "多少",
        "为什么",
        "我们",
        "你们",
        "他们",
        "以及",
        "或者",
        "还是",
        "支持",
        "问题",
        "文档",
    }
)

TITLE_WEIGHT = 3.0
CONTENT_WEIGHT = 1.0
ORIGINAL_TERM_MULTIPLIER = 2.0
EXPANDED_TERM_MULTIPLIER = 1.0
PHRASE_TITLE_BONUS = 4.0
PHRASE_CONTENT_BONUS = 1.5


class RetrievalError(Exception):
    """Base class for retrieval and knowledge-base failures."""


class DocumentDataError(RetrievalError):
    """The document data file is missing, unreadable, or structurally invalid."""


class InvalidArgumentError(ValueError, RetrievalError):
    """A search/read argument violates the public tool contract."""


class DocumentNotFoundError(LookupError, RetrievalError):
    """The requested document ID does not exist in the knowledge base."""


def normalize_text(text: str) -> str:
    """Normalize text for substring matching.

    NFKC folds full-width characters, ``casefold`` handles English case, and
    whitespace is removed so ``产品 X`` matches ``产品X``. Punctuation is kept
    because terms such as ``saml2.0`` must stay intact.
    """
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _WHITESPACE_PATTERN.sub("", normalized)


def _validate_non_empty_text(value: Any, field_name: str) -> str:
    try:
        return _NON_EMPTY_TEXT_ADAPTER.validate_python(value)
    except ValidationError as exc:
        raise InvalidArgumentError(
            f"{field_name} must be a non-empty string."
        ) from exc


def _validate_query(query: Any) -> str:
    return _validate_non_empty_text(query, "query")


def _validate_top_k(top_k: Any) -> int:
    try:
        return _TOP_K_ADAPTER.validate_python(top_k)
    except ValidationError as exc:
        raise InvalidArgumentError(
            "top_k must be an integer between 1 and 5."
        ) from exc


def _validate_doc_id(doc_id: Any) -> str:
    return _validate_non_empty_text(doc_id, "doc_id")


def _validate_version_date(value: str, index: int, doc_id: str) -> None:
    if not _DATE_PATTERN.fullmatch(value):
        raise DocumentDataError(
            f"Document at index {index} ({doc_id!r}) has invalid version_date "
            f"{value!r}; expected YYYY-MM-DD."
        )
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise DocumentDataError(
            f"Document at index {index} ({doc_id!r}) has invalid version_date "
            f"{value!r}; the calendar date does not exist."
        ) from exc


def _validate_document(raw_document: Any, index: int) -> dict[str, str]:
    if not isinstance(raw_document, dict):
        raise DocumentDataError(
            f"Document at index {index} must be a JSON object."
        )

    missing = [
        field for field in REQUIRED_DOCUMENT_FIELDS if field not in raw_document
    ]
    if missing:
        raise DocumentDataError(
            f"Document at index {index} is missing required field(s): "
            f"{', '.join(missing)}."
        )

    for field in REQUIRED_DOCUMENT_FIELDS:
        value = raw_document[field]
        if not isinstance(value, str):
            raise DocumentDataError(
                f"Document at index {index} field {field!r} must be a string, "
                f"got {type(value).__name__}."
            )
        if not value.strip():
            raise DocumentDataError(
                f"Document at index {index} field {field!r} must be non-empty."
            )

    doc_id = raw_document["doc_id"]
    _validate_version_date(raw_document["version_date"], index, doc_id)
    # Return only the contract fields; accidental extra keys are not exposed.
    return {field: raw_document[field] for field in REQUIRED_DOCUMENT_FIELDS}


def load_documents(
    path: str | Path | None = None,
) -> list[dict[str, str]]:
    """Load and validate the synthetic document JSON file.

    ``path`` is primarily for tests; production calls use ``DEFAULT_DATA_PATH``.
    Raises ``DocumentDataError`` for missing files, invalid JSON, bad structure,
    duplicate IDs, or invalid dates.
    """
    data_path = Path(path) if path is not None else DEFAULT_DATA_PATH

    try:
        raw_text = data_path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DocumentDataError(
            f"Document data file not found: {data_path}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise DocumentDataError(
            f"Could not read document data file {data_path}: {exc}"
        ) from exc

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise DocumentDataError(
            f"Invalid JSON in document data file {data_path}: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})."
        ) from exc

    if not isinstance(payload, list):
        raise DocumentDataError(
            f"Document data file {data_path} must contain a JSON array."
        )

    documents: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for index, raw_document in enumerate(payload):
        document = _validate_document(raw_document, index)
        doc_id = document["doc_id"]
        if doc_id in seen_ids:
            raise DocumentDataError(
                f"Duplicate doc_id {doc_id!r} at index {index} in {data_path}."
            )
        seen_ids.add(doc_id)
        documents.append(document)

    return documents


def get_document(
    doc_id: str,
    *,
    path: str | Path | None = None,
) -> dict[str, str]:
    """Return the complete raw document record for ``doc_id``.

    Raises ``InvalidArgumentError`` for an invalid ID and
    ``DocumentNotFoundError`` when no document matches.
    """
    target = _validate_doc_id(doc_id)
    for document in load_documents(path):
        if document["doc_id"] == target:
            return dict(document)
    raise DocumentNotFoundError(f"Document not found: {target!r}.")


def expand_query(query: str) -> list[str]:
    """Return expanded synonym terms for an already-validated query.

    The original query string is not modified; this list is only used as
    retrieval vocabulary.
    """
    normalized_query = normalize_text(_validate_query(query))
    expanded: list[str] = []
    for terms in SYNONYM_GROUPS.values():
        if any(term in normalized_query for term in terms):
            for term in terms:
                if term not in expanded:
                    expanded.append(term)
    return expanded


def _active_concepts(
    normalized_query: str,
) -> dict[str, tuple[tuple[str, ...], frozenset[str]]]:
    active: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {}
    for name, terms in SYNONYM_GROUPS.items():
        original_terms = frozenset(
            term for term in terms if term in normalized_query
        )
        if original_terms:
            active[name] = (terms, original_terms)
    return active


def _term_specificity(term: str) -> float:
    """Longer phrases are slightly stronger than short generic terms."""
    return 1.0 + 0.25 * max(0, len(term) - 2)


def _score_document(
    document: dict[str, str],
    normalized_query: str,
    active_concepts: dict[str, tuple[tuple[str, ...], frozenset[str]]],
    required_groups: int,
) -> tuple[float, set[str]] | None:
    title = normalize_text(document["title"])
    content = normalize_text(document["content"])
    score = 0.0
    matched_groups = 0
    content_terms: set[str] = set()

    for terms, original_terms in active_concepts.values():
        group_matched = False
        for term in terms:
            in_title = term in title
            in_content = term in content
            if not (in_title or in_content):
                continue

            group_matched = True
            if in_content:
                content_terms.add(term)

            multiplier = (
                ORIGINAL_TERM_MULTIPLIER
                if term in original_terms
                else EXPANDED_TERM_MULTIPLIER
            )
            weight = multiplier * _term_specificity(term)
            if in_title:
                score += TITLE_WEIGHT * weight
            if in_content:
                score += CONTENT_WEIGHT * weight

        if group_matched:
            matched_groups += 1

    if matched_groups < required_groups or score <= 0:
        return None

    if len(normalized_query) >= 2:
        if normalized_query in title:
            score += PHRASE_TITLE_BONUS
        if normalized_query in content:
            score += PHRASE_CONTENT_BONUS

    return score, content_terms


def _fallback_terms(normalized_query: str) -> list[str]:
    """Extract 2/3-character CJK n-grams and Latin tokens for general queries."""
    terms: set[str] = set()
    for token in _LATIN_TOKEN_PATTERN.findall(normalized_query):
        if len(token) >= 2 and token not in _FALLBACK_STOP_TERMS:
            terms.add(token)

    for run in _CJK_RUN_PATTERN.findall(normalized_query):
        for size in (2, 3):
            for start in range(len(run) - size + 1):
                term = run[start : start + size]
                if term not in _FALLBACK_STOP_TERMS:
                    terms.add(term)

    return sorted(terms)


def _score_fallback(
    document: dict[str, str],
    terms: list[str],
) -> tuple[float, set[str]]:
    title = normalize_text(document["title"])
    content = normalize_text(document["content"])
    score = 0.0
    content_terms: set[str] = set()

    for term in terms:
        weight = _term_specificity(term)
        if term in title:
            score += TITLE_WEIGHT * weight
        if term in content:
            score += CONTENT_WEIGHT * weight
            content_terms.add(term)

    return score, content_terms


def _make_snippet(content: str, matched_terms: set[str]) -> str:
    """Return a verbatim substring of ``content``; never rewrite the source."""
    if len(content) <= SNIPPET_MAX_CHARS:
        return content

    folded_content = content.casefold()
    positions: list[int] = []
    for term in matched_terms:
        position = folded_content.find(term)
        if position >= 0:
            positions.append(position)

    if not positions:
        return content[:SNIPPET_MAX_CHARS]

    center = min(positions)
    start = max(0, center - SNIPPET_MAX_CHARS // 3)
    end = min(len(content), start + SNIPPET_MAX_CHARS)
    if end - start < SNIPPET_MAX_CHARS:
        start = max(0, end - SNIPPET_MAX_CHARS)
    return content[start:end]


def search_documents(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    *,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Search the local knowledge base and return ranked candidate documents.

    An empty list means the query matched nothing; it is not an execution
    error. Invalid arguments raise ``InvalidArgumentError`` and data problems
    raise ``DocumentDataError``.
    """
    clean_query = _validate_query(query)
    clean_top_k = _validate_top_k(top_k)
    documents = load_documents(path)

    normalized_query = normalize_text(clean_query)
    active_concepts = _active_concepts(normalized_query)
    scored: list[tuple[float, str, dict[str, str], set[str]]] = []

    if active_concepts:
        required_groups = math.ceil(len(active_concepts) * 2 / 3)
        for document in documents:
            scored_document = _score_document(
                document,
                normalized_query,
                active_concepts,
                required_groups,
            )
            if scored_document is None:
                continue
            score, matched_terms = scored_document
            scored.append(
                (score, document["doc_id"], document, matched_terms)
            )
    else:
        fallback_terms = _fallback_terms(normalized_query)
        if fallback_terms:
            for document in documents:
                score, matched_terms = _score_fallback(
                    document, fallback_terms
                )
                if score > 0:
                    scored.append(
                        (score, document["doc_id"], document, matched_terms)
                    )

    # Descending score; doc_id is a neutral, deterministic tie-breaker. In
    # particular, version_date is not used, so older documents are not dropped.
    scored.sort(key=lambda item: (-item[0], item[1]))

    results: list[dict[str, Any]] = []
    for score, doc_id, document, matched_terms in scored[:clean_top_k]:
        results.append(
            {
                "doc_id": doc_id,
                "title": document["title"],
                "version_date": document["version_date"],
                "snippet": _make_snippet(
                    document["content"], matched_terms
                ),
                "score": round(score, 4),
            }
        )
    return results
