from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest

from opsassist.errors import PayloadTooLarge, RateLimited, ValidationFailed
from opsassist.identity.permissions import Permission, has_permission
from opsassist.identity.service import SlidingWindowLimiter
from opsassist.jobs.queue import backoff_seconds
from opsassist.knowledge.blobs import LocalBlobStore
from opsassist.knowledge.extraction import (
    CONTENT_MARKDOWN,
    CONTENT_PDF,
    ExtractionError,
    extract,
    validate_upload,
)
from opsassist.knowledge.retrieval import build_tsquery, is_identifier, query_terms
from opsassist.llm.anthropic_provider import AnthropicLLMProvider
from opsassist.llm.base import ProviderUnavailable
from opsassist.llm.hashing import HashEmbeddingProvider
from opsassist.models import EMBEDDING_DIMENSIONS, Role
from opsassist.observability import redact_secrets


def test_upload_accepts_markdown_and_sanitizes_name() -> None:
    upload = validate_upload("../../etc/run book.md", b"# Hi", max_bytes=100)
    assert upload.filename == "run book.md"
    assert upload.title == "run book"
    assert upload.content_type == CONTENT_MARKDOWN


@pytest.mark.parametrize(
    ("name", "data"),
    [
        ("malware.exe", b"MZ"),
        ("notes.pdf", b"not a pdf"),
        ("binary.txt", b"abc\x00def"),
        ("latin.txt", "café".encode("latin-1")),
        ("empty.md", b""),
        ("...", b"x"),
        ("double.md.exe", b"x"),
    ],
)
def test_upload_rejections(name: str, data: bytes) -> None:
    with pytest.raises(ValidationFailed):
        validate_upload(name, data, max_bytes=100)


def test_upload_size_limit() -> None:
    with pytest.raises(PayloadTooLarge):
        validate_upload("big.md", b"x" * 101, max_bytes=100)


def test_extract_limits_and_corrupt_pdf() -> None:
    with pytest.raises(ExtractionError):
        extract("text/plain", b"x" * 11, max_pages=1, max_chars=10)
    with pytest.raises(ExtractionError):
        extract(CONTENT_PDF, b"%PDF-1.7 garbage", max_pages=1, max_chars=10)
    assert extract("text/plain", b"a\r\nb", max_pages=1, max_chars=10).text == "a\nb"


async def test_blob_store_rejects_traversal(tmp_path: Any) -> None:
    store = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError, match="Invalid blob key"):
        await store.put("../../evil", b"x")


@pytest.mark.parametrize(
    "secret",
    [
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "AKIAABCDEFGHIJKLMNOP",
        "xoxb-1234567890-abcdefghij",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "postgresql://user:hunter2@db:5432/x",
    ],
)
def test_secret_redaction(secret: str) -> None:
    assert secret not in redact_secrets(f"value {secret} end")


def test_private_key_block_redacted() -> None:
    block = "-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----"
    assert "abc" not in redact_secrets(block)


def test_role_map_is_explicit() -> None:
    assert not has_permission(Role.VIEWER, Permission.DOCUMENTS_WRITE)
    assert has_permission(Role.MEMBER, Permission.DOCUMENTS_WRITE)
    assert not has_permission(Role.MEMBER, Permission.AUDIT_READ)
    assert has_permission(Role.ADMIN, Permission.AUDIT_READ)


def test_limiter_blocks_then_resets() -> None:
    limiter = SlidingWindowLimiter(2, 60)
    limiter.hit("k")
    limiter.check("k")
    limiter.hit("k")
    with pytest.raises(RateLimited) as info:
        limiter.check("k")
    assert info.value.retry_after_seconds > 0
    limiter.reset("k")
    limiter.check("k")


def test_backoff_grows_and_is_capped() -> None:
    assert backoff_seconds(1, 10) <= 10
    assert 40 <= backoff_seconds(4, 10) * 2 / 2 <= 80
    assert backoff_seconds(50, 10) <= 3600


def test_query_terms_drop_stopwords_and_keep_identifiers() -> None:
    assert query_terms("What is the owner of INC-1042?") == ["owner", "inc-1042"]
    assert is_identifier("inc-1042") and is_identifier("err_timeout") and not is_identifier("owner")


def test_tsquery_cannot_be_injected() -> None:
    assert build_tsquery(query_terms("a' OR 1=1; DROP TABLE chunks; --")) == (
        '"1" OR "1" OR "drop" OR "table" OR "chunks"'
    ).replace('"1" OR "1"', '"1"')


async def test_hash_embeddings_are_unit_vectors_of_right_size() -> None:
    provider = HashEmbeddingProvider()
    vector = await provider.embed_query("hello world")
    assert len(vector) == EMBEDDING_DIMENSIONS
    assert sum(v * v for v in vector) == pytest.approx(1.0)
    empty = await provider.embed_query("!!!")
    assert sum(v * v for v in empty) == pytest.approx(1.0)


class _FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.kwargs: dict[str, Any] = {}

    async def create(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _client(result: Any) -> Any:
    messages = _FakeMessages(result)
    return SimpleNamespace(messages=messages, beta=SimpleNamespace(messages=messages))


def _response(stop_reason: str = "end_turn", text: str = "Answer [S1].") -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(input_tokens=7, output_tokens=3),
    )


async def test_anthropic_adapter_shapes_request_and_reads_usage() -> None:
    client = _client(_response())
    provider = AnthropicLLMProvider("claude-opus-5-5", None, 30, client=client)
    result = await provider.generate(system="sys", user="usr", max_tokens=100)
    assert result.text == "Answer [S1]."
    assert (result.input_tokens, result.output_tokens) == (7, 3)
    sent = client.messages.kwargs
    assert sent["system"] == "sys"
    assert sent["messages"] == [{"role": "user", "content": "usr"}]
    assert sent["fallbacks"] == "default"
    assert "temperature" not in sent


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
async def test_anthropic_adapter_treats_incomplete_output_as_unavailable(stop_reason: str) -> None:
    provider = AnthropicLLMProvider(
        "claude-opus-5-5", None, 30, client=_client(_response(stop_reason))
    )
    with pytest.raises(ProviderUnavailable):
        await provider.generate(system="s", user="u", max_tokens=10)


async def test_anthropic_adapter_error_excludes_request_content() -> None:
    request = httpx.Request("POST", "https://example.test")
    error = anthropic.APIConnectionError(request=request)
    provider = AnthropicLLMProvider("claude-haiku-5-5", None, 30, client=_client(error))
    with pytest.raises(ProviderUnavailable) as info:
        await provider.generate(system="s", user="SECRET DOCUMENT", max_tokens=10)
    assert "SECRET DOCUMENT" not in str(info.value)
