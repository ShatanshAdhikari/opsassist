"""Read-only Gmail tools, as a native provider behind the tool contract.

Search returns whole threads rather than IDs to look up, because the planner runs once and cannot
feed one call's result into another (ADR-0013). Attachments are listed but never downloaded.

Anyone can send you an email, so all of this output is untrusted data. HTML is reduced to plain text
here and never rendered. Whether a thread awaits a reply is worked out in code, not by a model.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Any

import httpx

from opsassist.config import Settings
from opsassist.credentials.crypto import CredentialDecryptError
from opsassist.credentials.store import CredentialStore
from opsassist.db import SessionFactory, tenant_scope
from opsassist.integrations.gmail_client import (
    AccessTokens,
    GmailApi,
    GmailConnection,
    GmailNotFound,
    GmailRejected,
    new_http_client,
)
from opsassist.integrations.gmail_format import render_search
from opsassist.integrations.gmail_signals import AUTOMATION_HEADERS, sender_kind
from opsassist.models import IntegrationConnection
from opsassist.observability import redact_secrets, redact_tree
from opsassist.tools.base import (
    CredentialRejected,
    DefinitionChanged,
    ProviderTimeout,
    ToolDescriptor,
    ToolMissing,
    ToolOutput,
)

_SEARCH_DEFAULT_THREADS = 5
_SEARCH_MAX_THREADS = 10
_SEARCH_BODY_CHARS = 1500
_THREAD_BODY_CHARS = 6000
_MAX_RAW_BODY_CHARS = 2_000_000
_MAX_MESSAGES_PER_THREAD = 30
_MAX_ATTACHMENTS_LISTED = 20
_HEADERS = ("from", "to", "cc", "subject", "date", *AUTOMATION_HEADERS)
_READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "open_world_hint": True}
_SKIPPED_HTML = {"script", "style", "head", "title", "template"}
_BLOCK_HTML = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "blockquote", "table"}

_WEEKS = re.compile(r"\b(newer_than|older_than):(\d{1,4})w\b", re.IGNORECASE)


# Gmail's documented search operators. https://support.google.com/mail/answer/7190
_GMAIL_OPERATORS = frozenset(
    {
        "from", "to", "cc", "bcc", "subject", "label", "has", "list", "filename", "in", "is",
        "after", "before", "older", "newer", "older_than", "newer_than", "deliveredto",
        "category", "size", "larger", "smaller", "rfc822msgid",
    }
)  # fmt: skip
# An operator token: name:value, optionally negated, not part of a URL such as https://.
_OPERATOR = re.compile(r"(?<![\w/:.])-?([A-Za-z_]+):(?!//)\S+")


@dataclass(frozen=True)
class GmailQuery:
    text: str
    dropped: tuple[str, ...]


def gmail_query(query: str) -> GmailQuery:
    """Make a model-written query one Gmail can run, and say what changed.

    Gmail never rejects a bad query. It treats an unknown operator such as awaits:reply as text,
    and newer_than:1w as matching nothing, so the search returns no mail and the answer reads as
    "nothing needs a reply". Small models write both (qwen2.5:3b and llama3.2:3b, 2026-11-18).
    Weeks become days, and unknown operators are dropped and reported in the result, so the person
    can see the search that ran. Reply state is computed here, not by Gmail, so nothing is lost.
    """
    dropped: list[str] = []

    def keep_known(match: re.Match[str]) -> str:
        if match.group(1).lower() in _GMAIL_OPERATORS:
            return match.group(0)
        dropped.append(match.group(0))
        return ""

    text = _WEEKS.sub(lambda m: f"{m.group(1)}:{int(m.group(2)) * 7}d", query)
    text = " ".join(_OPERATOR.sub(keep_known, text).split())
    return GmailQuery(text, tuple(dropped))


TOOLS: dict[str, ToolDescriptor] = {
    tool.name: tool
    for tool in (
        ToolDescriptor(
            name="threads_search",
            description=(
                "Search this person's mailbox and return matching conversations with their "
                "messages in order, and whether each awaits a reply. Live data. The query uses "
                "Gmail search syntax, such as from:alice, is:unread, newer_than:7d, in:inbox. "
                "Time units for newer_than and older_than are d, m and y only."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 500},
                    "limit": {"type": "integer", "minimum": 1, "maximum": _SEARCH_MAX_THREADS},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
            annotations=_READ_ONLY,
        ),
        ToolDescriptor(
            name="thread_get",
            description="Get one whole conversation by its Gmail thread ID. Live data.",
            input_schema={
                "type": "object",
                "properties": {
                    "thread_id": {
                        "type": "string",
                        "pattern": r"^[A-Za-z0-9_\-]{1,100}$",
                    },
                },
                "required": ["thread_id"],
                "additionalProperties": False,
            },
            annotations=_READ_ONLY,
        ),
    )
}


class _TextExtractor(HTMLParser):
    """Keeps text only. Links, images and scripts do not survive, so nothing can fire later."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_HTML:
            self._skipping += 1
        elif tag in _BLOCK_HTML:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_HTML and self._skipping:
            self._skipping -= 1

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).splitlines())
    return "\n".join(line for line in lines if line)


def _decode_body(data: str) -> str:
    # Only a little of any body is kept, so a huge one is not worth decoding and parsing.
    data = data[:_MAX_RAW_BODY_CHARS]
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return ""


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    found = [part]
    for child in part.get("parts") or []:
        found.extend(_walk(child))
    return found


def _is_attachment(part: dict[str, Any]) -> bool:
    return bool(part.get("filename")) or bool((part.get("body") or {}).get("attachmentId"))


def _message_body(payload: dict[str, Any]) -> str:
    """Prefer the plain text a sender supplied, and otherwise reduce the HTML."""
    parts = [part for part in _walk(payload) if not _is_attachment(part)]
    for wanted, convert in (("text/plain", str), ("text/html", html_to_text)):
        for part in parts:
            data = (part.get("body") or {}).get("data")
            if part.get("mimeType") == wanted and data:
                return convert(_decode_body(data)).strip()
    return ""


def _attachments(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "filename": part.get("filename"),
            "mime_type": part.get("mimeType"),
            "size": (part.get("body") or {}).get("size"),
        }
        for part in _walk(payload)
        if _is_attachment(part)
    ][:_MAX_ATTACHMENTS_LISTED]


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = str(header.get("name", "")).lower()
        if name in _HEADERS and name not in found:
            found[name] = str(header.get("value", ""))
    return found


def _internal_date(message: dict[str, Any]) -> int:
    value = str(message.get("internalDate", "0"))
    return int(value) if value.isdigit() else 0


def _message(message: dict[str, Any], address: str, *, body_chars: int) -> dict[str, Any]:
    payload = message.get("payload") or {}
    headers = _headers(payload)
    labels = list(message.get("labelIds") or [])
    sender = parseaddr(headers.get("from", ""))[1].lower()
    body = _message_body(payload)
    from_me = "SENT" in labels or sender == address.lower()
    kind = "person" if from_me else sender_kind(headers, sender)
    return {
        "message_id": message.get("id"),
        "from": headers.get("from"),
        "to": headers.get("to"),
        "cc": headers.get("cc"),
        "subject": headers.get("subject"),
        "date": headers.get("date"),
        "internal_timestamp_ms": _internal_date(message),
        "from_me": from_me,
        "automated": kind == "automated",
        "mailing_list": kind == "mailing_list",
        "is_draft": "DRAFT" in labels,
        "unread": "UNREAD" in labels,
        "labels": labels,
        "body": body[:body_chars],
        "body_truncated": len(body) > body_chars,
        "attachments": _attachments(payload),
    }


def thread_summary(thread: dict[str, Any], address: str, *, body_chars: int) -> dict[str, Any]:
    """Messages oldest first. If a thread is too long, the newest ones are kept and it says so."""
    raw = sorted(thread.get("messages") or [], key=_internal_date)
    messages = [_message(item, address, body_chars=body_chars) for item in raw]
    sent = [item for item in messages if not item["is_draft"]]
    kept = messages[-_MAX_MESSAGES_PER_THREAD:]
    thread_id = thread.get("id")
    return {
        "thread_id": thread_id,
        "url": f"https://mail.google.com/mail/u/0/#all/{thread_id}",
        "subject": sent[0]["subject"] if sent else None,
        "message_count": len(messages),
        "messages_omitted": len(messages) - len(kept),
        "last_message_date": sent[-1]["date"] if sent else None,
        # Says only that a person other than the owner wrote last, not through a mailing list.
        # Whether a reply is really needed is a judgement left to people.
        "awaiting_reply": bool(sent)
        and not sent[-1]["from_me"]
        and not sent[-1]["automated"]
        and not sent[-1]["mailing_list"],
        "last_message_automated": bool(sent) and sent[-1]["automated"],
        "last_message_via_list": bool(sent) and sent[-1]["mailing_list"],
        "has_unread": any(item["unread"] for item in messages),
        "messages": kept,
    }


class _ToolRefusal(Exception):
    """A tool-level answer written by this module, safe to return to the caller."""


@dataclass(frozen=True)
class GmailDeps:
    settings: Settings
    session_factory: SessionFactory
    credentials: CredentialStore
    tokens: AccessTokens
    transport: httpx.AsyncBaseTransport | None = None


async def load_connection(
    deps: GmailDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> GmailConnection:
    """Read settings and decrypt the token for one operation. A deleted connection fails here."""
    async with tenant_scope(deps.session_factory, workspace_id) as session:
        row = await session.get(IntegrationConnection, connection_id)
        if row is None or row.credential_id is None:
            raise CredentialRejected()
        try:
            secret = await deps.credentials.get(session, row.credential_id)
        except CredentialDecryptError as exc:
            raise CredentialRejected() from exc
        return GmailConnection(
            connection_id=row.id,
            credential_id=row.credential_id,
            address=str(row.config["address"]),
            refresh_token=str(secret["refresh_token"]),
        )


class GmailToolProvider:
    def __init__(self, deps: GmailDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID) -> None:
        self._deps = deps
        self._workspace_id = workspace_id
        self._connection_id = connection_id

    async def discover(self, *, timeout_seconds: float) -> list[ToolDescriptor]:
        await self.verify(timeout_seconds=timeout_seconds)
        return list(TOOLS.values())

    async def verify(self, *, timeout_seconds: float) -> None:
        """Prove the sign-in still works, by reading the mailbox profile."""

        async def check(api: GmailApi, _connection: GmailConnection) -> None:
            await api.check_authentication()

        await self._with_api(check, timeout_seconds)

    async def invoke(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        expected_hash: str,
        timeout_seconds: float,
    ) -> ToolOutput:
        descriptor = TOOLS.get(name)
        if descriptor is None:
            raise ToolMissing()
        if descriptor.definition_hash != expected_hash:
            raise DefinitionChanged(descriptor)

        async def run(api: GmailApi, connection: GmailConnection) -> ToolOutput:
            try:
                if name == "threads_search":
                    return await self._threads_search(api, connection, arguments)
                return await self._thread_get(api, connection, arguments)
            except _ToolRefusal as exc:
                return ToolOutput(text=str(exc), structured=None, is_error=True)
            except GmailNotFound:
                return ToolOutput(
                    text="Gmail reports no such conversation in this mailbox.",
                    structured=None,
                    is_error=True,
                )
            except GmailRejected:
                return ToolOutput(
                    text="Gmail could not process this request.", structured=None, is_error=True
                )

        return await self._with_api(run, timeout_seconds)

    async def _with_api[T](
        self,
        operation: Callable[[GmailApi, GmailConnection], Awaitable[T]],
        timeout_seconds: float,
    ) -> T:
        deps = self._deps
        connection = await load_connection(deps, self._workspace_id, self._connection_id)
        try:
            async with (
                asyncio.timeout(timeout_seconds),
                new_http_client(timeout_seconds=timeout_seconds, transport=deps.transport) as http,
            ):
                api = GmailApi(http, deps.settings, deps.tokens, connection)
                return await operation(api, connection)
        except TimeoutError as exc:
            raise ProviderTimeout() from exc

    def _finish(
        self,
        summary: str,
        data: dict[str, Any],
        render: Callable[[dict[str, Any]], str] | None = None,
    ) -> ToolOutput:
        max_chars = self._deps.settings.tool_max_output_chars
        redacted = redact_tree(data)
        encoded = json.dumps(redacted)
        # Rendered from the redacted data, so the model's copy holds no secret the JSON lost.
        rendered = redact_secrets(render(redacted)) if render is not None else None
        if len(encoded) <= max_chars:
            if rendered is None:
                return ToolOutput(text=redact_secrets(summary), structured=redacted, is_error=False)
            return ToolOutput(
                text=redact_secrets(summary),
                structured=redacted,
                is_error=False,
                truncated=len(rendered) > max_chars,
                model_text=rendered[:max_chars],
            )
        if rendered is not None:
            return ToolOutput(
                text=rendered[:max_chars],
                structured=None,
                is_error=False,
                truncated=len(rendered) > max_chars,
            )
        return ToolOutput(text=encoded[:max_chars], structured=None, is_error=False, truncated=True)

    async def _threads_search(
        self, api: GmailApi, connection: GmailConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        query = gmail_query(arguments["query"])
        listed = await api.get(
            "/gmail/v1/users/me/threads",
            {
                "q": query.text,
                "maxResults": arguments.get("limit", _SEARCH_DEFAULT_THREADS),
            },
        )
        threads = []
        for item in listed.get("threads") or []:
            try:
                full = await api.get(f"/gmail/v1/users/me/threads/{item['id']}", {"format": "full"})
            except GmailNotFound:
                # Deleted between the two requests. It is no longer in the mailbox.
                continue
            threads.append(thread_summary(full, connection.address, body_chars=_SEARCH_BODY_CHARS))
        data = {
            "mailbox": connection.address,
            # The query Gmail ran, which can differ from the arguments (see gmail_query).
            "query": query.text,
            "dropped_terms": list(query.dropped),
            "threads": threads,
            # Absent results must not read as "there is no more mail".
            "more_results_exist": bool(listed.get("nextPageToken")),
            "result_size_estimate": listed.get("resultSizeEstimate"),
        }
        return self._finish(f"{len(threads)} conversations returned.", data, render_search)

    async def _thread_get(
        self, api: GmailApi, connection: GmailConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        full = await api.get(
            f"/gmail/v1/users/me/threads/{arguments['thread_id']}", {"format": "full"}
        )
        summary = thread_summary(full, connection.address, body_chars=_THREAD_BODY_CHARS)
        return self._finish(f"{summary['message_count']} messages returned.", summary)
