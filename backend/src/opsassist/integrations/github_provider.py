"""Read-only GitHub tools, as a native provider behind the tool contract.

Native rather than MCP because the access rules need enforcing here: the repository allowlist,
down-scoped tokens, and bounded output. Tools still go through the same discovery, admin review,
pinning, validation and logging as any MCP tool. A code change to a definition below changes its
hash, so it needs a new review.

GitHub content is written by anyone who can open an issue. It is returned as untrusted data.
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
from typing import Any

import httpx

from opsassist.config import Settings
from opsassist.credentials.crypto import CredentialDecryptError
from opsassist.credentials.store import CredentialStore
from opsassist.db import SessionFactory, tenant_scope
from opsassist.integrations.github_client import (
    GitHubApi,
    GitHubConnection,
    GitHubNotFound,
    GitHubRejected,
    InstallationTokens,
    encode_path,
    new_http_client,
)
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

_MAX_BODY_CHARS = 20_000
_MAX_PATCH_CHARS = 4_000
_MAX_COMMENTS = 30
_MAX_FILES = 100
_MAX_FILE_BYTES = 1_000_000
_MAX_QUERY_CHARS = 256
# These widen a search beyond the allowlist. Tokens are scoped as well, so this is the outer layer.
_SCOPE_QUALIFIER = re.compile(r"(?i)(?:^|[\s\-(])(?:repo|org|user|owner):")
_REF = re.compile(r"^[A-Za-z0-9_./\-]{1,200}$")

_REPO_SCHEMA = {
    "type": "string",
    # GitHub owners cannot start with a dot. Repositories can: ".github" is common.
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9\-]{0,99}/[A-Za-z0-9_.\-]{1,100}$",
    "description": "Repository as owner/name",
}
_READ_ONLY = {"read_only_hint": True, "destructive_hint": False, "open_world_hint": True}


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        annotations=_READ_ONLY,
    )


TOOLS: dict[str, ToolDescriptor] = {
    tool.name: tool
    for tool in (
        _tool(
            "issues_search",
            "Search issues and pull requests in the connection's repositories. Live data.",
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 200},
                "repo": _REPO_SCHEMA,
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["query"],
        ),
        _tool(
            "issue_get",
            "Get one issue with its comments. Live data.",
            {"repo": _REPO_SCHEMA, "number": {"type": "integer", "minimum": 1}},
            ["repo", "number"],
        ),
        _tool(
            "pulls_list",
            "List pull requests in a repository, most recently updated first. Live data.",
            {
                "repo": _REPO_SCHEMA,
                "state": {"enum": ["open", "closed", "all"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            },
            ["repo"],
        ),
        _tool(
            "pull_get",
            "Get one pull request with changed files, reviews and check status. Live data.",
            {"repo": _REPO_SCHEMA, "number": {"type": "integer", "minimum": 1}},
            ["repo", "number"],
        ),
        _tool(
            "file_get",
            "Read a text file, or list a directory, at a branch, tag or commit. Live data.",
            {
                "repo": _REPO_SCHEMA,
                "path": {"type": "string", "minLength": 1, "maxLength": 1000},
                "ref": {"type": "string", "minLength": 1, "maxLength": 200},
            },
            ["repo", "path"],
        ),
    )
}


class _ToolRefusal(Exception):
    """A tool-level answer written by this module, safe to return to the caller."""


@dataclass(frozen=True)
class GitHubDeps:
    settings: Settings
    session_factory: SessionFactory
    credentials: CredentialStore
    tokens: InstallationTokens
    transport: httpx.AsyncBaseTransport | None = None


async def load_connection(
    deps: GitHubDeps, workspace_id: uuid.UUID, connection_id: uuid.UUID
) -> GitHubConnection:
    """Read settings and decrypt the key for one operation. A deleted connection fails here."""
    async with tenant_scope(deps.session_factory, workspace_id) as session:
        row = await session.get(IntegrationConnection, connection_id)
        if row is None or row.credential_id is None:
            raise CredentialRejected()
        try:
            secret = await deps.credentials.get(session, row.credential_id)
        except CredentialDecryptError as exc:
            raise CredentialRejected() from exc
        return GitHubConnection(
            connection_id=row.id,
            credential_id=row.credential_id,
            app_id=str(row.config["app_id"]),
            installation_id=int(row.config["installation_id"]),
            repositories=tuple(row.config["repositories"]),
            private_key=str(secret["private_key"]),
        )


def _login(user: Any) -> str | None:
    return user.get("login") if isinstance(user, dict) else None


def _clip(text: Any, limit: int) -> str:
    value = text if isinstance(text, str) else ""
    return value if len(value) <= limit else f"{value[:limit]}\n[truncated]"


def _issue_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "is_pull_request": "pull_request" in item,
        "author": _login(item.get("user")),
        "labels": [label.get("name") for label in item.get("labels", [])],
        "assignees": [_login(user) for user in item.get("assignees", [])],
        "milestone": (item.get("milestone") or {}).get("title"),
        "comments": item.get("comments"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "closed_at": item.get("closed_at"),
        "url": item.get("html_url"),
    }


def _pull_summary(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "number": item.get("number"),
        "title": item.get("title"),
        "state": item.get("state"),
        "draft": item.get("draft"),
        "author": _login(item.get("user")),
        "head": (item.get("head") or {}).get("ref"),
        "base": (item.get("base") or {}).get("ref"),
        "labels": [label.get("name") for label in item.get("labels", [])],
        "requested_reviewers": [_login(user) for user in item.get("requested_reviewers", [])],
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "merged_at": item.get("merged_at"),
        "url": item.get("html_url"),
    }


def _output(summary: str, data: dict[str, Any], *, max_chars: int) -> ToolOutput:
    redacted = redact_tree(data)
    size = len(json.dumps(redacted))
    if size <= max_chars:
        return ToolOutput(text=redact_secrets(summary), structured=redacted, is_error=False)
    # Too large to return whole. Cut text is still readable, and the flag says it is incomplete.
    return ToolOutput(
        text=json.dumps(redacted)[:max_chars], structured=None, is_error=False, truncated=True
    )


class GitHubToolProvider:
    def __init__(
        self,
        deps: GitHubDeps,
        workspace_id: uuid.UUID,
        connection_id: uuid.UUID,
    ) -> None:
        self._deps = deps
        self._workspace_id = workspace_id
        self._connection_id = connection_id

    async def discover(self, *, timeout_seconds: float) -> list[ToolDescriptor]:
        await self.verify(timeout_seconds=timeout_seconds)
        return list(TOOLS.values())

    async def verify(self, *, timeout_seconds: float) -> None:
        """Prove the key, app, installation and repository list work, by getting a token."""
        await self._with_api(lambda api, _connection: api.check_authentication(), timeout_seconds)

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

        async def run(api: GitHubApi, connection: GitHubConnection) -> ToolOutput:
            try:
                return await self._dispatch(name, api, connection, arguments)
            except _ToolRefusal as exc:
                return ToolOutput(text=str(exc), structured=None, is_error=True)
            except GitHubNotFound:
                return ToolOutput(
                    text="GitHub reports this as not found, or not visible to this connection.",
                    structured=None,
                    is_error=True,
                )
            except GitHubRejected:
                return ToolOutput(
                    text="GitHub could not process this request.", structured=None, is_error=True
                )

        return await self._with_api(run, timeout_seconds)

    async def _with_api[T](
        self,
        operation: Callable[[GitHubApi, GitHubConnection], Awaitable[T]],
        timeout_seconds: float,
    ) -> T:
        settings = self._deps.settings
        connection = await load_connection(self._deps, self._workspace_id, self._connection_id)
        try:
            async with (
                asyncio.timeout(timeout_seconds),
                new_http_client(
                    settings.github_api_url,
                    timeout_seconds=timeout_seconds,
                    transport=self._deps.transport,
                ) as http,
            ):
                api = GitHubApi(
                    http,
                    self._deps.tokens,
                    connection,
                    max_response_bytes=settings.github_max_response_bytes,
                )
                return await operation(api, connection)
        except TimeoutError as exc:
            raise ProviderTimeout() from exc

    async def _dispatch(
        self,
        name: str,
        api: GitHubApi,
        connection: GitHubConnection,
        arguments: dict[str, Any],
    ) -> ToolOutput:
        handlers = {
            "issues_search": self._issues_search,
            "issue_get": self._issue_get,
            "pulls_list": self._pulls_list,
            "pull_get": self._pull_get,
            "file_get": self._file_get,
        }
        return await handlers[name](api, connection, arguments)

    def _finish(self, summary: str, data: dict[str, Any]) -> ToolOutput:
        return _output(summary, data, max_chars=self._deps.settings.tool_max_output_chars)

    @staticmethod
    def _allowed_repo(connection: GitHubConnection, repo: str) -> str:
        """Return the allowlisted spelling. GitHub treats names case-insensitively."""
        owner, _, name = repo.partition("/")
        if owner in {".", ".."} or name in {".", ".."}:
            raise _ToolRefusal("This repository name is not valid.")
        for allowed in connection.repositories:
            if allowed.lower() == repo.lower():
                return allowed
        raise _ToolRefusal("This repository is not enabled for this connection.")

    async def _issues_search(
        self, api: GitHubApi, connection: GitHubConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        query = arguments["query"]
        if _SCOPE_QUALIFIER.search(query):
            raise _ToolRefusal(
                "Remove repo:, org:, user: and owner: from the query. Use the repo argument."
            )
        repos = (
            [self._allowed_repo(connection, arguments["repo"])]
            if "repo" in arguments
            else list(connection.repositories)
        )
        full_query = " ".join([query, *(f"repo:{repo}" for repo in repos)])
        if len(full_query) > _MAX_QUERY_CHARS:
            raise _ToolRefusal("This search is too long for GitHub. Name one repository.")
        limit = arguments.get("limit", 20)
        body = await api.get("/search/issues", {"q": full_query, "per_page": limit})
        allowed = {repo.lower() for repo in connection.repositories}
        results = []
        excluded = 0
        for item in body.get("items", []):
            # repository_url ends in /repos/owner/name.
            repo = "/".join(str(item.get("repository_url", "")).rsplit("/", 2)[-2:])
            if repo.lower() not in allowed:
                excluded += 1
                continue
            results.append({"repo": repo, **_issue_summary(item)})
        data = {
            "total_count": body.get("total_count"),
            "incomplete_results": body.get("incomplete_results"),
            "items": results,
            "excluded_outside_allowlist": excluded,
        }
        return self._finish(f"{len(results)} results returned.", data)

    async def _issue_get(
        self, api: GitHubApi, connection: GitHubConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        repo = self._allowed_repo(connection, arguments["repo"])
        number = arguments["number"]
        issue = await api.get(f"/repos/{repo}/issues/{number}")
        comments = await api.get(
            f"/repos/{repo}/issues/{number}/comments", {"per_page": _MAX_COMMENTS}
        )
        data = {
            "repo": repo,
            **_issue_summary(issue),
            "body": _clip(issue.get("body"), _MAX_BODY_CHARS),
            "comments_returned": [
                {
                    "author": _login(comment.get("user")),
                    "created_at": comment.get("created_at"),
                    "url": comment.get("html_url"),
                    "body": _clip(comment.get("body"), _MAX_BODY_CHARS),
                }
                for comment in comments
            ],
        }
        return self._finish(f"{repo}#{number}: {issue.get('title')}", data)

    async def _pulls_list(
        self, api: GitHubApi, connection: GitHubConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        repo = self._allowed_repo(connection, arguments["repo"])
        pulls = await api.get(
            f"/repos/{repo}/pulls",
            {
                "state": arguments.get("state", "open"),
                "per_page": arguments.get("limit", 20),
                "sort": "updated",
                "direction": "desc",
            },
        )
        data = {"repo": repo, "items": [_pull_summary(item) for item in pulls]}
        return self._finish(f"{len(pulls)} pull requests returned.", data)

    async def _pull_get(
        self, api: GitHubApi, connection: GitHubConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        repo = self._allowed_repo(connection, arguments["repo"])
        number = arguments["number"]
        pull = await api.get(f"/repos/{repo}/pulls/{number}")
        files = await api.get(f"/repos/{repo}/pulls/{number}/files", {"per_page": _MAX_FILES})
        reviews = await api.get(f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 50})
        data: dict[str, Any] = {
            "repo": repo,
            **_pull_summary(pull),
            "body": _clip(pull.get("body"), _MAX_BODY_CHARS),
            "mergeable_state": pull.get("mergeable_state"),
            "commits": pull.get("commits"),
            "additions": pull.get("additions"),
            "deletions": pull.get("deletions"),
            "changed_files": pull.get("changed_files"),
            "files": [
                {
                    "path": item.get("filename"),
                    "status": item.get("status"),
                    "additions": item.get("additions"),
                    "deletions": item.get("deletions"),
                    "patch": _clip(item.get("patch"), _MAX_PATCH_CHARS),
                }
                for item in files
            ],
            "files_complete": len(files) >= (pull.get("changed_files") or 0),
            "reviews": [
                {
                    "author": _login(review.get("user")),
                    "state": review.get("state"),
                    "submitted_at": review.get("submitted_at"),
                    "body": _clip(review.get("body"), _MAX_BODY_CHARS),
                }
                for review in reviews
            ],
        }
        sha = (pull.get("head") or {}).get("sha")
        data["head_sha"] = sha
        # Report checks as unknown when unreadable. Absent data must not read as "no failures".
        data["checks_available"] = False
        data["checks"] = []
        if sha:
            try:
                checks = await api.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 50})
            except GitHubNotFound:
                pass
            else:
                data["checks_available"] = True
                data["checks"] = [
                    {
                        "name": run.get("name"),
                        "status": run.get("status"),
                        "conclusion": run.get("conclusion"),
                        "url": run.get("html_url"),
                    }
                    for run in checks.get("check_runs", [])
                ]
        return self._finish(f"{repo}#{number}: {pull.get('title')}", data)

    async def _file_get(
        self, api: GitHubApi, connection: GitHubConnection, arguments: dict[str, Any]
    ) -> ToolOutput:
        repo = self._allowed_repo(connection, arguments["repo"])
        path = arguments["path"].strip("/")
        if any(part in {"", ".", ".."} for part in path.split("/")):
            raise _ToolRefusal("This path is not valid.")
        params: dict[str, Any] = {}
        if ref := arguments.get("ref"):
            if not _REF.fullmatch(ref) or ".." in ref:
                raise _ToolRefusal("This ref is not valid.")
            params["ref"] = ref
        body = await api.get(f"/repos/{repo}/contents/{encode_path(path)}", params)
        if isinstance(body, list):
            entries = [
                {"name": item.get("name"), "type": item.get("type"), "path": item.get("path")}
                for item in body
            ]
            return self._finish(
                f"{len(entries)} entries in {path}.",
                {"repo": repo, "path": path, "ref": ref, "entries": entries},
            )
        if body.get("type") != "file":
            raise _ToolRefusal("This path is not a regular file.")
        size = body.get("size") or 0
        if size > _MAX_FILE_BYTES or body.get("encoding") != "base64":
            raise _ToolRefusal("This file is too large to read through this tool.")
        try:
            content = base64.b64decode(body.get("content", "")).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise _ToolRefusal("This file is not text.") from exc
        data = {
            "repo": repo,
            "path": body.get("path"),
            "ref": ref,
            "sha": body.get("sha"),
            "size": size,
            "url": body.get("html_url"),
            "content": content,
        }
        return self._finish(f"{repo}/{path}, {size} bytes.", data)
