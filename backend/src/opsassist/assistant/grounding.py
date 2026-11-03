"""Validate model output against the evidence that was actually supplied.

This proves citations refer to real evidence and measures how much text is cited. It does not
prove a cited excerpt supports the claim. That is measured offline by the evaluation harness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from opsassist.assistant.prompts import NOT_FOUND_SENTINEL

_GROUP = re.compile(r"\[\s*((?:S\s*\d+)(?:\s*[,;]\s*S\s*\d+)*)\s*\]", re.IGNORECASE)
_MARKER = re.compile(r"\[(S\d+)\]")
# A marker after the full stop ("Claim. [S1]") belongs to the sentence before it.
_SENTENCE = re.compile(r"(?<=[.!?\]])\s+(?!\[S\d)|\n+(?!\s*\[S\d)")
_MIN_CLAIM_CHARS = 25


@dataclass(frozen=True)
class ValidatedAnswer:
    text: str
    used_markers: frozenset[str]
    removed_markers: tuple[str, ...]
    uncited_sentences: int
    cited_sentences: int
    says_not_found: bool


def _normalize_markers(text: str) -> str:
    """Rewrite variants like [S1, S2] and [ s1 ] to [S1][S2]."""

    def replace(match: re.Match[str]) -> str:
        parts = re.split(r"\s*[,;]\s*", match.group(1))
        return "".join("[" + re.sub(r"\s+", "", part).upper() + "]" for part in parts)

    return _GROUP.sub(replace, text)


def validate_answer(raw_text: str, valid_markers: set[str]) -> ValidatedAnswer:
    text = _normalize_markers(raw_text.strip())
    removed: list[str] = []
    used: set[str] = set()

    def check(match: re.Match[str]) -> str:
        found = match.group(1)
        if found in valid_markers:
            used.add(found)
            return match.group(0)
        removed.append(found)
        return ""

    text = _MARKER.sub(check, text).strip()

    cited = uncited = 0
    for sentence in _SENTENCE.split(text):
        stripped = sentence.strip()
        if _MARKER.search(stripped):
            cited += 1
        elif len(stripped) >= _MIN_CLAIM_CHARS and not stripped.endswith(":"):
            uncited += 1

    return ValidatedAnswer(
        text=text,
        used_markers=frozenset(used),
        removed_markers=tuple(dict.fromkeys(removed)),
        uncited_sentences=uncited,
        cited_sentences=cited,
        says_not_found=NOT_FOUND_SENTINEL.lower() in text.lower() and not used,
    )
