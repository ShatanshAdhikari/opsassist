"""Validated application settings. All configuration enters through here."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProviderName = Literal["extractive", "ollama", "anthropic"]
EmbeddingProviderName = Literal["hash", "ollama"]

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


class McpStdioServer(BaseModel):
    """A local MCP server the deployment allows. The API can only refer to one by name."""

    command: str
    args: list[str] = []
    env: dict[str, str] = {}


class Settings(BaseSettings):
    # Commands run from the repository root or from backend/.
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    database_url: SecretStr
    db_pool_size: int = 10

    cookie_secure: bool = True
    cookie_name: str = "opsassist_session"
    session_idle_minutes: int = 120
    session_absolute_hours: int = 24
    allowed_origins: list[str] = []
    login_max_failures: int = 5
    login_window_seconds: int = 300
    questions_per_minute: int = 30

    blob_dir: Path = Path("./var/blobs")
    max_upload_bytes: int = 25 * 1024 * 1024
    max_pdf_pages: int = 2000
    max_extracted_chars: int = 5_000_000

    llm_provider: LLMProviderName = "extractive"
    llm_model: str = "llama3"
    llm_timeout_seconds: float = 180.0
    llm_max_output_tokens: int = 8000
    embedding_provider: EmbeddingProviderName = "hash"
    embedding_model: str = "nomic-embed-text"
    embedding_timeout_seconds: float = 60.0
    embedding_batch_size: int = 32
    ollama_url: str = "http://localhost:11434"
    anthropic_api_key: SecretStr | None = None
    external_model_data_approved: bool = False

    retrieval_candidates: int = 40
    retrieval_top_k: int = 8
    retrieval_max_per_document: int = 4
    retrieval_max_context_chars: int = 16_000
    retrieval_min_dense_similarity: float = 0.62
    retrieval_min_term_coverage: float = 0.6
    max_question_chars: int = 4000
    stale_after_days: int = 365

    worker_poll_seconds: float = 1.0
    worker_lease_seconds: int = 120
    job_max_attempts: int = 5
    job_backoff_base_seconds: float = 5.0

    tool_default_timeout_seconds: float = 20.0
    tool_max_timeout_seconds: float = 60.0
    tool_read_retries: int = 2
    tool_retry_base_seconds: float = 0.5
    tool_calls_per_minute: int = 60
    tool_breaker_failures: int = 5
    tool_breaker_cooldown_seconds: float = 30.0
    tool_max_argument_bytes: int = 64 * 1024
    tool_max_output_chars: int = 200_000
    tool_max_schema_bytes: int = 64 * 1024
    tool_max_discovered: int = 200
    # Hosts allowed despite resolving privately, and over plain HTTP. For internal MCP servers
    # and local development. Empty means public HTTPS only.
    tool_allowed_private_hosts: list[str] = []
    mcp_stdio_servers: dict[str, McpStdioServer] = {}

    @model_validator(mode="after")
    def _check_production_safety(self) -> Settings:
        if self.environment == "production" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true in production")
        if self.uses_external_model and not self.external_model_data_approved:
            raise ValueError(
                "A configured model provider sends data off this machine. Set "
                "EXTERNAL_MODEL_DATA_APPROVED=true only after an explicit data-processing "
                "decision (docs/SECURITY_MODEL.md section 6)."
            )
        return self

    @property
    def uses_external_model(self) -> bool:
        if self.llm_provider == "anthropic":
            return True
        uses_ollama = self.llm_provider == "ollama" or self.embedding_provider == "ollama"
        host = urlparse(self.ollama_url).hostname or ""
        # Inside compose the host daemon is reached by name but stays on this machine.
        return uses_ollama and host not in LOOPBACK_HOSTS | {"host.docker.internal"}


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
