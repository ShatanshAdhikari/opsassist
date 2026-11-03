"""Anthropic adapter. External: only constructed after the data-egress approval check.

Status: tested against a fake client only. No API key was available when this was written, so
it has never run against the live API.
"""

from __future__ import annotations

from typing import Any

from opsassist.llm.base import GenerationResult, ProviderUnavailable

# Server-side refusal fallback is accepted on these models, using the "default" routing form.
_FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicLLMProvider:
    name = "anthropic"

    def __init__(self, model: str, api_key: str | None, timeout: float, client: Any = None) -> None:
        self.model = model
        if client is None:
            import anthropic  # optional dependency: `uv sync --extra anthropic`

            client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout)
        self._client = client

    async def generate(self, *, system: str, user: str, max_tokens: int) -> GenerationResult:
        import anthropic

        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            if self.model in _FALLBACK_MODELS:
                response = await self._client.beta.messages.create(
                    betas=[_FALLBACK_BETA], fallbacks="default", **request
                )
            else:
                response = await self._client.messages.create(**request)
        except anthropic.APIStatusError as exc:
            raise ProviderUnavailable(f"Anthropic API error: status {exc.status_code}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderUnavailable(f"Anthropic connection error: {type(exc).__name__}") from exc

        if response.stop_reason == "refusal":
            raise ProviderUnavailable("Anthropic declined the request")
        if response.stop_reason == "max_tokens":
            raise ProviderUnavailable("Anthropic response was cut off at the output limit")
        text = "".join(block.text for block in response.content if block.type == "text")
        if not text.strip():
            raise ProviderUnavailable("Anthropic returned no text")
        return GenerationResult(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
