"""Repeatable RAG evaluation against a dedicated database. Never point it at real data.

Retrieval only, with deterministic hash embeddings (the default, and what CI runs):
    uv run python -m evals.run_rag_eval

Real local embeddings:
    uv run python -m evals.run_rag_eval --embedding ollama

End to end with a local model, including citation and injection checks:
    uv run python -m evals.run_rag_eval --embedding ollama --llm ollama

Metrics are reported as measured. Faithfulness uses a lexical-overlap heuristic: useful for spotting
regressions, not proof that a claim is supported.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from evals.corpus import CASES, CORPUS, RESTRICTED_MARKERS, Case
from opsassist.api.app import create_app
from opsassist.config import Settings
from opsassist.container import build_container
from opsassist.db import global_scope
from opsassist.identity.service import hash_password
from opsassist.jobs.worker import Worker
from opsassist.knowledge.ingestion import INGEST_JOB_KIND, IngestHandler
from opsassist.models import Membership, Organization, Role, User, Workspace

BACKEND_DIR = Path(__file__).resolve().parents[1]
OWNER_URL = os.environ.get(
    "TEST_MIGRATION_DATABASE_URL",
    "postgresql+asyncpg://opsassist_owner:dev_owner_password@localhost:5433/opsassist_test",
)
APP_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://opsassist_app:dev_app_password@localhost:5433/opsassist_test",
)
ORIGIN = "http://eval.local"
PASSWORD = "evaluation-password-1234"  # noqa: S105 - throwaway account in a test database
_MARKER = re.compile(r"\[S\d+\]")
_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class CaseResult:
    id: str
    category: str
    retrieved: list[str]
    retrieval_ms: float
    recall_hit: bool | None = None
    all_expected_hit: bool | None = None
    text_hit: bool | None = None
    false_positive: bool | None = None
    leak: bool = False
    answer_status: str | None = None
    answer: str | None = None
    answer_ms: float | None = None
    invented_citations: int = 0
    expected_source_cited: bool | None = None
    faithfulness: float | None = None
    injection_followed: bool | None = None
    says_not_found: bool | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    notes: list[str] = field(default_factory=list)


async def _reset_database() -> None:
    env = {**os.environ, "MIGRATION_DATABASE_URL": OWNER_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=BACKEND_DIR,
        env=env,
        check=True,
        capture_output=True,
    )
    engine = create_async_engine(OWNER_URL)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE organizations, users, sessions, audit_events, jobs, workspaces CASCADE")
        )
    await engine.dispose()


def _faithfulness(answer: str, excerpts: list[str]) -> float:
    """Share of answer content words that occur in the cited excerpts."""
    words = [w for w in _WORD.findall(_MARKER.sub("", answer).lower()) if len(w) > 3]
    if not words:
        return 1.0
    vocabulary = set(_WORD.findall(" ".join(excerpts).lower()))
    return sum(1 for word in words if word in vocabulary) / len(words)


async def _setup(container: Any, client: httpx.AsyncClient) -> tuple[uuid.UUID, str, str]:
    """Create one workspace, with a privileged author and a plain member who runs the cases."""
    password_hash = hash_password(PASSWORD)
    author_email, member_email = "author@eval.local", "member@eval.local"
    async with global_scope(container.session_factory) as session:
        organization = Organization(name="Eval", slug=f"eval-{uuid.uuid4().hex[:8]}")
        session.add(organization)
        await session.flush()
        workspace = Workspace(organization_id=organization.id, name="Eval", slug="eval")
        session.add(workspace)
        author = User(email=author_email, display_name="Author", password_hash=password_hash)
        member = User(email=member_email, display_name="Member", password_hash=password_hash)
        session.add_all([author, member])
        await session.flush()
        session.add_all(
            [
                Membership(workspace_id=workspace.id, user_id=author.id, role=Role.OWNER),
                Membership(workspace_id=workspace.id, user_id=member.id, role=Role.MEMBER),
            ]
        )
        workspace_id = workspace.id
    login = await client.post(
        "/api/v1/auth/login", json={"email": author_email, "password": PASSWORD}
    )
    login.raise_for_status()
    base = f"/api/v1/workspaces/{workspace_id}"
    for name, (visibility, documents) in CORPUS.items():
        response = await client.post(
            f"{base}/sources", json={"name": name, "visibility": visibility}
        )
        response.raise_for_status()
        source_id = response.json()["id"]
        for filename, content in documents.items():
            upload = await client.post(
                f"{base}/sources/{source_id}/documents",
                files={"file": (filename, content.encode())},
            )
            upload.raise_for_status()
    return workspace_id, member_email, author_email


async def _run_case(
    client: httpx.AsyncClient, base: str, case: Case, *, use_llm: bool
) -> CaseResult:
    started = time.perf_counter()
    response = await client.post(f"{base}/search", json={"query": case.question, "limit": 8})
    response.raise_for_status()
    retrieval_ms = (time.perf_counter() - started) * 1000
    hits = response.json()["hits"]
    titles = [hit["document_title"] for hit in hits]
    joined = "\n".join(hit["excerpt"] for hit in hits)
    result = CaseResult(case.id, case.category, titles, retrieval_ms)
    result.leak = any(marker in joined for marker in (*RESTRICTED_MARKERS, *case.forbidden_text))

    if case.expected:
        result.recall_hit = any(title in case.expected for title in titles)
    if case.expected_all:
        result.all_expected_hit = all(title in titles for title in case.expected_all)
    if case.expected_text:
        result.text_hit = all(snippet in joined for snippet in case.expected_text)
    if case.category == "missing":
        result.false_positive = bool(hits)

    if use_llm:
        await _run_answer(client, base, case, result)
    return result


async def _run_answer(client: httpx.AsyncClient, base: str, case: Case, result: CaseResult) -> None:
    conversation = await client.post(f"{base}/conversations", json={"title": case.id})
    conversation.raise_for_status()
    started = time.perf_counter()
    response = await client.post(
        f"{base}/conversations/{conversation.json()['id']}/messages",
        json={"question": case.question},
        timeout=None,
    )
    response.raise_for_status()
    result.answer_ms = (time.perf_counter() - started) * 1000
    answer = response.json()["answer"]
    content: str = answer["content"]
    grounding = answer["grounding"]
    citations = answer["citations"]
    result.answer = content
    result.answer_status = grounding["status"]
    result.says_not_found = grounding["status"] == "no_evidence"
    result.invented_citations = len(grounding["removed_citations"])
    if case.expected and grounding["status"] in {"grounded", "partially_grounded"}:
        result.expected_source_cited = any(
            c["used"] and c.get("document_title") in case.expected for c in citations
        )
    result.leak = result.leak or any(
        marker in json.dumps(answer) for marker in (*RESTRICTED_MARKERS, *case.forbidden_text)
    )
    if grounding["status"] in {"grounded", "partially_grounded"}:
        used = [c["excerpt"] for c in citations if c["used"] and c.get("excerpt")]
        result.faithfulness = _faithfulness(content, used)
    if case.category == "injection":
        lowered = content.lower()
        result.injection_followed = "cancelled" in lowered and "not" not in lowered


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    latencies = sorted(result.retrieval_ms for result in results)
    answered = [r.answer_ms for r in results if r.answer_ms is not None]
    relevant = [r for r in results if r.recall_hit is not None]
    faithfulness = [r.faithfulness for r in results if r.faithfulness is not None]
    return {
        "cases": len(results),
        "retrieval_recall_any_expected": _rate([bool(r.recall_hit) for r in relevant]),
        "retrieval_all_expected_conflict_sources": _rate(
            [bool(r.all_expected_hit) for r in results if r.all_expected_hit is not None]
        ),
        "expected_text_in_evidence": _rate(
            [bool(r.text_hit) for r in results if r.text_hit is not None]
        ),
        "missing_questions_with_false_hits": _rate(
            [bool(r.false_positive) for r in results if r.false_positive is not None]
        ),
        "mean_precision_at_retrieved": _precision(relevant),
        "permission_leaks": sum(1 for r in results if r.leak),
        "retrieval_p50_ms": round(statistics.median(latencies), 1),
        "retrieval_max_ms": round(latencies[-1], 1),
        "answer_status_counts": dict(
            Counter(r.answer_status for r in results if r.answer_status is not None)
        ),
        "model_invented_citations_removed": sum(r.invented_citations for r in results),
        "expected_source_cited": _rate(
            [bool(r.expected_source_cited) for r in results if r.expected_source_cited is not None]
        ),
        "faithfulness_heuristic_mean": round(statistics.mean(faithfulness), 3)
        if faithfulness
        else None,
        "injection_followed": sum(1 for r in results if r.injection_followed),
        "missing_questions_declined": _rate(
            [bool(r.says_not_found) for r in results if r.category == "missing" and r.answer]
        ),
        "answer_mean_ms": round(statistics.mean(answered), 1) if answered else None,
    }


def _precision(relevant: list[CaseResult]) -> float | None:
    scores = []
    for result in relevant:
        expected = next(c for c in CASES if c.id == result.id).expected
        if result.retrieved:
            scores.append(sum(t in expected for t in result.retrieved) / len(result.retrieved))
    return round(statistics.mean(scores), 3) if scores else None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--embedding", choices=["hash", "ollama"], default="hash")
    parser.add_argument("--llm", choices=["extractive", "ollama"], default="extractive")
    parser.add_argument("--llm-model", default="llama3")
    parser.add_argument("--min-similarity", type=float)
    parser.add_argument("--min-recall", type=float, help="Fail below this retrieval recall")
    parser.add_argument("--require-no-leaks", action="store_true")
    parser.add_argument("--output", type=Path, help="Write the full report as JSON")
    args = parser.parse_args()

    await _reset_database()
    with tempfile.TemporaryDirectory() as blob_dir:
        overrides: dict[str, Any] = {}
        if args.min_similarity is not None:
            overrides["retrieval_min_dense_similarity"] = args.min_similarity
        settings = Settings(
            _env_file=None,  # type: ignore[call-arg]
            environment="test",
            database_url=APP_URL,  # type: ignore[arg-type]
            cookie_secure=False,
            allowed_origins=[ORIGIN],
            blob_dir=Path(blob_dir),
            embedding_provider=args.embedding,
            llm_provider=args.llm,
            llm_model=args.llm_model,
            llm_max_output_tokens=512,
            questions_per_minute=10_000,
            log_level="ERROR",
            **overrides,
        )
        container = build_container(settings)
        app = create_app(container=container)
        transport = httpx.ASGITransport(app=app)
        async with (
            httpx.AsyncClient(
                transport=transport, base_url="http://eval", headers={"Origin": ORIGIN}
            ) as author_client,
            httpx.AsyncClient(
                transport=transport, base_url="http://eval", headers={"Origin": ORIGIN}
            ) as member_client,
        ):
            workspace_id, member_email, _ = await _setup(container, author_client)
            worker = Worker(
                settings,
                container.session_factory,
                {
                    INGEST_JOB_KIND: IngestHandler(
                        settings, container.session_factory, container.blobs, container.embedder
                    )
                },
            )
            while await worker.run_once():
                pass
            login = await member_client.post(
                "/api/v1/auth/login", json={"email": member_email, "password": PASSWORD}
            )
            login.raise_for_status()
            base = f"/api/v1/workspaces/{workspace_id}"
            results = []
            for case in CASES:
                results.append(
                    await _run_case(member_client, base, case, use_llm=args.llm != "extractive")
                )
                print(f"  {case.id}: {results[-1].retrieved[:3]}", file=sys.stderr)
        await container.close()

    summary = summarize(results)
    print(
        json.dumps(
            {"embedding": args.embedding, "llm": args.llm, "model": args.llm_model, **summary},
            indent=2,
        )
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(
                {"summary": summary, "cases": [asdict(result) for result in results]}, indent=2
            ),
            encoding="utf-8",
        )

    if args.require_no_leaks and summary["permission_leaks"]:
        print("FAILED: permission leaks detected", file=sys.stderr)
        return 1
    recall = summary["retrieval_recall_any_expected"]
    if args.min_recall is not None and (recall or 0) < args.min_recall:
        print(f"FAILED: recall {recall} is below {args.min_recall}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
