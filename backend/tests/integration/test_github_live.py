"""Read-only checks against real GitHub. Skipped unless configured: see docs/GITHUB_SETUP.md.

Run with: uv run pytest -m live_github tests/integration/test_github_live.py -v

The one request that is not a read posts an empty issue title. GitHub refuses that with 422 if the
token could write, so nothing is created either way, and a token that truly cannot write gets 403.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from opsassist.container import build_container
from opsassist.credentials.crypto import generate_key
from opsassist.integrations.github_client import (
    GitHubConnection,
    InstallationTokens,
    new_http_client,
)
from opsassist.models import Role
from tests.conftest import Harness, make_settings

APP_ID = os.environ.get("GITHUB_TEST_APP_ID", "")
INSTALLATION = os.environ.get("GITHUB_TEST_INSTALLATION_ID", "")
KEY_FILE = os.environ.get("GITHUB_TEST_PRIVATE_KEY_FILE", "")
REPO = os.environ.get("GITHUB_TEST_REPO", "")

pytestmark = [
    pytest.mark.live_github,
    pytest.mark.skipif(
        not (APP_ID and INSTALLATION and KEY_FILE and REPO),
        reason="Set GITHUB_TEST_APP_ID, _INSTALLATION_ID, _PRIVATE_KEY_FILE and _REPO",
    ),
]


@pytest_asyncio.fixture
async def live(
    clean_database: None, tmp_path: Path
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    settings = make_settings(
        tmp_path, credential_keys={"k1": generate_key()}, credential_active_key_id="k1"
    )
    harness = Harness(build_container(settings))
    tenant = await harness.tenant(Role.OWNER)
    client = await harness.login(tenant.email)
    base = f"/api/v1/workspaces/{tenant.workspace_id}"
    response = await client.post(
        f"{base}/connections/github",
        json={
            "slug": "github",
            "name": "Live",
            "app_id": APP_ID,
            "installation_id": int(INSTALLATION),
            "private_key": Path(KEY_FILE).read_text(encoding="utf-8"),
            "repositories": [REPO],
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    verified = await client.post(f"{base}/connections/{created['connection']['id']}/verify")
    assert verified.json() == {"verified": True, "code": None}, verified.text
    await client.post(f"{base}/tool-servers/{created['tool_server']['id']}/discover")
    for tool in (await client.get(f"{base}/tools")).json():
        await client.post(
            f"{base}/tools/{tool['id']}/review",
            json={
                "definition_hash": tool["definition_hash"],
                "risk_level": 0,
                "access": "read",
                "enabled": True,
            },
        )
    yield client, base
    await harness.close()


async def _call(live: tuple[httpx.AsyncClient, str], name: str, **arguments: Any) -> dict[str, Any]:
    client, base = live
    tools = (await client.get(f"{base}/tools")).json()
    tool = next(item for item in tools if item["name"] == name)
    response = await client.post(f"{base}/tools/{tool['id']}/invoke", json={"arguments": arguments})
    assert response.status_code == 200, response.text
    return response.json()


async def test_every_tool_reads_real_data(live: tuple[httpx.AsyncClient, str]) -> None:
    pulls = await _call(live, "pulls_list", repo=REPO, state="all")
    assert pulls["status"] == "succeeded", pulls
    listing = await _call(live, "file_get", repo=REPO, path="README.md")
    assert listing["status"] == "succeeded", listing
    assert listing["structured"]["sha"]

    search = await _call(live, "issues_search", query="is:pr", repo=REPO)
    assert search["status"] == "succeeded", search
    assert search["structured"]["excluded_outside_allowlist"] == 0

    if pulls["structured"]["items"]:
        number = pulls["structured"]["items"][0]["number"]
        pull = await _call(live, "pull_get", repo=REPO, number=number)
        assert pull["status"] == "succeeded", pull
        assert pull["structured"]["checks_available"] is True, "checks:read may be missing"
        # GitHub's issues API also serves pull requests.
        issue = await _call(live, "issue_get", repo=REPO, number=number)
        assert issue["status"] == "succeeded", issue


async def test_token_really_cannot_write_or_reach_other_repositories(tmp_path: Path) -> None:
    connection = GitHubConnection(
        connection_id="live",
        credential_id="live",
        app_id=APP_ID,
        installation_id=int(INSTALLATION),
        repositories=(REPO,),
        private_key=Path(KEY_FILE).read_text(encoding="utf-8"),
    )
    async with new_http_client(
        "https://api.github.com", timeout_seconds=30, transport=None
    ) as http:
        token = await InstallationTokens().get(http, connection)
        headers = {"Authorization": f"Bearer {token}"}

        visible = await http.get("/installation/repositories", headers=headers)
        assert visible.status_code == 200, visible.text
        names = [item["full_name"].lower() for item in visible.json()["repositories"]]
        assert names == [REPO.lower()]

        # Empty title: a token able to write gets 422 and creates nothing.
        write = await http.post(f"/repos/{REPO}/issues", headers=headers, json={"title": ""})
        assert write.status_code == 403, f"expected 403, got {write.status_code}"
