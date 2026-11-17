"""Read-only checks against real Gmail. Skipped unless configured: see docs/GMAIL_SETUP.md.

Run with: uv run pytest -m live_gmail tests/integration/test_gmail_live.py -v

This reads your real mailbox and changes nothing. Output is not printed, except counts. It needs
GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET, and GMAIL_TEST_REFRESH_TOKEN_FILE pointing at a
file holding a refresh token. Keep that file outside the repository and OneDrive.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import httpx
import pytest

from opsassist.config import Settings
from opsassist.integrations.gmail_client import (
    GMAIL_READONLY_SCOPE,
    AccessTokens,
    GmailApi,
    GmailConnection,
    new_http_client,
)
from opsassist.integrations.gmail_provider import thread_summary

CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
TOKEN_FILE = os.environ.get("GMAIL_TEST_REFRESH_TOKEN_FILE", "")

pytestmark = [
    pytest.mark.live_gmail,
    pytest.mark.skipif(
        not (CLIENT_ID and CLIENT_SECRET and TOKEN_FILE),
        reason="Set GOOGLE_OAUTH_CLIENT_ID, _SECRET and GMAIL_TEST_REFRESH_TOKEN_FILE",
    ),
]


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        database_url="postgresql+asyncpg://unused:unused@localhost/unused",
        google_oauth_client_id=CLIENT_ID,
        google_oauth_client_secret=CLIENT_SECRET,
    )


async def test_real_mailbox_reads_and_cannot_send() -> None:
    settings = _settings()
    connection = GmailConnection(
        uuid.uuid4(), uuid.uuid4(), "", Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    )
    async with new_http_client(timeout_seconds=30, transport=None) as http:
        tokens = AccessTokens()
        api = GmailApi(http, settings, tokens, connection)
        profile = await api.check_authentication()
        address = profile["emailAddress"]

        listed = await api.get("/gmail/v1/users/me/threads", {"maxResults": 3})
        count = 0
        for item in listed.get("threads") or []:
            full = await api.get(f"/gmail/v1/users/me/threads/{item['id']}", {"format": "full"})
            summary = thread_summary(full, address, body_chars=500)
            assert summary["messages"], "a real thread parses into messages"
            count += 1
        print(f"parsed {count} real threads")

        token = await tokens.get(http, settings, connection)
        info = await http.get(
            "https://oauth2.googleapis.com/tokeninfo", params={"access_token": token}
        )
        assert info.status_code == 200, info.status_code
        assert info.json()["scope"].split() == [GMAIL_READONLY_SCOPE]

        # An empty message creates nothing. A token able to send would get 400, not 403.
        sent = await http.post(
            f"{settings.gmail_api_url}/gmail/v1/users/me/messages/send",
            headers={"Authorization": f"Bearer {token}"},
            json={"raw": ""},
        )
        assert sent.status_code == 403, f"expected 403, got {sent.status_code}"
        assert isinstance(sent, httpx.Response)
