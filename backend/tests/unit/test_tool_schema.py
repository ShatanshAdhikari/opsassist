"""Definition checks, argument validation, URL policy, retry and circuit breaking."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from opsassist.db import connect_or_unavailable
from opsassist.errors import ValidationFailed
from opsassist.tools.base import (
    ProviderProtocolError,
    ProviderTimeout,
    ProviderUnreachable,
    ToolDescriptor,
)
from opsassist.tools.reliability import CircuitBreaker, call_with_retry
from opsassist.tools.schema import InvalidDefinition, check_definition, validate_arguments
from opsassist.tools.url_policy import BlockedUrl, check_server_url

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"repo": {"type": "string"}, "limit": {"type": "integer", "maximum": 50}},
    "required": ["repo"],
}


def _descriptor(**overrides: Any) -> ToolDescriptor:
    values: dict[str, Any] = {"name": "search", "description": "Search", "input_schema": SCHEMA}
    values.update(overrides)
    return ToolDescriptor(**values)


def test_definition_hash_changes_with_any_part_and_ignores_key_order() -> None:
    base = _descriptor()
    reordered = _descriptor(input_schema=dict(reversed(list(SCHEMA.items()))))
    assert base.definition_hash == reordered.definition_hash
    assert base.definition_hash != _descriptor(description="Search now").definition_hash
    assert base.definition_hash != _descriptor(annotations={"x": 1}).definition_hash
    assert base.definition_hash != _descriptor(input_schema={"type": "object"}).definition_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "bad name!"},
        {"name": ""},
        {"description": "x" * 5000},
        {"input_schema": {"type": "string"}},
        {"input_schema": {"type": "object", "properties": {"a": {"type": "nonsense"}}}},
        {"input_schema": {"type": "object", "$ref": "http://169.254.169.254/latest"}},
        {"input_schema": {"type": "object", "properties": {"a": {"$ref": "https://evil.test/x"}}}},
        {"input_schema": {"type": "object", "description": "x" * 2000}},
    ],
)
def test_bad_definitions_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(InvalidDefinition):
        check_definition(_descriptor(**overrides), max_schema_bytes=1024)


def test_local_reference_is_allowed() -> None:
    schema = {
        "type": "object",
        "$defs": {"name": {"type": "string"}},
        "properties": {"a": {"$ref": "#/$defs/name"}},
    }
    check_definition(_descriptor(input_schema=schema), max_schema_bytes=1024)
    validate_arguments(schema, {"a": "ok"}, max_bytes=1024)
    with pytest.raises(ValidationFailed):
        validate_arguments(schema, {"a": 1}, max_bytes=1024)


def test_valid_arguments_pass() -> None:
    validate_arguments(SCHEMA, {"repo": "a/b", "limit": 5}, max_bytes=1024)


@pytest.mark.parametrize(
    "arguments",
    [{}, {"repo": 1}, {"repo": "a", "limit": 500}, {"repo": "a", "unexpected": True}],
)
def test_invalid_arguments_are_rejected(arguments: dict[str, Any]) -> None:
    with pytest.raises(ValidationFailed):
        validate_arguments(SCHEMA, arguments, max_bytes=1024)


def test_server_cannot_widen_arguments_with_additional_properties_true() -> None:
    schema = {**SCHEMA, "additionalProperties": True}
    with pytest.raises(ValidationFailed):
        validate_arguments(schema, {"repo": "a", "unexpected": True}, max_bytes=1024)


def test_validation_error_does_not_echo_the_value() -> None:
    with pytest.raises(ValidationFailed) as caught:
        validate_arguments(SCHEMA, {"repo": 1, "limit": "SENSITIVEVALUE"}, max_bytes=1024)
    assert "SENSITIVEVALUE" not in str(caught.value.details)
    assert "SENSITIVEVALUE" not in caught.value.message


def test_oversized_arguments_are_rejected() -> None:
    with pytest.raises(ValidationFailed):
        validate_arguments(SCHEMA, {"repo": "x" * 2000}, max_bytes=1024)


def test_remote_reference_in_a_stored_schema_is_never_fetched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth, for a schema that somehow bypassed the discovery check."""

    def no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("schema validation attempted network access")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    schema = {"type": "object", "properties": {"a": {"$ref": "http://169.254.169.254/x"}}}
    with pytest.raises(ValidationFailed):
        validate_arguments(schema, {"a": 1}, max_bytes=1024)


# --- URL policy -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/mcp",
        "file:///etc/passwd",
        "https://",
        "https://user:pass@example.com/mcp",
        "https://example.com/mcp?token=abc",
        "http://8.8.8.8/mcp",
        "https://localhost/mcp",
        "https://127.0.0.1/mcp",
        "https://10.0.0.5/mcp",
        "https://192.168.1.1/mcp",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/mcp",
        "https://[::ffff:127.0.0.1]/mcp",
        "https://2130706433/mcp",
    ],
)
async def test_unsafe_urls_are_blocked(url: str) -> None:
    with pytest.raises(BlockedUrl):
        await check_server_url(url, allowed_private_hosts=frozenset())


async def test_public_https_passes() -> None:
    await check_server_url("https://8.8.8.8/mcp", allowed_private_hosts=frozenset())


async def test_name_with_any_private_answer_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    def answers(_host: str, port: int, *_args: object, **_kw: object) -> list[Any]:
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", port)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", answers)
    with pytest.raises(BlockedUrl):
        await check_server_url("https://mixed.example/mcp", allowed_private_hosts=frozenset())


async def test_allowlisted_private_host_passes_only_when_listed() -> None:
    allowed = frozenset({"127.0.0.1"})
    await check_server_url("http://127.0.0.1:9000/mcp", allowed_private_hosts=allowed)
    with pytest.raises(BlockedUrl):
        await check_server_url("http://127.0.0.2:9000/mcp", allowed_private_hosts=allowed)


# --- Retry and breaker ------------------------------------------------------------------


async def test_retry_recovers_from_transient_failure() -> None:
    calls = 0

    async def call(_remaining: float) -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ProviderUnreachable()
        return "ok"

    outcome = await call_with_retry(call, budget_seconds=5, retries=2, base_delay_seconds=0)
    assert outcome.value == "ok"
    assert outcome.attempts == 3


async def test_retry_stops_at_limit_and_on_non_retryable() -> None:
    calls = 0

    async def unreachable(_remaining: float) -> str:
        nonlocal calls
        calls += 1
        raise ProviderUnreachable()

    with pytest.raises(ProviderUnreachable):
        await call_with_retry(unreachable, budget_seconds=5, retries=2, base_delay_seconds=0)
    assert calls == 3

    calls = 0

    async def protocol(_remaining: float) -> str:
        nonlocal calls
        calls += 1
        raise ProviderProtocolError()

    with pytest.raises(ProviderProtocolError):
        await call_with_retry(protocol, budget_seconds=5, retries=2, base_delay_seconds=0)
    assert calls == 1


async def test_retry_passes_shrinking_budget_and_stops_when_spent() -> None:
    seen: list[float] = []

    async def call(remaining: float) -> str:
        seen.append(remaining)
        raise ProviderTimeout()

    with pytest.raises(ProviderTimeout):
        await call_with_retry(call, budget_seconds=0.05, retries=50, base_delay_seconds=0.02)
    assert seen[0] <= 0.05
    assert len(seen) < 51
    assert seen == sorted(seen, reverse=True)


def test_breaker_opens_then_lets_one_probe_through() -> None:
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=0)
    assert breaker.allow("s")
    breaker.record_failure("s")
    assert breaker.allow("s")
    breaker.record_failure("s")

    held = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    held.record_failure("s")
    assert not held.allow("s")
    assert held.allow("other")
    held.record_success("s")
    assert held.allow("s")


def test_any_socket_failure_connecting_to_the_database_becomes_connection_error() -> None:
    """Regression: on Linux asyncio raises plain OSError for a host with several addresses."""

    class Dialect:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> None:
            raise OSError("Multiple exceptions: [Errno 111], [Errno 111]")

    with pytest.raises(ConnectionError):
        connect_or_unavailable(Dialect(), None, (), {})  # type: ignore[arg-type]
