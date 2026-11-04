"""The assistant using tools, through the API, against a real MCP server."""

from __future__ import annotations

import json
from typing import Any

import pytest_asyncio
from sqlalchemy import select, update

from opsassist.db import global_scope, tenant_scope
from opsassist.llm.base import ProviderUnavailable
from opsassist.models import AuditEvent, Membership, Role, ToolInvocation, UsageRecord
from tests.conftest import FakeLLM, Harness
from tests.integration.test_tools import MockWorld, _approve, _discover, _ready, _register
from tests.mock_mcp_server import FAKE_TOKEN


@pytest_asyncio.fixture
async def world(harness: Harness) -> MockWorld:
    return MockWorld(harness)


def _plan(*calls: tuple[str, dict[str, Any]], search_query: str | None = None) -> str:
    body: dict[str, Any] = {
        "calls": [{"tool": f"mock.{name}", "arguments": args} for name, args in calls]
    }
    if search_query:
        body["search_query"] = search_query
    return json.dumps(body)


async def _invocations(harness: Harness, tenant: Any) -> list[ToolInvocation]:
    async with tenant_scope(harness.container.session_factory, tenant.workspace_id) as session:
        return list(await session.scalars(select(ToolInvocation)))


async def test_answer_cites_tool_result_with_source_and_fetch_time(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    llm_harness.plan = _plan(("echo", {"text": "hello"}))
    llm_harness.response = "The tool returned hello [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "what does echo say")

    assert answer["grounding"]["status"] == "grounded"
    assert answer["grounding"]["planner"] == "ran"
    citation = answer["citations"][0]
    assert citation["kind"] == "tool"
    assert citation["tool_id"] == "mock.echo"
    assert citation["fetched_at"]
    assert citation["arguments"] == {"text": "hello"}
    assert "echo: hello" in citation["excerpt"]
    assert citation["chunk_id"] is None

    call = answer["grounding"]["tool_calls"][0]
    assert (call["tool_id"], call["status"], call["code"]) == ("mock.echo", "succeeded", None)
    assert call["invocation_id"] == citation["invocation_id"]
    assert 'kind="live"' in llm_harness.prompts[0]
    assert "echo: hello" in llm_harness.prompts[0]
    assert world.state.calls == ["echo"]


async def test_every_step_is_recorded_without_content(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    llm_harness.plan = _plan(("echo", {"text": "private-argument"}))
    llm_harness.response = "It echoed [S1]."
    await harness.ask(client, tenant.workspace_id, "private-question")

    async with tenant_scope(harness.container.session_factory, tenant.workspace_id) as session:
        usage = list(await session.scalars(select(UsageRecord)))
        audit = list(await session.scalars(select(AuditEvent)))
    assert len(usage) == 2, "planning and answering are both counted"
    query = next(event for event in audit if event.action == "knowledge.query")
    assert query.event_metadata["tool_ids"] == ["mock.echo"]
    assert query.event_metadata["planner"] == "ran"
    assert {"tool.invoke", "tool.invoke.result"} <= {event.action for event in audit}
    text = json.dumps([event.event_metadata for event in audit])
    assert "private-argument" not in text
    assert "private-question" not in text
    invocations = await _invocations(harness, tenant)
    assert [row.status for row in invocations] == ["succeeded"]


async def test_documents_and_tools_answer_together_with_distinct_markers(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id, name="Policies")
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    llm_harness.plan = _plan(("echo", {"text": "live"}))
    llm_harness.response = "Zebra rule applies [S1]. Live state is live [S2]."
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    assert [(c["marker"], c["kind"]) for c in answer["citations"]] == [
        ("S1", "document"),
        ("S2", "tool"),
    ]
    assert all(c["used"] for c in answer["citations"])


async def test_no_tools_needed_makes_no_call(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    answer = await harness.ask(client, tenant.workspace_id, "nothing relevant anywhere")
    assert answer["grounding"]["planner"] == "ran"
    assert answer["grounding"]["status"] == "no_evidence"
    assert world.state.calls == []
    assert llm_harness.prompts == [], "no evidence still means no answering call"


async def test_no_callable_tools_and_no_history_skips_planning(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base = (await _ready(harness))[:3]
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    assert llm_harness.plan_prompts == []
    assert answer["grounding"]["planner"] == "not_run"


async def test_viewer_gets_no_planning_and_no_tools(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, _client, _base, _server = await _ready(harness, "echo")
    viewer = await harness.member_of(tenant.workspace_id, Role.VIEWER)
    client = await harness.login(viewer.email)
    llm_harness.plan = _plan(("echo", {"text": "x"}))
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    assert llm_harness.plan_prompts == []
    assert world.state.calls == []
    assert answer["grounding"]["tool_calls"] == []


async def test_catalog_excludes_tools_that_cannot_run(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    """Unreviewed, write-classified and disabled tools are never offered to the model."""
    tenant, client, base, _server = await _ready(harness, "echo")
    await _approve(client, base, "leaky", enabled=False)
    await _approve(client, base, "huge", access="write", risk_level=2)
    await harness.ask(client, tenant.workspace_id, "anything")
    prompt = llm_harness.plan_prompts[0]
    assert "mock.echo" in prompt
    for absent in ("mock.delete_everything", "mock.leaky", "mock.huge", "mock.boom"):
        assert absent not in prompt


async def test_invalid_arguments_are_refused_by_the_gateway(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    llm_harness.plan = _plan(("echo", {"text": "x", "extra": "y"}))
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    call = answer["grounding"]["tool_calls"][0]
    assert (call["status"], call["code"]) == ("denied", "validation_failed")
    assert world.state.calls == []
    assert any("did not return data" in note for note in answer["grounding"]["notes"])
    assert (await _invocations(harness, tenant))[0].status == "denied"


async def test_call_limit_applies_to_the_model_plan(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    harness.container.settings.planner_max_tool_calls = 2
    llm_harness.plan = _plan(*(("echo", {"text": str(n)}) for n in range(5)))
    llm_harness.response = "Done [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    assert world.state.calls == ["echo", "echo"]
    assert any("exceeded the limit" in note for note in answer["grounding"]["notes"])


async def test_unparseable_plan_falls_back_to_documents_and_says_so(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    llm_harness.plan = "I would call the echo tool for you."
    llm_harness.response = "Zebra rule applies [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    assert answer["grounding"]["status"] == "grounded"
    assert answer["grounding"]["planner"] == "failed"
    assert any("not consulted" in note for note in answer["grounding"]["notes"])
    assert world.state.calls == []


async def test_model_outage_during_planning_is_not_reported_as_a_checked_system(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    llm_harness.plan_error = ProviderUnavailable("down")
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    assert answer["grounding"]["planner"] == "failed"
    assert answer["grounding"]["status"] == "no_evidence"
    assert any("not consulted" in note for note in answer["grounding"]["notes"])


async def test_tool_outage_is_stated_and_the_model_is_told(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    world.reachable = False
    llm_harness.plan = _plan(("echo", {"text": "x"}))
    llm_harness.response = "Zebra rule applies [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    call = answer["grounding"]["tool_calls"][0]
    assert call["status"] == "failed"
    assert call["fetched_at"] is None
    assert any("mock.echo did not return data" in note for note in answer["grounding"]["notes"])
    assert "Live data was not retrieved from: mock.echo" in llm_harness.prompts[0]
    assert all(c["kind"] == "document" for c in answer["citations"])


async def test_outage_with_no_documents_does_not_generate_an_answer(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    world.reachable = False
    llm_harness.plan = _plan(("echo", {"text": "x"}))
    answer = await harness.ask(client, tenant.workspace_id, "what is the live state")
    assert answer["grounding"]["status"] == "no_evidence"
    assert llm_harness.prompts == []


async def test_model_saying_not_found_does_not_hide_that_a_system_answered(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    llm_harness.plan = _plan(("echo", {"text": "x"}))
    llm_harness.response = "I could not find this in the available sources."
    answer = await harness.ask(client, tenant.workspace_id, "anything")
    assert answer["grounding"]["status"] == "no_evidence"
    assert answer["grounding"]["tool_calls"][0]["status"] == "succeeded"
    assert any("live system returned data" in note for note in answer["grounding"]["notes"])


async def test_tool_reported_error_text_never_reaches_the_model(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "boom")
    llm_harness.plan = _plan(("boom", {}))
    answer = await harness.ask(client, tenant.workspace_id, "explode")
    assert answer["grounding"]["tool_calls"][0]["status"] == "tool_error"
    assert answer["grounding"]["status"] == "no_evidence"
    assert llm_harness.prompts == []
    assert "exploded" not in json.dumps(answer)


async def test_large_output_is_bounded_and_marked_incomplete(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "huge")
    harness.container.settings.assistant_max_tool_context_chars = 100
    llm_harness.plan = _plan(("huge", {}))
    llm_harness.response = "It is large [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "how big")
    assert answer["citations"][0]["truncated"] is True
    assert len(answer["citations"][0]["excerpt"]) <= 100
    assert 'status="incomplete"' in llm_harness.prompts[0]
    assert "x" * 101 not in llm_harness.prompts[0]
    assert any("cut off" in note for note in answer["grounding"]["notes"])


async def test_secrets_in_tool_output_reach_neither_model_nor_user(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "leaky")
    llm_harness.plan = _plan(("leaky", {}))
    llm_harness.response = "A token exists [S1]."
    answer = await harness.ask(client, tenant.workspace_id, "show token")
    assert FAKE_TOKEN not in llm_harness.prompts[0]
    assert FAKE_TOKEN not in json.dumps(answer)


async def test_time_limit_returns_excerpts_labelled_not_generated(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    harness.container.settings.assistant_max_seconds = 0
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    assert answer["grounding"]["status"] == "budget_exceeded"
    assert "limit" in answer["content"]
    assert llm_harness.prompts == []
    assert world.state.calls == []


async def test_token_limit_stops_the_answer_after_planning_used_it(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    harness.container.settings.assistant_max_tokens_per_question = 1000
    llm_harness.tokens = (900, 90)
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    assert answer["grounding"]["status"] == "budget_exceeded"
    assert llm_harness.prompts == []


async def test_output_tokens_are_capped_by_what_remains(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    harness.container.settings.assistant_max_tokens_per_question = 2000
    await harness.ask(client, tenant.workspace_id, "zebra")
    assert llm_harness.max_tokens[-1] < 2000


# --- Follow-up context ------------------------------------------------------------------


async def _with_document(harness: Harness) -> tuple[Any, Any, dict, dict]:
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    source = await harness.create_source(client, tenant.workspace_id, name="Policies")
    document = await harness.upload(
        client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra refunds need approval."
    )
    return tenant, client, source, document


async def test_followup_search_uses_the_rewritten_question(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant, client, _source, _document = await _with_document(harness)
    conversation = await harness.conversation(client, tenant.workspace_id)
    llm_harness.response = "They need approval [S1]."
    await harness.ask(client, tenant.workspace_id, "zebra refunds", conversation)
    assert llm_harness.plan_prompts == [], "nothing to plan on a first question without tools"

    llm_harness.plan = json.dumps({"calls": [], "search_query": "zebra refunds approval"})
    answer = await harness.ask(client, tenant.workspace_id, "and who decides that?", conversation)
    assert "zebra refunds" in llm_harness.plan_prompts[0]
    assert answer["grounding"]["search_query"] == "zebra refunds approval"
    assert answer["citations"], "the rewritten question found the document"
    assert "and who decides that?" in llm_harness.prompts[-1], "the user's words are answered"


async def test_garbled_rewrite_cannot_replace_a_question_that_finds_documents(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant, client, _source, _document = await _with_document(harness)
    conversation = await harness.conversation(client, tenant.workspace_id)
    await harness.ask(client, tenant.workspace_id, "zebra refunds", conversation)
    llm_harness.plan = json.dumps({"calls": [], "search_query": "Summarize it"})
    answer = await harness.ask(client, tenant.workspace_id, "zebra refunds approval", conversation)
    assert answer["grounding"]["search_query"] is None
    assert answer["citations"]


async def test_first_question_ignores_a_model_rewrite(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, _server = await _ready(harness, "echo")
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    llm_harness.plan = json.dumps({"calls": [], "search_query": "something else entirely"})
    answer = await harness.ask(client, tenant.workspace_id, "zebra")
    assert answer["grounding"]["search_query"] is None
    assert answer["citations"]


async def test_earlier_answer_informs_the_answer_but_is_not_citable(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant, client, _source, _document = await _with_document(harness)
    conversation = await harness.conversation(client, tenant.workspace_id)
    llm_harness.response = "Finance approves zebra refunds [S1]."
    await harness.ask(client, tenant.workspace_id, "zebra refunds", conversation)
    llm_harness.plan = json.dumps({"calls": [], "search_query": "zebra refunds"})
    await harness.ask(client, tenant.workspace_id, "zebra again", conversation)
    prompt = llm_harness.prompts[-1]
    block = prompt.split("<evidence-")[0]
    assert "<earlier-conversation-" in block
    assert "Finance approves zebra refunds" in block
    assert "[S1]" not in block


async def test_answer_from_a_deleted_document_leaves_later_prompts(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant, client, _source, document = await _with_document(harness)
    conversation = await harness.conversation(client, tenant.workspace_id)
    llm_harness.response = "CANARY-ANSWER approves zebra refunds [S1]."
    await harness.ask(client, tenant.workspace_id, "zebra refunds", conversation)

    other = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, other["id"], "b.md", "# B\n\nZebra other.")
    document_id = document["document"]["id"]
    deleted = await client.delete(
        f"/api/v1/workspaces/{tenant.workspace_id}/documents/{document_id}"
    )
    assert deleted.status_code == 204

    llm_harness.plan = json.dumps({"calls": [], "search_query": "zebra"})
    llm_harness.response = "Other [S1]."
    await harness.ask(client, tenant.workspace_id, "zebra", conversation)
    prompt = llm_harness.prompts[-1]
    assert "CANARY-ANSWER" not in prompt
    assert "CANARY-ANSWER" not in llm_harness.plan_prompts[-1]
    assert "Earlier question: zebra refunds" in prompt


async def test_conversation_context_does_not_cross_users(
    harness: Harness, llm_harness: FakeLLM
) -> None:
    tenant, client, _source, _document = await _with_document(harness)
    conversation = await harness.conversation(client, tenant.workspace_id)
    await harness.ask(client, tenant.workspace_id, "zebra private question", conversation)

    member = await harness.member_of(tenant.workspace_id)
    member_client = await harness.login(member.email)
    theirs = await harness.conversation(member_client, tenant.workspace_id)
    await harness.ask(member_client, tenant.workspace_id, "zebra", theirs)
    assert "private question" not in llm_harness.prompts[-1]
    url = f"/api/v1/workspaces/{tenant.workspace_id}/conversations/{conversation}/messages"
    denied = await member_client.post(url, json={"question": "continue"})
    assert denied.status_code == 404


# --- Reading stored answers -------------------------------------------------------------


async def _answered_from_tool(
    harness: Harness, llm_harness: FakeLLM, role: Role = Role.OWNER
) -> tuple[Any, Any, str, str, dict]:
    tenant, client, base, server = await _ready(harness, "echo")
    conversation = await harness.conversation(client, tenant.workspace_id)
    llm_harness.plan = _plan(("echo", {"text": "CANARY-LIVE"}))
    llm_harness.response = "CANARY-LIVE was returned [S1]."
    await harness.ask(client, tenant.workspace_id, "echo it", conversation)
    return tenant, client, base, f"{base}/conversations/{conversation}/messages", server


async def test_stored_tool_answer_is_withheld_when_its_server_is_disabled(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    _tenant, client, base, messages, server = await _answered_from_tool(harness, llm_harness)
    before = (await client.get(messages)).json()[1]
    assert before["grounding"]["status"] == "grounded"

    disabled = await client.patch(f"{base}/tool-servers/{server['id']}", json={"enabled": False})
    assert disabled.status_code == 200, disabled.text
    after = (await client.get(messages)).json()[1]
    assert after["grounding"]["status"] == "withheld"
    assert after["citations"][0]["access_revoked"] is True
    assert "CANARY-LIVE" not in after["content"]
    assert "CANARY-LIVE" not in json.dumps(after["citations"])


async def test_stored_tool_answer_is_withheld_after_losing_tool_permission(
    harness: Harness, world: MockWorld, llm_harness: FakeLLM
) -> None:
    tenant, client, _base, messages, _server = await _answered_from_tool(harness, llm_harness)
    async with global_scope(harness.container.session_factory) as session:
        await session.execute(
            update(Membership).where(Membership.user_id == tenant.user_id).values(role=Role.VIEWER)
        )
    after = (await client.get(messages)).json()[1]
    assert after["grounding"]["status"] == "withheld"
    assert "CANARY-LIVE" not in json.dumps(after["citations"])


async def test_answers_stored_before_this_change_still_read(harness: Harness) -> None:
    """Older citations have no `kind`. They must keep working as documents."""
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    source = await harness.create_source(client, tenant.workspace_id)
    await harness.upload(client, tenant.workspace_id, source["id"], "z.md", "# Z\n\nZebra rule.")
    conversation = await harness.conversation(client, tenant.workspace_id)
    await harness.ask(client, tenant.workspace_id, "zebra", conversation)
    from opsassist.models import Message

    async with tenant_scope(harness.container.session_factory, tenant.workspace_id) as session:
        for message in await session.scalars(select(Message)):
            message.citations = [
                {key: value for key, value in citation.items() if key != "kind"}
                for citation in message.citations
            ]
    url = f"/api/v1/workspaces/{tenant.workspace_id}/conversations/{conversation}/messages"
    stored = (await client.get(url)).json()[1]
    assert stored["citations"][0]["kind"] == "document"
    assert stored["grounding"]["status"] == "extractive"


__all__ = ["_discover", "_register"]
