"""Phase 1 acceptance: migrations, health, authentication, sessions, workspace boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from opsassist.api.app import create_app
from opsassist.container import build_container
from opsassist.db import global_scope, tenant_scope
from opsassist.identity.service import hash_token
from opsassist.models import AuditEvent, Role, Session, utcnow
from tests.conftest import BACKEND_DIR, ORIGIN, OWNER_URL, PASSWORD, Harness, make_settings


async def test_health_endpoints(harness: Harness) -> None:
    client = harness.client()
    assert (await client.get("/healthz")).json() == {"status": "ok"}
    assert (await client.get("/readyz")).status_code == 200


async def test_readyz_reports_database_outage(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        database_url="postgresql+asyncpg://nobody:wrong@localhost:1/none",
    )
    harness = Harness(build_container(settings))
    assert (await harness.client().get("/readyz")).status_code == 503
    await harness.close()


async def test_database_outage_returns_503_envelope(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, database_url="postgresql+asyncpg://nobody:wrong@localhost:1/none"
    )
    harness = Harness(build_container(settings))
    response = await harness.client().post(
        "/api/v1/auth/login", json={"email": "a@example.test", "password": "x"}
    )
    assert response.status_code == 503
    body = response.json()["error"]
    assert body["code"] == "service_unavailable"
    assert "wrong" not in response.text and "nobody" not in response.text
    await harness.close()


def test_migrations_have_no_drift(migrated_database: None) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "check"],
        cwd=BACKEND_DIR,
        env={**__import__("os").environ, "MIGRATION_DATABASE_URL": OWNER_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


async def test_login_sets_hardened_cookie_and_stores_only_hash(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = harness.client()
    response = await client.post(
        "/api/v1/auth/login", json={"email": tenant.email.upper(), "password": PASSWORD}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    token = client.cookies[harness.container.settings.cookie_name]
    async with global_scope(harness.container.session_factory) as session:
        stored = await session.scalar(select(Session.token_hash))
    assert stored == hash_token(token)
    assert stored != token


async def test_secure_cookie_flag_follows_settings(harness: Harness) -> None:
    tenant = await harness.tenant()
    harness.container.settings.cookie_secure = True
    client = harness.client()
    response = await client.post(
        "/api/v1/auth/login", json={"email": tenant.email, "password": PASSWORD}
    )
    assert "secure" in response.headers["set-cookie"].lower()


async def test_login_failure_is_uniform_and_audited(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = harness.client()
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": tenant.email, "password": "not-the-password"}
    )
    missing = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.test", "password": "not-the-password"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]
    # Audit rows with no workspace are invisible to the app role, so read as owner.
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(OWNER_URL)
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(AuditEvent.outcome).where(AuditEvent.action == "auth.login")
        )
        assert [row[0] for row in rows] == ["failure", "failure"]
    await engine.dispose()


async def test_login_is_throttled_with_retry_after(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = harness.client()
    for _ in range(5):
        await client.post(
            "/api/v1/auth/login", json={"email": tenant.email, "password": "not-the-password"}
        )
    response = await client.post(
        "/api/v1/auth/login", json={"email": tenant.email, "password": PASSWORD}
    )
    assert response.status_code == 429
    assert int(response.headers["retry-after"]) > 0


async def test_validation_error_does_not_echo_password(harness: Harness) -> None:
    response = await harness.client().post(
        "/api/v1/auth/login",
        json={"email": "a@example.test", "password": "hunter2-secret", "extra": "x"},
    )
    assert response.status_code == 422
    assert "hunter2-secret" not in response.text
    assert response.json()["error"]["code"] == "validation_failed"


async def test_unauthenticated_requests_are_rejected(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = harness.client()
    assert (await client.get("/api/v1/me")).status_code == 401
    response = await client.get(f"/api/v1/workspaces/{tenant.workspace_id}/members")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "authentication_required"
    assert response.json()["error"]["request_id"]


async def test_logout_revokes_session_server_side(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    token = client.cookies[harness.container.settings.cookie_name]
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    replay = harness.client()
    replay.cookies.set(harness.container.settings.cookie_name, token)
    assert (await replay.get("/api/v1/me")).status_code == 401


@pytest.mark.parametrize("field", ["expires_at", "last_seen_at"])
async def test_expired_and_idle_sessions_are_rejected(harness: Harness, field: str) -> None:
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    assert (await client.get("/api/v1/me")).status_code == 200
    past = utcnow() - timedelta(days=30)
    async with global_scope(harness.container.session_factory) as session:
        await session.execute(update(Session).values({field: past}))
    assert (await client.get("/api/v1/me")).status_code == 401


async def test_me_lists_only_own_workspaces(harness: Harness) -> None:
    first = await harness.tenant()
    second = await harness.tenant()
    client = await harness.login(first.email)
    workspaces = (await client.get("/api/v1/me")).json()["workspaces"]
    assert [w["id"] for w in workspaces] == [str(first.workspace_id)]
    assert str(second.workspace_id) not in json.dumps(workspaces)


async def test_non_member_gets_404_not_403(harness: Harness) -> None:
    first = await harness.tenant()
    second = await harness.tenant()
    client = await harness.login(first.email)
    response = await client.get(f"/api/v1/workspaces/{second.workspace_id}/members")
    assert response.status_code == 404


async def test_cross_site_post_is_blocked(harness: Harness) -> None:
    tenant = await harness.tenant()
    client = await harness.login(tenant.email)
    response = await client.post(
        f"/api/v1/workspaces/{tenant.workspace_id}/groups",
        json={"name": "x"},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    allowed = await client.post(
        f"/api/v1/workspaces/{tenant.workspace_id}/groups",
        json={"name": "x"},
        headers={"Origin": ORIGIN},
    )
    assert allowed.status_code == 201


async def test_role_permissions_are_enforced(harness: Harness) -> None:
    owner = await harness.tenant()
    viewer = await harness.member_of(owner.workspace_id, Role.VIEWER)
    member = await harness.member_of(owner.workspace_id, Role.MEMBER)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    viewer_client = await harness.login(viewer.email)
    member_client = await harness.login(member.email)

    denied = await viewer_client.post(f"{base}/sources", json={"name": "s"})
    assert denied.status_code == 403
    assert (await member_client.post(f"{base}/sources", json={"name": "s"})).status_code == 201
    assert (await member_client.get(f"{base}/audit")).status_code == 403
    assert (await member_client.post(f"{base}/groups", json={"name": "g"})).status_code == 403


async def test_admin_cannot_escalate_or_remove_owner(harness: Harness) -> None:
    owner = await harness.tenant()
    admin = await harness.member_of(owner.workspace_id, Role.ADMIN)
    other_id, other_email = await harness.create_user()
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    client = await harness.login(admin.email)
    escalate = await client.post(f"{base}/members", json={"email": other_email, "role": "owner"})
    assert escalate.status_code == 403
    assert (await client.delete(f"{base}/members/{owner.user_id}")).status_code == 403
    assert other_id


async def test_last_owner_cannot_be_removed(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    response = await client.delete(
        f"/api/v1/workspaces/{owner.workspace_id}/members/{owner.user_id}"
    )
    assert response.status_code == 409


async def test_member_changes_are_audited(harness: Harness) -> None:
    owner = await harness.tenant()
    _, email = await harness.create_user()
    client = await harness.login(owner.email)
    base = f"/api/v1/workspaces/{owner.workspace_id}"
    created = await client.post(f"{base}/members", json={"email": email, "role": "member"})
    assert created.status_code == 201
    events = (await client.get(f"{base}/audit")).json()
    assert events[0]["action"] == "workspace.member_added"
    assert events[0]["actor_user_id"] == str(owner.user_id)
    assert events[0]["request_id"]


async def test_app_role_cannot_rewrite_audit(harness: Harness) -> None:
    owner = await harness.tenant()
    client = await harness.login(owner.email)
    await client.post(f"/api/v1/workspaces/{owner.workspace_id}/groups", json={"name": "g"})
    from sqlalchemy.exc import DBAPIError

    with pytest.raises(DBAPIError, match="permission denied"):
        async with tenant_scope(harness.container.session_factory, owner.workspace_id) as session:
            await session.execute(update(AuditEvent).values(outcome="tampered"))


def test_external_provider_requires_explicit_approval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="EXTERNAL_MODEL_DATA_APPROVED"):
        make_settings(tmp_path, llm_provider="anthropic")
    with pytest.raises(ValueError, match="EXTERNAL_MODEL_DATA_APPROVED"):
        make_settings(tmp_path, embedding_provider="ollama", ollama_url="http://gpu.corp:11434")
    assert make_settings(tmp_path, llm_provider="anthropic", external_model_data_approved=True)


def test_production_requires_secure_cookie(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="COOKIE_SECURE"):
        make_settings(tmp_path, environment="production", cookie_secure=False)


def test_openapi_disabled_in_production(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, environment="production", cookie_secure=True)
    app = create_app(container=build_container(settings))
    assert app.openapi_url is None and app.docs_url is None
