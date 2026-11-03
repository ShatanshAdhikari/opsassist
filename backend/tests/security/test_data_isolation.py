"""Cross-tenant and cross-user leakage. Each test proves content never reaches model or client.

Canary strings are planted in restricted documents. A test fails if a canary appears anywhere in
a response or in the prompt sent to the model.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from opsassist.db import global_scope, tenant_scope
from opsassist.knowledge.retrieval import retrieve
from opsassist.models import Chunk, Conversation, KnowledgeSource, Role
from tests.conftest import FakeLLM, Harness

CANARY = "CANARYBLUEFALCON"
RESTRICTED_DOC = f"# Acquisition\n\nThe merger codename is {CANARY} and closes in March."
QUERY = f"merger codename {CANARY}"


async def _search(client, workspace_id, query: str = QUERY) -> str:
    response = await client.post(f"/api/v1/workspaces/{workspace_id}/search", json={"query": query})
    assert response.status_code == 200
    return response.text


async def _restricted_setup(harness: Harness):
    """Owner holds a restricted source. `outsider` is a workspace member with no grant."""
    owner = await harness.tenant()
    owner_client = await harness.login(owner.email)
    source = await harness.create_source(
        owner_client, owner.workspace_id, name="Board", visibility="restricted"
    )
    await harness.upload(owner_client, owner.workspace_id, source["id"], "deal.md", RESTRICTED_DOC)
    outsider = await harness.member_of(owner.workspace_id, Role.MEMBER)
    outsider_client = await harness.login(outsider.email)
    return owner, owner_client, source, outsider, outsider_client


# --- Between workspaces -----------------------------------------------------------------


async def test_other_workspace_cannot_see_content_through_any_endpoint(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    first = await harness.tenant()
    first_client = await harness.login(first.email)
    source = await harness.create_source(first_client, first.workspace_id)
    uploaded = await harness.upload(
        first_client, first.workspace_id, source["id"], "a.md", RESTRICTED_DOC
    )
    chunk_id = (json.loads(await _search(first_client, first.workspace_id)))["hits"][0]["chunk_id"]

    second = await harness.tenant()
    client = await harness.login(second.email)
    theirs = f"/api/v1/workspaces/{first.workspace_id}"
    mine = f"/api/v1/workspaces/{second.workspace_id}"

    # Direct access to the other workspace looks like it does not exist.
    for path in ("/sources", "/members", "/audit", "/conversations", f"/chunks/{chunk_id}"):
        assert (await client.get(theirs + path)).status_code == 404, path
    assert (await client.post(f"{theirs}/search", json={"query": QUERY})).status_code == 404

    # Foreign IDs used through the caller's own workspace are also not found.
    assert (await client.get(f"{mine}/chunks/{chunk_id}")).status_code == 404
    assert (await client.get(f"{mine}/sources/{source['id']}/documents")).status_code == 404
    delete_foreign = await client.delete(f"{mine}/documents/{uploaded['document']['id']}")
    assert delete_foreign.status_code == 404
    upload_foreign = await client.post(
        f"{mine}/sources/{source['id']}/documents", files={"file": ("x.md", b"# x")}
    )
    assert upload_foreign.status_code == 404

    # Search and ask from the caller's workspace never surface it.
    assert CANARY not in await _search(client, second.workspace_id)
    answer = await harness.ask(client, second.workspace_id, QUERY)
    assert answer["grounding"]["status"] == "no_evidence"
    assert CANARY not in json.dumps(answer)
    assert llm_harness.prompts == []


async def test_same_named_slug_workspaces_are_separate_tenants(harness: Harness) -> None:
    first = await harness.tenant()
    second = await harness.tenant()
    first_client = await harness.login(first.email)
    second_client = await harness.login(second.email)
    await harness.create_source(first_client, first.workspace_id, name="Shared Name")
    await harness.create_source(second_client, second.workspace_id, name="Shared Name")


# --- Database layer: holds even if application filters are wrong ------------------------


async def test_rls_blocks_retrieval_with_forged_workspace_parameter(harness: Harness) -> None:
    """Simulate an application bug: the query asks for tenant A inside tenant B's context."""
    first = await harness.tenant()
    client = await harness.login(first.email)
    source = await harness.create_source(client, first.workspace_id)
    await harness.upload(client, first.workspace_id, source["id"], "a.md", RESTRICTED_DOC)
    second = await harness.tenant()

    async with tenant_scope(harness.container.session_factory, second.workspace_id) as session:
        result = await retrieve(
            session,
            settings=harness.container.settings,
            workspace_id=first.workspace_id,
            user_id=first.user_id,
            query=QUERY,
            query_embedding=await harness.container.embedder.embed_query(QUERY),
            embedding_model=harness.container.embedder.model,
        )
    assert result.evidence == []


async def test_rls_unfiltered_select_sees_only_current_tenant(harness: Harness) -> None:
    first = await harness.tenant()
    client = await harness.login(first.email)
    source = await harness.create_source(client, first.workspace_id)
    await harness.upload(client, first.workspace_id, source["id"], "a.md", RESTRICTED_DOC)
    second = await harness.tenant()
    factory = harness.container.session_factory

    async with tenant_scope(factory, second.workspace_id) as session:
        assert await session.scalar(select(func.count()).select_from(Chunk)) == 0
        assert await session.scalar(select(func.count()).select_from(KnowledgeSource)) == 0
    async with tenant_scope(factory, first.workspace_id) as session:
        assert await session.scalar(select(func.count()).select_from(Chunk)) == 1


async def test_rls_fails_closed_without_context(harness: Harness) -> None:
    first = await harness.tenant()
    client = await harness.login(first.email)
    source = await harness.create_source(client, first.workspace_id)
    await harness.upload(client, first.workspace_id, source["id"], "a.md", RESTRICTED_DOC)
    async with global_scope(harness.container.session_factory) as session:
        for table in ("chunks", "documents", "knowledge_sources", "messages"):
            count = await session.scalar(text(f"SELECT count(*) FROM {table}"))
            assert count == 0, table


async def test_rls_blocks_cross_tenant_writes(harness: Harness) -> None:
    first = await harness.tenant()
    second = await harness.tenant()
    with pytest.raises(DBAPIError, match="row-level security"):
        async with tenant_scope(harness.container.session_factory, second.workspace_id) as session:
            session.add(
                KnowledgeSource(
                    workspace_id=first.workspace_id,
                    name="planted",
                    description="",
                    kind="upload",
                    visibility="workspace",
                )
            )


async def test_rls_context_does_not_leak_between_pooled_transactions(harness: Harness) -> None:
    first = await harness.tenant()
    factory = harness.container.session_factory
    async with tenant_scope(factory, first.workspace_id):
        pass
    for _ in range(harness.container.settings.db_pool_size + 2):
        async with global_scope(factory) as session:
            setting = await session.scalar(text("SELECT current_setting('app.workspace_id', true)"))
            assert not setting


async def test_app_role_is_not_privileged(harness: Harness) -> None:
    async with global_scope(harness.container.session_factory) as session:
        row = (
            await session.execute(
                text(
                    "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            )
        ).one()
    assert tuple(row) == (False, False, False)


# --- Between users in one workspace -----------------------------------------------------


async def test_restricted_source_hidden_from_member_everywhere(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, owner_client, source, _outsider, client = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    chunk_id = json.loads(await _search(owner_client, owner.workspace_id))["hits"][0]["chunk_id"]

    assert CANARY in await _search(owner_client, owner.workspace_id), "grant holder sees it"
    assert CANARY not in await _search(client, owner.workspace_id)
    assert (await client.get(f"{base}/chunks/{chunk_id}")).status_code == 404
    assert (await client.get(f"{base}/sources/{source['id']}/documents")).status_code == 404
    sources = await client.get(f"{base}/sources")
    assert source["id"] not in sources.text and "Board" not in sources.text

    answer = await harness.ask(client, owner.workspace_id, QUERY)
    assert CANARY not in json.dumps(answer)
    assert all(CANARY not in prompt for prompt in llm_harness.prompts)


async def test_restricted_source_excluded_while_public_source_still_answers(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, owner_client, _, _, client = await _restricted_setup(harness)
    public = await harness.create_source(owner_client, owner.workspace_id, name="Public")
    await harness.upload(
        owner_client,
        owner.workspace_id,
        public["id"],
        "faq.md",
        "# FAQ\n\nThe merger codename rumour is not confirmed by anyone.",
    )
    llm_harness.response = "Nothing is confirmed [S1]."
    answer = await harness.ask(client, owner.workspace_id, "merger codename")
    assert [c["source_name"] for c in answer["citations"]] == ["Public"]
    assert CANARY not in llm_harness.prompts[0]


async def test_admin_has_no_implicit_read_of_restricted_content(harness: Harness) -> None:
    owner, _owner_client, source, _, _ = await _restricted_setup(harness)
    admin = await harness.member_of(owner.workspace_id, Role.ADMIN)
    client = await harness.login(admin.email)
    base = f"/api/v1/workspaces/{owner.workspace_id}"

    # Admins see metadata in order to manage, flagged as unreadable.
    listed = (await client.get(f"{base}/sources")).json()
    assert [s["can_read"] for s in listed if s["id"] == source["id"]] == [False]
    assert CANARY not in await _search(client, owner.workspace_id)
    assert (await client.get(f"{base}/sources/{source['id']}/documents")).status_code == 404

    # Self-granting works and is audited.
    grant = await client.post(
        f"{base}/sources/{source['id']}/grants", json={"user_id": str(admin.user_id)}
    )
    assert grant.status_code == 201
    assert CANARY in await _search(client, owner.workspace_id)
    events = (await client.get(f"{base}/audit")).json()
    assert any(
        e["action"] == "source.grant_created" and e["actor_user_id"] == str(admin.user_id)
        for e in events
    )


async def test_user_grant_then_revocation_is_immediate(harness: Harness) -> None:
    owner, owner_client, source, outsider, client = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    grant = (
        await owner_client.post(
            f"{base}/sources/{source['id']}/grants", json={"user_id": str(outsider.user_id)}
        )
    ).json()
    assert CANARY in await _search(client, owner.workspace_id)

    response = await owner_client.delete(f"{base}/sources/{source['id']}/grants/{grant['id']}")
    assert response.status_code == 204
    assert CANARY not in await _search(client, owner.workspace_id)


async def test_group_grant_and_group_removal(harness: Harness) -> None:
    owner, owner_client, source, outsider, client = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    group = (await owner_client.post(f"{base}/groups", json={"name": "execs"})).json()
    await owner_client.post(f"{base}/sources/{source['id']}/grants", json={"group_id": group["id"]})
    assert CANARY not in await _search(client, owner.workspace_id), "not yet in the group"

    added = await owner_client.post(
        f"{base}/groups/{group['id']}/members", json={"user_id": str(outsider.user_id)}
    )
    assert added.status_code == 204
    assert CANARY in await _search(client, owner.workspace_id)

    removed = await owner_client.delete(f"{base}/groups/{group['id']}/members/{outsider.user_id}")
    assert removed.status_code == 204
    assert CANARY not in await _search(client, owner.workspace_id)


async def test_removed_member_loses_all_access(harness: Harness) -> None:
    owner, owner_client, source, outsider, client = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    await owner_client.post(
        f"{base}/sources/{source['id']}/grants", json={"user_id": str(outsider.user_id)}
    )
    await owner_client.delete(f"{base}/members/{outsider.user_id}")
    assert (await client.post(f"{base}/search", json={"query": QUERY})).status_code == 404


async def test_stored_answer_is_withheld_after_revocation(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner, owner_client, source, outsider, client = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    grant = (
        await owner_client.post(
            f"{base}/sources/{source['id']}/grants", json={"user_id": str(outsider.user_id)}
        )
    ).json()
    llm_harness.response = f"The codename is {CANARY} [S1]."
    conversation = await harness.conversation(client, owner.workspace_id)
    answer = await harness.ask(client, owner.workspace_id, QUERY, conversation)
    assert CANARY in answer["content"]

    await owner_client.delete(f"{base}/sources/{source['id']}/grants/{grant['id']}")
    messages = await client.get(f"{base}/conversations/{conversation}/messages")
    # The question was typed by this user, so strip it before looking for leaked content.
    body = messages.json()
    stored = body[1]
    assert CANARY not in json.dumps(stored)
    assert stored["grounding"]["status"] == "withheld"
    assert stored["citations"][0]["access_revoked"] is True
    assert stored["citations"][0]["excerpt"] is None


async def test_stored_answer_is_withheld_after_document_deletion(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    source = await harness.create_source(client, owner.workspace_id)
    uploaded = await harness.upload(
        client, owner.workspace_id, source["id"], "a.md", RESTRICTED_DOC
    )
    llm_harness.response = f"The codename is {CANARY} [S1]."
    conversation = await harness.conversation(client, owner.workspace_id)
    await harness.ask(client, owner.workspace_id, QUERY, conversation)

    base = f"/api/v1/workspaces/{owner.workspace_id}"
    await client.delete(f"{base}/documents/{uploaded['document']['id']}")
    stored = (await client.get(f"{base}/conversations/{conversation}/messages")).json()[1]
    assert CANARY not in json.dumps(stored)
    assert stored["grounding"]["status"] == "withheld"


async def test_conversations_are_private_between_users_even_for_admins(harness: Harness) -> None:
    owner = await harness.tenant()
    member = await harness.member_of(owner.workspace_id, Role.MEMBER)
    member_client = await harness.login(member.email)
    owner_client = await harness.login(owner.email)
    conversation = await harness.conversation(member_client, owner.workspace_id)
    base = f"/api/v1/workspaces/{owner.workspace_id}/conversations"

    assert (await owner_client.get(f"{base}/{conversation}/messages")).status_code == 404
    posted = await owner_client.post(f"{base}/{conversation}/messages", json={"question": "hi"})
    assert posted.status_code == 404
    assert conversation not in (await owner_client.get(base)).text


async def test_grant_rejects_user_or_group_from_other_workspace(harness: Harness) -> None:
    first = await harness.tenant()
    second = await harness.tenant()
    client = await harness.login(first.email)
    other_client = await harness.login(second.email)
    base = f"/api/v1/workspaces/{first.workspace_id}"
    source = await harness.create_source(client, first.workspace_id, visibility="restricted")
    foreign_group = (
        await other_client.post(
            f"/api/v1/workspaces/{second.workspace_id}/groups", json={"name": "g"}
        )
    ).json()
    url = f"{base}/sources/{source['id']}/grants"
    assert (await client.post(url, json={"user_id": str(second.user_id)})).status_code == 404
    assert (await client.post(url, json={"group_id": foreign_group["id"]})).status_code == 404
    both = await client.post(
        url, json={"user_id": str(first.user_id), "group_id": str(uuid.uuid4())}
    )
    assert both.status_code == 422


async def test_grant_delete_requires_matching_source(harness: Harness) -> None:
    owner, owner_client, source, outsider, _ = await _restricted_setup(harness)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    grant = (
        await owner_client.post(
            f"{base}/sources/{source['id']}/grants", json={"user_id": str(outsider.user_id)}
        )
    ).json()
    other = await harness.create_source(owner_client, owner.workspace_id, name="Other")
    response = await owner_client.delete(f"{base}/sources/{other['id']}/grants/{grant['id']}")
    assert response.status_code == 404


async def test_conversation_rows_are_tenant_scoped(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    await harness.conversation(client, owner.workspace_id)
    other = await harness.tenant()
    async with tenant_scope(harness.container.session_factory, other.workspace_id) as session:
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 0
