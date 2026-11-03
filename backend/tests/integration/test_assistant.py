"""Cited answers, honest degradation, retrieval behaviour."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from opsassist.db import tenant_scope
from opsassist.llm.base import ProviderUnavailable
from opsassist.models import Document, UsageRecord, utcnow
from tests.conftest import FailingEmbedder, FakeLLM, Harness

POLICY = """# Refund Policy

## Limits

Refunds above 500 dollars require approval from a finance manager.

## Timing

Refunds are processed within 10 business days.
"""


async def _ready(harness: Harness):
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id, name="Policies")
    await harness.upload(client, owner.workspace_id, source["id"], "refunds.md", POLICY)
    return owner, client, source


async def test_extractive_answer_cites_section_lines_and_makes_no_generation(
    harness: Harness,
) -> None:
    owner, client, _ = await _ready(harness)
    answer = await harness.ask(client, owner.workspace_id, "refunds approval finance manager")
    assert answer["grounding"]["status"] == "extractive"
    first = answer["citations"][0]
    assert first["marker"] == "S1"
    assert first["document_title"] == "refunds"
    assert first["source_name"] == "Policies"
    assert first["section_path"] == ["Refund Policy", "Limits"]
    assert POLICY.split("\n")[first["line_start"] - 1].startswith("Refunds above")
    assert "finance manager" in first["excerpt"]
    assert "[S1]" in answer["content"]


async def test_grounded_answer_keeps_citations_and_records_usage(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.response = "Refunds above 500 dollars need finance approval [S1]."
    answer = await harness.ask(client, owner.workspace_id, "Who approves large refunds?")
    assert answer["grounding"]["status"] == "grounded"
    assert answer["grounding"]["provider"] == "fake"
    assert answer["content"] == llm_harness.response
    used = [c["marker"] for c in answer["citations"] if c["used"]]
    assert used == ["S1"]
    prompt = llm_harness.prompts[0]
    assert "<excerpt" in prompt and "Who approves large refunds?" in prompt
    async with tenant_scope(harness.container.session_factory, owner.workspace_id) as session:
        usage = await session.scalar(select(UsageRecord))
    assert (usage.input_tokens, usage.output_tokens, usage.provider) == (10, 5, "fake")


async def test_no_evidence_never_calls_model(harness: Harness, llm_harness: FakeLLM) -> None:
    owner, client, _ = await _ready(harness)
    answer = await harness.ask(client, owner.workspace_id, "quantum spaceship teleportation")
    assert answer["grounding"]["status"] == "no_evidence"
    assert "No reliable source" in answer["content"]
    assert answer["citations"] == []
    assert llm_harness.prompts == []


async def test_no_evidence_mentions_documents_still_indexing(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    await harness.upload(
        client, owner.workspace_id, source["id"], "a.md", "# T\n\nZebra.", ingest=False
    )
    answer = await harness.ask(client, owner.workspace_id, "zebra")
    assert answer["grounding"]["status"] == "no_evidence"
    assert any("still being indexed" in note for note in answer["grounding"]["notes"])


async def test_indexing_note_ignores_sources_the_user_cannot_read(harness: Harness) -> None:
    owner = await harness.tenant()
    owner_client = await harness.login(owner.email)
    source = await harness.create_source(owner_client, owner.workspace_id, visibility="restricted")
    await harness.upload(
        owner_client, owner.workspace_id, source["id"], "a.md", "# T\n\nZebra.", ingest=False
    )
    member = await harness.member_of(owner.workspace_id)
    member_client = await harness.login(member.email)
    answer = await harness.ask(member_client, owner.workspace_id, "zebra")
    assert answer["grounding"]["notes"] == []


async def test_empty_workspace_has_no_evidence(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    answer = await harness.ask(client, owner.workspace_id, "anything at all about refunds")
    assert answer["grounding"]["status"] == "no_evidence"


async def test_invented_citation_is_stripped_and_flagged(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.response = "Refunds need approval [S1]. Also a claim from nowhere here [S99]."
    answer = await harness.ask(client, owner.workspace_id, "refunds approval")
    assert "[S99]" not in answer["content"]
    assert answer["grounding"]["removed_citations"] == ["S99"]
    assert any("do not exist" in note for note in answer["grounding"]["notes"])


async def test_uncited_generation_is_discarded(harness: Harness, llm_harness: FakeLLM) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.response = "Refunds are approved automatically by the CEO on Tuesdays."
    answer = await harness.ask(client, owner.workspace_id, "refunds approval")
    assert answer["grounding"]["status"] == "unsupported"
    assert "CEO" not in answer["content"]
    assert "finance manager" in answer["content"]


async def test_partly_cited_answer_is_labelled(harness: Harness, llm_harness: FakeLLM) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.response = (
        "Refunds above 500 dollars need finance approval [S1].\n"
        "The CEO also reviews every refund personally each week."
    )
    answer = await harness.ask(client, owner.workspace_id, "refunds approval")
    assert answer["grounding"]["status"] == "partially_grounded"
    assert answer["grounding"]["uncited_sentences"] == 1


async def test_model_saying_not_found_is_reported_without_citations(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.response = "I could not find this in the available sources."
    answer = await harness.ask(client, owner.workspace_id, "refunds approval timeline audit")
    assert answer["grounding"]["status"] == "no_evidence"
    assert answer["citations"] == []


async def test_model_outage_returns_excerpts_labelled_not_generated(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, client, _ = await _ready(harness)
    llm_harness.error = ProviderUnavailable("down")
    answer = await harness.ask(client, owner.workspace_id, "refunds approval")
    assert answer["grounding"]["status"] == "model_unavailable"
    assert "no answer was generated" in answer["content"]
    assert answer["citations"]


async def test_embedding_outage_falls_back_to_lexical_and_says_so(harness: Harness) -> None:
    owner, client, _ = await _ready(harness)
    harness.container.embedder = FailingEmbedder()
    answer = await harness.ask(client, owner.workspace_id, "refunds finance approval")
    assert answer["grounding"]["dense_available"] is False
    assert answer["citations"]
    assert any("keyword search" in note for note in answer["grounding"]["notes"])


async def test_exact_identifier_is_found_lexically(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    for name, body in [
        ("one.md", "# A\n\nThe outage INC-1042 was caused by a full disk."),
        ("two.md", "# B\n\nThe outage INC-2077 was caused by bad DNS."),
    ]:
        await harness.upload(client, owner.workspace_id, source["id"], name, body)
    hits = (
        await client.post(
            f"/api/v1/workspaces/{owner.workspace_id}/search", json={"query": "INC-2077"}
        )
    ).json()["hits"]
    assert len(hits) == 1
    assert "bad DNS" in hits[0]["excerpt"]
    assert "lexical" in hits[0]["matched_by"]


async def test_hostile_search_strings_do_not_error(harness: Harness) -> None:
    owner, client, _ = await _ready(harness)
    for query in ["'; DROP TABLE chunks; --", '"unbalanced', "a & | ! ( <->", "or OR or", "%%%"]:
        response = await client.post(
            f"/api/v1/workspaces/{owner.workspace_id}/search", json={"query": query}
        )
        assert response.status_code == 200, query


async def test_stale_document_is_flagged(harness: Harness) -> None:
    owner, client, _ = await _ready(harness)
    async with tenant_scope(harness.container.session_factory, owner.workspace_id) as session:
        await session.execute(update(Document).values(updated_at=utcnow() - timedelta(days=800)))
    answer = await harness.ask(client, owner.workspace_id, "refunds approval")
    assert answer["citations"][0]["stale"] is True
    assert answer["grounding"]["stale_sources"] >= 1
    assert any("outdated" in note for note in answer["grounding"]["notes"])


async def test_context_budget_limits_evidence(harness: Harness, llm_harness: FakeLLM) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    sections = "\n\n".join(f"## Part {i}\n\nZebra fact number {i} " + "x " * 200 for i in range(30))
    await harness.upload(client, owner.workspace_id, source["id"], "big.md", f"# Z\n\n{sections}")
    settings = harness.container.settings
    settings.retrieval_max_context_chars = 1000
    answer = await harness.ask(client, owner.workspace_id, "zebra fact")
    assert sum(len(c["excerpt"]) for c in answer["citations"]) <= 1000
    assert len(llm_harness.prompts[0]) < 2500


async def test_per_document_cap_keeps_other_sources_represented(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    many = "\n\n".join(f"## P{i}\n\nZebra fact {i}." for i in range(12))
    await harness.upload(client, owner.workspace_id, source["id"], "many.md", f"# M\n\n{many}")
    await harness.upload(
        client, owner.workspace_id, source["id"], "one.md", "# O\n\nZebra fact solo."
    )
    answer = await harness.ask(client, owner.workspace_id, "zebra fact")
    titles = [c["document_title"] for c in answer["citations"]]
    assert titles.count("many") == harness.container.settings.retrieval_max_per_document
    assert "one" in titles


async def test_query_is_audited_with_sources_but_no_content(harness: Harness) -> None:
    owner, client, source = await _ready(harness)
    await harness.ask(client, owner.workspace_id, "refunds approval secret-question-text")
    events = (await client.get(f"/api/v1/workspaces/{owner.workspace_id}/audit")).json()
    query = next(e for e in events if e["action"] == "knowledge.query")
    assert query["metadata"]["source_ids"] == [source["id"]]
    assert "secret-question-text" not in str(query)
    assert "finance manager" not in str(query)


async def test_conversation_history_is_persisted_in_order(harness: Harness) -> None:
    owner, client, _ = await _ready(harness)
    conversation = await harness.conversation(client, owner.workspace_id)
    await harness.ask(client, owner.workspace_id, "refunds approval", conversation)
    await harness.ask(client, owner.workspace_id, "refunds timing", conversation)
    messages = (
        await client.get(
            f"/api/v1/workspaces/{owner.workspace_id}/conversations/{conversation}/messages"
        )
    ).json()
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["content"] == "refunds approval"
    assert messages[1]["citations"]


async def test_question_rate_limit(harness: Harness) -> None:
    owner, client, _ = await _ready(harness)
    harness.container.question_limiter._limit = 1
    conversation = await harness.conversation(client, owner.workspace_id)
    url = f"/api/v1/workspaces/{owner.workspace_id}/conversations/{conversation}/messages"
    assert (await client.post(url, json={"question": "refunds"})).status_code == 200
    limited = await client.post(url, json={"question": "refunds"})
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
