"""Whether a message came from an automated sender, decided in code from standard headers.

A model shown "the last message is not from you" took that to mean a reply was needed, even for
LinkedIn notifications (docs/PROGRESS.md, 2026-11-17). These signals let the result say so instead.

The headers are set by the sender, so a person could fake them. The effect is only that a thread is
shown as not awaiting a reply, which a person can still see and read. It never hides mail.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

# Headers that _headers in gmail_provider must collect for this check.
AUTOMATION_HEADERS = ("auto-submitted", "precedence", "list-unsubscribe", "list-id")

_BULK_PRECEDENCE = {"bulk", "list", "junk"}
# Matched against the local part of the address, before the @.
_AUTOMATED_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer[-_.]?daemon|postmaster|bounces?)([-+_.].*)?$"
)


def is_automated(headers: Mapping[str, str], sender_address: str) -> bool:
    """`headers` has lower-case names. `sender_address` is the bare address, or empty."""
    auto_submitted = headers.get("auto-submitted", "").strip().lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if headers.get("precedence", "").strip().lower() in _BULK_PRECEDENCE:
        return True
    if headers.get("list-unsubscribe", "").strip() or headers.get("list-id", "").strip():
        return True
    local = sender_address.lower().partition("@")[0]
    return bool(_AUTOMATED_LOCAL.match(local))
