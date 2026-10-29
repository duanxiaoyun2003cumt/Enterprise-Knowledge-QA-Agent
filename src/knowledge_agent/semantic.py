"""Conservative citation semantic-support guard.

This is not a general natural-language entailment engine. It checks whether a
citation's claim topics and numeric values are compatible with the answer's
key claims. It is deliberately conservative: an uncertain result is not
accepted as support.

Keeping this separate from authenticity validation makes the distinction clear:
- authenticity: the quote exists and was read from the cited document;
- semantic support: the quote's claim is relevant to the answer's claim.

The check is deterministic and adds no model calls, so it cannot consume the
agent's ``MAX_STEPS`` budget. It catches clear cross-topic misuse (for example,
using a reimbursement-deadline quote to support a travel-materials claim) but
cannot prove full entailment. Callers must treat ``uncertain`` as unsupported.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .retrieval import normalize_text
from .schemas import Citation

_DURATION_RE = re.compile(r"(\d+)\s*(个工作日|工作日|天|日)")
_CLAIM_SEPARATOR_RE = re.compile(r"[。；;！？!?\n，,]+")

# These are specific business topics. "报销"/"费用" are intentionally not
# specific topics: they are too generic and appear in almost every document.
_TOPIC_TERMS: dict[str, tuple[str, ...]] = {
    "deadline": (
        "期限",
        "时限",
        "最晚",
        "截止",
        "提交",
        "几天",
        "多少天",
        "多久",
        "日内",
        "到期",
    ),
    "materials": (
        "材料",
        "发票",
        "票据",
        "订单",
        "凭证",
        "入住",
        "提供",
    ),
    "travel": ("差旅", "住宿", "酒店", "出差", "行程"),
    "sso": ("单点登录", "sso", "saml", "企业版", "产品x"),
}


@dataclass(frozen=True)
class CitationSupport:
    """Deterministic semantic-support assessment for one citation."""

    doc_id: str
    status: str  # supported | unsupported | uncertain
    answer_topics: frozenset[str]
    quote_topics: frozenset[str]
    reason: str
    supported_claims: tuple[str, ...] = ()


@dataclass(frozen=True)
class ClaimSupport:
    """One answer claim that lacks a supporting authentic citation."""

    claim: str
    status: str  # unsupported | uncertain
    topics: frozenset[str]
    durations: frozenset[tuple[int, str]]
    reason: str


def _topics(text: str) -> frozenset[str]:
    normalized = normalize_text(text)
    topics = {
        topic
        for topic, terms in _TOPIC_TERMS.items()
        if any(term in normalized for term in terms)
    }
    if _DURATION_RE.search(normalized):
        topics.add("deadline")
    return frozenset(topics)


def _durations(text: str) -> frozenset[tuple[int, str]]:
    return frozenset(
        (
            int(number),
            "工作日" if "工作日" in unit else "日",
        )
        for number, unit in _DURATION_RE.findall(normalize_text(text))
    )


def split_claims(answer: str) -> list[str]:
    """Split an answer into short claims at sentence/clause boundaries."""
    normalized = normalize_text(answer)
    return [
        part.strip()
        for part in _CLAIM_SEPARATOR_RE.split(normalized)
        if part.strip()
    ]


def _is_authentic(
    citation: Citation, read_documents: Mapping[str, Mapping[str, str]]
) -> bool:
    document = read_documents.get(citation.doc_id)
    return document is not None and citation.quote in str(
        document.get("content", "")
    )


def _claim_is_supported(
    claim_topics: frozenset[str],
    claim_durations: frozenset[tuple[int, str]],
    quote_topics: frozenset[str],
    quote_durations: frozenset[tuple[int, str]],
) -> bool:
    if not (claim_topics & quote_topics):
        return False
    # A numeric deadline claim requires the number to appear in the quote.
    # Topic overlap alone would let a 30-day quote "support" a 45-day claim.
    if claim_durations and not claim_durations <= quote_durations:
        return False
    return True


def assess_citation_support(
    answer: str,
    citations: Sequence[Citation],
    read_documents: Mapping[str, Mapping[str, str]],
) -> list[CitationSupport]:
    """Assess each authentic citation against the answer's specific claims.

    Unread or tampered quotes are intentionally skipped: authenticity
    validation owns those errors and must remain a separate check.
    """
    answer_topics = _topics(answer)
    claims = [
        (claim, _topics(claim), _durations(claim))
        for claim in split_claims(answer)
    ]
    assessments: list[CitationSupport] = []

    for citation in citations:
        if not _is_authentic(citation, read_documents):
            continue

        quote_topics = _topics(citation.quote)
        quote_durations = _durations(citation.quote)
        supported_claims = tuple(
            claim
            for claim, claim_topics, claim_durations in claims
            if _claim_is_supported(
                claim_topics,
                claim_durations,
                quote_topics,
                quote_durations,
            )
        )

        if not answer_topics or not quote_topics:
            status = "uncertain"
            reason = "answer or quote has no recognizable claim topic"
        elif supported_claims:
            status = "supported"
            reason = "quote supports answer claim(s): " + " | ".join(
                supported_claims[:2]
            )
        else:
            status = "unsupported"
            reason = (
                f"answer topics {sorted(answer_topics)} and quote topics "
                f"{sorted(quote_topics)} have no matching factual claim"
            )

        assessments.append(
            CitationSupport(
                doc_id=citation.doc_id,
                status=status,
                answer_topics=answer_topics,
                quote_topics=quote_topics,
                reason=reason,
                supported_claims=supported_claims,
            )
        )

    return assessments


def find_unsupported_claims(
    answer: str,
    citations: Sequence[Citation],
    read_documents: Mapping[str, Mapping[str, str]],
) -> list[ClaimSupport]:
    """Find specific answer claims with no authentic supporting quote.

    Generic clauses (for example "请确认") carry no verifiable business fact
    and are skipped. A claim that mentions a numeric deadline only counts as
    supported when the quote contains the same normalized number and unit.
    """
    authentic = [
        citation
        for citation in citations
        if _is_authentic(citation, read_documents)
    ]
    if not authentic:
        return []

    quotes = [
        (_topics(citation.quote), _durations(citation.quote))
        for citation in authentic
    ]
    any_quote_topics = any(topics for topics, _ in quotes)
    unsupported: list[ClaimSupport] = []

    for claim in split_claims(answer):
        claim_topics = _topics(claim)
        if not claim_topics:
            continue
        claim_durations = _durations(claim)
        if any(
            _claim_is_supported(
                claim_topics,
                claim_durations,
                quote_topics,
                quote_durations,
            )
            for quote_topics, quote_durations in quotes
        ):
            continue

        status = "unsupported" if any_quote_topics else "uncertain"
        unsupported.append(
            ClaimSupport(
                claim=claim,
                status=status,
                topics=claim_topics,
                durations=claim_durations,
                reason=(
                    "no authentic citation has a compatible topic"
                    if status == "unsupported"
                    else "citations have no recognizable claim topic"
                ),
            )
        )

    return unsupported


__all__ = [
    "CitationSupport",
    "ClaimSupport",
    "assess_citation_support",
    "find_unsupported_claims",
    "split_claims",
]
