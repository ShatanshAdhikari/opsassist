"""Operator commands: worker, bootstrap and user management."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import os
import signal
import uuid

from sqlalchemy import func, select

from opsassist.audit import record_event
from opsassist.config import get_settings
from opsassist.container import build_container
from opsassist.credentials.crypto import generate_key
from opsassist.db import SessionFactory, global_scope, tenant_scope
from opsassist.identity.service import hash_password
from opsassist.jobs.worker import JobHandler, Worker
from opsassist.knowledge.ingestion import INGEST_JOB_KIND, IngestHandler
from opsassist.models import Membership, Organization, Role, User, Workspace
from opsassist.observability import configure_logging


def _read_password(env_var: str | None) -> str:
    """Never accept a password as a command argument: arguments leak into process lists."""
    if env_var:
        value = os.environ.get(env_var)
        if not value:
            raise SystemExit(f"Environment variable {env_var} is empty")
        return value
    return getpass.getpass("Password: ")


async def _create_user(factory: SessionFactory, email: str, name: str, password: str) -> uuid.UUID:
    async with global_scope(factory) as session:
        if await session.scalar(select(User).where(func.lower(User.email) == email.lower())):
            raise SystemExit(f"User already exists: {email}")
        user = User(email=email, display_name=name, password_hash=hash_password(password))
        session.add(user)
        await session.flush()
        record_event(session, "user.created", target_type="user", target_id=user.id)
        return user.id


async def _bootstrap(args: argparse.Namespace) -> None:
    container = build_container(get_settings())
    try:
        password = _read_password(args.password_env)
        user_id = await _create_user(container.session_factory, args.email, args.name, password)
        async with global_scope(container.session_factory) as session:
            organization = Organization(name=args.org, slug=args.org_slug)
            session.add(organization)
            await session.flush()
            workspace = Workspace(
                organization_id=organization.id, name=args.workspace, slug=args.workspace_slug
            )
            session.add(workspace)
            await session.flush()
            session.add(Membership(workspace_id=workspace.id, user_id=user_id, role=Role.OWNER))
            workspace_id = workspace.id
        print(f"Created organization, workspace {workspace_id} and owner {args.email}")
    finally:
        await container.close()


async def _create_user_command(args: argparse.Namespace) -> None:
    container = build_container(get_settings())
    try:
        password = _read_password(args.password_env)
        user_id = await _create_user(container.session_factory, args.email, args.name, password)
        print(f"Created user {user_id}")
    finally:
        await container.close()


async def _rotate_credential_keys() -> None:
    """Idempotent. One transaction per workspace, so a failure leaves the rest consistent."""
    container = build_container(get_settings())
    try:
        factory = container.session_factory
        async with global_scope(factory) as session:
            workspace_ids = list(await session.scalars(select(Workspace.id)))
        total = 0
        for workspace_id in workspace_ids:
            async with tenant_scope(factory, workspace_id) as session:
                moved = await container.credentials.rotate_workspace(session)
                if moved:
                    record_event(
                        session,
                        "credential.rotate_keys",
                        workspace_id=workspace_id,
                        metadata={"moved": moved},
                    )
                total += moved
        print(f"Re-wrapped {total} credentials. Remove an old key only once this reports 0.")
    finally:
        await container.close()


async def _run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    container = build_container(settings)
    handlers: dict[str, JobHandler] = {
        INGEST_JOB_KIND: IngestHandler(
            settings, container.session_factory, container.blobs, container.embedder
        )
    }
    worker = Worker(settings, container.session_factory, handlers)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        # Windows has no add_signal_handler. Ctrl+C then raises KeyboardInterrupt instead.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(signum, stop.set)
    try:
        await worker.run_forever(stop)
    finally:
        await container.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="opsassist")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("worker", help="Run the background job worker")

    bootstrap = commands.add_parser("bootstrap", help="Create first org, workspace and owner")
    bootstrap.add_argument("--org", required=True)
    bootstrap.add_argument("--org-slug", required=True)
    bootstrap.add_argument("--workspace", required=True)
    bootstrap.add_argument("--workspace-slug", required=True)
    bootstrap.add_argument("--email", required=True)
    bootstrap.add_argument("--name", required=True)
    bootstrap.add_argument("--password-env", help="Environment variable holding the password")

    create_user = commands.add_parser("create-user", help="Create a user account")
    create_user.add_argument("--email", required=True)
    create_user.add_argument("--name", required=True)
    create_user.add_argument("--password-env", help="Environment variable holding the password")

    commands.add_parser("generate-credential-key", help="Print a new base64 encryption key")
    commands.add_parser(
        "rotate-credential-keys", help="Re-wrap stored credentials under the active key"
    )

    args = parser.parse_args()
    if args.command == "worker":
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(_run_worker())
    elif args.command == "bootstrap":
        asyncio.run(_bootstrap(args))
    elif args.command == "create-user":
        asyncio.run(_create_user_command(args))
    elif args.command == "generate-credential-key":
        print(generate_key())
    elif args.command == "rotate-credential-keys":
        asyncio.run(_rotate_credential_keys())


if __name__ == "__main__":
    main()
