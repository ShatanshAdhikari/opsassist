"""What kind of sender a message came from, decided in code from standard headers.

A model shown "the last message is not from you" took that to mean a reply was needed, even for
LinkedIn notifications (docs/PROGRESS.md, 2026-11-17). These signals let the result say so instead.

Mailing lists are kept apart from automated mail. Google Groups and Mailman set `List-Id` on
messages that real people write, so a list message may well want an answer. Calling it automated
would report a colleague's question as needing none.

The headers are set by the sender, so a person could fake them. The effect is only that a thread is
shown as not awaiting a reply, or as unclear, and a person can still see and read it. It never
hides mail.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Literal

SenderKind = Literal["person", "automated", "mailing_list"]

# Headers that _headers in gmail_provider must collect for this check.
AUTOMATION_HEADERS = ("auto-submitted", "precedence", "list-unsubscribe", "list-id")

_BULK_PRECEDENCE = {"bulk", "junk"}
# A whole token of the local part, so notifications-noreply@ matches and noreplyann@ does not.
_AUTOMATED_LOCAL = re.compile(
    r"(^|[-+_.])(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?)"
    r"($|[-+_.])"
)


def sender_kind(headers: Mapping[str, str], sender_address: str) -> SenderKind:
    """`headers` has lower-case names. `sender_address` is the bare address, or empty."""
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return "automated"
    local = sender_address.lower().partition("@")[0]
    if _AUTOMATED_LOCAL.search(local):
        return "automated"
    precedence = headers.get("precedence", "").strip().lower()
    if headers.get("list-id", "").strip() or precedence == "list":
        return "mailing_list"
    if precedence in _BULK_PRECEDENCE or headers.get("list-unsubscribe", "").strip():
        return "automated"
    return "person"
