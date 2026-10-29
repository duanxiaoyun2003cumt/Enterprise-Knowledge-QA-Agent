"""Business tools exposed to the knowledge agent.

These functions are the stable, JSON-serializable boundary for the future
Agent Loop. They do not call an LLM, choose the next action, resolve policy
conflicts, or generate final answers.

Error contract:
- invalid arguments -> ``InvalidArgumentError`` (a ``ValueError``)
- unknown document ID -> ``DocumentNotFoundError`` (a ``LookupError``)
- missing/invalid data file -> ``DocumentDataError``
- no matching documents -> an empty list, not an exception
"""

from __future__ import annotations

import logging
from typing import Any

from .retrieval import (
    DocumentDataError,
    DocumentNotFoundError,
    InvalidArgumentError,
    RetrievalError,
    get_document,
    search_documents as _search_documents,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DocumentDataError",
    "DocumentNotFoundError",
    "InvalidArgumentError",
    "RetrievalError",
    "read_document",
    "search_documents",
]


def search_documents(
    query: str,
    top_k: int = 3,
) -> list[dict[str, Any]]:
    """Return candidate documents for ``query``.

    Each result contains ``doc_id``, ``title``, ``version_date``, a verbatim
    ``snippet``, and a relevance ``score``. An empty list is a valid no-match
    result; data and argument errors are raised instead of being swallowed.

    Raises:
        InvalidArgumentError: ``query`` or ``top_k`` violates the contract.
        DocumentDataError: the local data file is missing or invalid.
    """
    results = _search_documents(query, top_k=top_k)
    logger.debug(
        "search_documents top_k=%d candidates=%d", top_k, len(results)
    )
    return results


def read_document(doc_id: str) -> dict[str, str]:
    """Return the full raw document for ``doc_id`` with unchanged content.

    Raises:
        InvalidArgumentError: ``doc_id`` is not a non-empty string.
        DocumentNotFoundError: no document has this ID.
        DocumentDataError: the local data file is missing or invalid.
    """
    document = get_document(doc_id)
    logger.debug(
        "read_document doc_id=%s content_chars=%d",
        document["doc_id"],
        len(document["content"]),
    )
    return document
