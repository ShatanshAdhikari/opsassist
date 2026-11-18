"""A compact plain-text rendering of Gmail search results, written for the model to read.

The raw search result is JSON with every header, label and ID, about 5,000 tokens for five threads.
A small local model read that slowly and then reported it held nothing (docs/PROGRESS.md,
2026-11-17). This keeps only what a summary needs. The full JSON still goes to API callers.

Mail is untrusted. Whitespace inside every field is collapsed, so a subject or body cannot start a
line of its own and pass itself off as another conversation or message in this layout.
"""

from __future__ import annotations

from typing import Any

SNIPPET_CHARS = 500
MESSAGES_SHOWN = 5


def _flat(value: Any) -> str:
    return " ".join(str(value or "").split())


def _snippet(body: Any, clipped_before: bool) -> str:
    text = _flat(body)
    if len(text) > SNIPPET_CHARS:
        return text[:SNIPPET_CHARS].rstrip() + " [cut]"
    return text + (" [cut]" if clipped_before and text else "")


def _message_line(number: int, message: dict[str, Any]) -> str:
    marks = []
    if message.get("from_me"):
        marks.append("sent by the mailbox owner")
    if message.get("automated"):
        marks.append("automated sender")
    if message.get("mailing_list"):
        marks.append("mailing list")
    if message.get("is_draft"):
        marks.append("unsent draft")
    if message.get("unread"):
        marks.append("unread")
    names = [_flat(item.get("filename")) for item in message.get("attachments") or []]
    attachments = [name for name in names if name]
    if attachments:
        marks.append("attachments: " + ", ".join(attachments))
    mark_text = f" ({'; '.join(marks)})" if marks else ""
    body = _snippet(message.get("body"), bool(message.get("body_truncated")))
    sender = _flat(message.get("from")) or "unknown sender"
    date = _flat(message.get("date")) or "unknown date"
    return f"  Message {number}, {date}, from {sender}{mark_text}: {body or '(no text)'}"


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _reply_state(thread: dict[str, Any]) -> str:
    if thread.get("awaiting_reply"):
        return "Awaiting a reply: yes, someone else wrote last."
    if thread.get("last_message_automated"):
        return "Awaiting a reply: no, the last message is from an automated sender."
    if thread.get("last_message_via_list"):
        return "Awaiting a reply: unclear, the last message came through a mailing list."
    if not any(not item.get("is_draft") for item in thread.get("messages") or []):
        return "Awaiting a reply: no, nothing has been sent in this conversation."
    return "Awaiting a reply: no, the mailbox owner wrote last."


def _reply_counts(threads: list[dict[str, Any]]) -> str:
    """Count reply states in code, so a small model does not have to (llama3.2:3b miscounted)."""
    states = [_reply_state(thread) for thread in threads]
    yes = sum(state.startswith("Awaiting a reply: yes") for state in states)
    unclear = sum(state.startswith("Awaiting a reply: unclear") for state in states)
    return (
        f"Of these, awaiting a reply: {yes}. Unclear, through a mailing list: {unclear}."
        f" Not awaiting a reply: {len(states) - yes - unclear}."
    )


def render_search(data: dict[str, Any]) -> str:
    """Render the dict built by the threads_search tool. Expects already-redacted data."""
    threads = data.get("threads") or []
    lines = [f"Mailbox: {_flat(data.get('mailbox'))}. Conversations returned: {len(threads)}."]
    if data.get("query"):
        lines.append(f"Gmail search run: {_flat(data.get('query'))}")
    if data.get("dropped_terms"):
        terms = ", ".join(_flat(term) for term in data["dropped_terms"])
        lines.append(f"Left out because Gmail does not support them: {terms}")
    if threads:
        lines.append(_reply_counts(threads))
    if data.get("more_results_exist"):
        lines.append("More conversations match this search than are shown here.")
    for index, thread in enumerate(threads, start=1):
        messages = thread.get("messages") or []
        shown = messages[-MESSAGES_SHOWN:]
        hidden = len(messages) - len(shown) + int(thread.get("messages_omitted") or 0)
        lines.append("")
        lines.append(f"Conversation {index}: {_flat(thread.get('subject')) or '(no subject)'}")
        lines.append(
            f"  {thread.get('message_count', len(messages))} messages."
            f" Has unread: {_yes_no(thread.get('has_unread'))}. {_reply_state(thread)}"
        )
        if hidden:
            lines.append(f"  {hidden} earlier messages not shown.")
        lines.extend(_message_line(hidden + 1 + offset, item) for offset, item in enumerate(shown))
    if not threads:
        lines.append("No conversations matched this search.")
    return "\n".join(lines)
