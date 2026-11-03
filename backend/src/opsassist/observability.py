"""Structured logging with correlation IDs and secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|authorization|cookie|credential|private[_-]?key",
    re.IGNORECASE,
)

SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{16,}"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:\s/]+:)[^@\s]+(@)"),
)

REDACTED = "[REDACTED]"


def redact_secrets(text: str) -> str:
    """Replace token-shaped values. Pattern-based: a mitigation, not a guarantee."""
    for pattern in SECRET_PATTERNS:
        text = pattern.sub(REDACTED, text)
    return text


def redact_tree(value: Any) -> Any:
    """Redact every string inside decoded JSON."""
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {key: redact_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_tree(item) for item in value]
    return value


def _redact_event(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in list(event_dict.items()):
        if _SENSITIVE_KEYS.search(key):
            event_dict[key] = REDACTED
        elif isinstance(value, str):
            event_dict[key] = redact_secrets(value)
    return event_dict


def configure_logging(level: str = "INFO") -> None:
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        _redact_event,
        structlog.processors.JSONRenderer(),
    ]
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
