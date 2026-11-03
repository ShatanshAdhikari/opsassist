"""Hybrid retrieval: dense and lexical candidates, rank fusion, relevance gate, context budget.

Authorization is part of the SQL for each candidate query, so unauthorized text is never loaded.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsassist.config import Settings
from opsassist.knowledge.access import VISIBLE_SOURCES_SQL
from opsassist.models import utcnow

_RRF_K = 60
_STEM_CHARS = 5

_COLUMNS = """
    c.id AS chunk_id, c.document_id, c.source_id, c.content, c.content_hash, c.section_path,
    c.page, c.line_start, c.line_end, c.lexical_text,
    d.title AS document_title, d.updated_at AS document_updated_at,
    s.name AS source_name, v.version_number, v.indexed_at
"""

# Joining on current_version_id means only the active version of a document can be found.
_FROM = f"""
    FROM chunks c
    JOIN documents d ON d.id = c.document_id AND d.current_version_id = c.version_id
    JOIN document_versions v ON v.id = c.version_id
    JOIN knowledge_sources s ON s.id = c.source_id
    WHERE c.workspace_id = :workspace_id
      AND c.source_id IN ({VISIBLE_SOURCES_SQL})
"""

_DENSE_SQL = f"""
    SELECT {_COLUMNS}, 1 - (c.embedding <=> CAST(:embedding AS text)::vector) AS similarity
    {_FROM}
      AND c.embedding_model = :embedding_model
    ORDER BY c.embedding <=> CAST(:embedding AS text)::vector
    LIMIT :limit
"""

_LEXICAL_SQL = f"""
    SELECT {_COLUMNS}, ts_rank_cd(c.tsv, q.query) AS rank
    FROM websearch_to_tsquery('english', :tsquery) AS q(query), LATERAL (
        SELECT c.* FROM chunks c WHERE c.tsv @@ q.query
    ) c
    JOIN documents d ON d.id = c.document_id AND d.current_version_id = c.version_id
    JOIN document_versions v ON v.id = c.version_id
    JOIN knowledge_sources s ON s.id = c.source_id
    WHERE c.workspace_id = :workspace_id
      AND c.source_id IN ({VISIBLE_SOURCES_SQL})
    ORDER BY rank DESC
    LIMIT :limit
"""

_TOKEN = re.compile(r"[\w][\w.\-]*\w|\w", re.UNICODE)
_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "before",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "may",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "should",
        "so",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "find",
        "tell",
        "show",
        "give",
        "please",
        "list",
        "explain",
        "describe",
    ]
)


@dataclass(frozen=True)
class Evidence:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    source_id: uuid.UUID
    document_title: str
    source_name: str
    version_number: int
    content: str
    section_path: tuple[str, ...]
    page: int | None
    line_start: int | None
    line_end: int | None
    indexed_at: datetime
    document_updated_at: datetime
    score: float
    similarity: float | None
    term_coverage: float
    matched_by: tuple[str, ...]
    stale: bool


@dataclass(frozen=True)
class RetrievalResult:
    evidence: list[Evidence] = field(default_factory=list)
    dense_available: bool = True


def query_terms(query: str) -> list[str]:
    """Lowercased meaningful query tokens, without stopwords and duplicates."""
    seen: dict[str, None] = {}
    for token in _TOKEN.findall(query.lower()):
        if token not in _STOPWORDS:
            seen.setdefault(token)
    return list(seen)


def build_tsquery(terms: list[str]) -> str:
    """Build an OR query for websearch_to_tsquery, which never raises on any input."""
    return " OR ".join(f'"{term}"' for term in terms)


def is_identifier(term: str) -> bool:
    """Tokens such as INC-1042, ERR_TIMEOUT or v2.3 must match exactly to count as relevant."""
    return any(char.isdigit() for char in term) or "-" in term or "_" in term


def _word_stem(term: str) -> str:
    """Crude prefix stem so approves matches approval. The gate only needs rough recall."""
    return term[:_STEM_CHARS]


def _term_coverage(terms: list[str], haystack: str) -> tuple[float, bool]:
    if not terms:
        return 0.0, False
    tokens = set(_TOKEN.findall(haystack.lower()))
    stems = {_word_stem(token) for token in tokens if not is_identifier(token)}
    matched_words = 0
    identifier_hit = False
    for term in terms:
        if is_identifier(term):
            if term in tokens:
                matched_words += 1
                identifier_hit = True
        elif _word_stem(term) in stems:
            matched_words += 1
    return matched_words / len(terms), identifier_hit


@dataclass
class _Candidate:
    row: Mapping[str, Any]
    score: float = 0.0
    similarity: float | None = None
    methods: list[str] = field(default_factory=list)


async def retrieve(
    session: AsyncSession,
    *,
    settings: Settings,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    query_embedding: list[float] | None,
    embedding_model: str,
    top_k: int | None = None,
) -> RetrievalResult:
    base_params = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "limit": settings.retrieval_candidates,
    }
    candidates: dict[uuid.UUID, _Candidate] = {}

    def add(row: Any, rank: int, method: str) -> None:
        candidate = candidates.setdefault(row["chunk_id"], _Candidate(row=row))
        candidate.score += 1.0 / (_RRF_K + rank)
        candidate.methods.append(method)
        if method == "dense":
            candidate.similarity = float(row["similarity"])

    if query_embedding is not None:
        # Filtered HNSW can return too few rows. Iterative scan keeps going until LIMIT is met.
        await session.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        await session.execute(text("SET LOCAL hnsw.ef_search = 100"))
        rows = await session.execute(
            text(_DENSE_SQL),
            {
                **base_params,
                "embedding": json.dumps(query_embedding),
                "embedding_model": embedding_model,
            },
        )
        for rank, row in enumerate(rows.mappings(), start=1):
            add(row, rank, "dense")

    terms = query_terms(query)
    if terms:
        rows = await session.execute(
            text(_LEXICAL_SQL), {**base_params, "tsquery": build_tsquery(terms)}
        )
        for rank, row in enumerate(rows.mappings(), start=1):
            add(row, rank, "lexical")

    evidence = _select(candidates, terms, settings, top_k or settings.retrieval_top_k)
    return RetrievalResult(evidence=evidence, dense_available=query_embedding is not None)


def _select(
    candidates: dict[uuid.UUID, _Candidate],
    terms: list[str],
    settings: Settings,
    top_k: int,
) -> list[Evidence]:
    """Apply the relevance gate, dedup, per-document cap and context budget, best first."""
    stale_before = utcnow() - timedelta(days=settings.stale_after_days)
    selected: list[Evidence] = []
    seen_hashes: set[str] = set()
    per_document: dict[uuid.UUID, int] = {}
    total_chars = 0
    for candidate in sorted(candidates.values(), key=lambda item: item.score, reverse=True):
        row = candidate.row
        coverage, identifier_hit = _term_coverage(terms, row["lexical_text"])
        relevant = (
            identifier_hit
            or coverage >= settings.retrieval_min_term_coverage
            or (
                candidate.similarity is not None
                and candidate.similarity >= settings.retrieval_min_dense_similarity
            )
        )
        if not relevant or row["content_hash"] in seen_hashes:
            continue
        document_id: uuid.UUID = row["document_id"]
        content: str = row["content"]
        if per_document.get(document_id, 0) >= settings.retrieval_max_per_document:
            continue
        if total_chars + len(content) > settings.retrieval_max_context_chars:
            continue
        seen_hashes.add(row["content_hash"])
        per_document[document_id] = per_document.get(document_id, 0) + 1
        total_chars += len(content)
        selected.append(
            Evidence(
                chunk_id=row["chunk_id"],
                document_id=document_id,
                source_id=row["source_id"],
                document_title=row["document_title"],
                source_name=row["source_name"],
                version_number=row["version_number"],
                content=content,
                section_path=tuple(row["section_path"]),
                page=row["page"],
                line_start=row["line_start"],
                line_end=row["line_end"],
                indexed_at=row["indexed_at"],
                document_updated_at=row["document_updated_at"],
                score=candidate.score,
                similarity=candidate.similarity,
                term_coverage=coverage,
                matched_by=tuple(candidate.methods),
                stale=row["document_updated_at"] < stale_before,
            )
        )
        if len(selected) >= top_k:
            break
    return selected
