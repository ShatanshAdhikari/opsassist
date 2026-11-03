"""Structure-aware chunking. Each content type keeps the boundaries citations rely on.

Markdown keeps heading hierarchy, code fences and tables with their headers, and line numbers.
PDF keeps page numbers. Text splits on paragraphs. Add new strategies through `chunk_document`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from opsassist.knowledge.extraction import CONTENT_MARKDOWN, CONTENT_PDF, ExtractedDocument

DEFAULT_MAX_CHARS = 1200

_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(```|~~~)")
_TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    kind: str
    section_path: tuple[str, ...] = ()
    page: int | None = None
    line_start: int | None = None
    line_end: int | None = None


@dataclass(frozen=True)
class _Block:
    text: str
    kind: str
    line_start: int | None
    line_end: int | None


def chunk_document(
    content_type: str, document: ExtractedDocument, *, max_chars: int = DEFAULT_MAX_CHARS
) -> list[ChunkDraft]:
    if content_type == CONTENT_PDF:
        return _chunk_pdf(document, max_chars)
    if content_type == CONTENT_MARKDOWN:
        return _chunk_markdown(document.text, max_chars)
    return _pack(_paragraph_blocks(document.text.split("\n"), 1), max_chars)


def _chunk_pdf(document: ExtractedDocument, max_chars: int) -> list[ChunkDraft]:
    chunks: list[ChunkDraft] = []
    for page in document.pages:
        paragraphs = [part.strip() for part in re.split(r"\n\s*\n", page.text) if part.strip()]
        blocks = [_Block(paragraph, "prose", None, None) for paragraph in paragraphs]
        chunks.extend(_pack(blocks, max_chars, page=page.number))
    return chunks


def _chunk_markdown(text: str, max_chars: int) -> list[ChunkDraft]:
    lines = text.split("\n")
    chunks: list[ChunkDraft] = []
    headings: list[tuple[int, str]] = []
    section_start = 0
    in_fence = False

    def flush(end: int) -> None:
        path = tuple(title for _, title in headings)
        section_lines = lines[section_start:end]
        chunks.extend(_pack(_markdown_blocks(section_lines, section_start + 1), max_chars, path))

    for index, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        match = None if in_fence else _HEADING.match(line)
        if match:
            flush(index)
            level = len(match.group(1))
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, match.group(2)))
            section_start = index + 1
    flush(len(lines))
    return chunks


def _markdown_blocks(lines: list[str], first_line: int) -> list[_Block]:
    blocks: list[_Block] = []
    paragraph: list[str] = []
    paragraph_start = first_line

    def end_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(
                _Block(
                    "\n".join(paragraph).strip(),
                    "prose",
                    paragraph_start,
                    paragraph_start + len(paragraph) - 1,
                )
            )
            paragraph = []

    index = 0
    while index < len(lines):
        line = lines[index]
        number = first_line + index
        if _FENCE.match(line):
            end_paragraph()
            end = index + 1
            while end < len(lines) and not _FENCE.match(lines[end]):
                end += 1
            end = min(end, len(lines) - 1)
            text = "\n".join(lines[index : end + 1])
            blocks.append(_Block(text, "code", number, first_line + end))
            index = end + 1
        elif (
            "|" in line
            and index + 1 < len(lines)
            and "-" in lines[index + 1]
            and _TABLE_SEPARATOR.match(lines[index + 1])
        ):
            end_paragraph()
            end = index + 2
            while end < len(lines) and "|" in lines[end] and lines[end].strip():
                end += 1
            text = "\n".join(lines[index:end])
            blocks.append(_Block(text, "table", number, first_line + end - 1))
            index = end
        elif not line.strip():
            end_paragraph()
            index += 1
        else:
            if not paragraph:
                paragraph_start = number
            paragraph.append(line)
            index += 1
    end_paragraph()
    return blocks


def _paragraph_blocks(lines: list[str], first_line: int) -> list[_Block]:
    blocks: list[_Block] = []
    current: list[str] = []
    start = first_line
    for offset, line in enumerate([*lines, ""]):
        if line.strip():
            if not current:
                start = first_line + offset
            current.append(line)
        elif current:
            blocks.append(
                _Block("\n".join(current).strip(), "prose", start, start + len(current) - 1)
            )
            current = []
    return blocks


def _split_oversized(block: _Block, max_chars: int) -> Iterable[_Block]:
    if len(block.text) <= max_chars:
        yield block
    elif block.kind == "table":
        yield from _split_table(block, max_chars)
    elif block.kind == "code":
        yield from _split_lines(block, max_chars)
    else:
        yield from _split_prose(block, max_chars)


def _split_table(block: _Block, max_chars: int) -> Iterable[_Block]:
    """Split between rows and repeat the header, so rows keep their column names."""
    rows = block.text.split("\n")
    header = rows[:2]
    header_size = sum(len(row) + 1 for row in header)
    start_line = (block.line_start or 1) + 2
    current: list[str] = []
    current_start = start_line
    size = header_size
    for offset, row in enumerate(rows[2:]):
        if current and size + len(row) + 1 > max_chars:
            end_line = current_start + len(current) - 1
            yield _Block("\n".join([*header, *current]), "table", current_start, end_line)
            current, size, current_start = [], header_size, start_line + offset
        current.append(row)
        size += len(row) + 1
    if current:
        end_line = current_start + len(current) - 1
        yield _Block("\n".join([*header, *current]), "table", current_start, end_line)


def _split_lines(block: _Block, max_chars: int) -> Iterable[_Block]:
    start_line = block.line_start or 1
    current: list[str] = []
    current_start = start_line
    size = 0
    for offset, line in enumerate(block.text.split("\n")):
        piece = line
        if current and size + len(piece) + 1 > max_chars:
            yield _Block(
                "\n".join(current), block.kind, current_start, current_start + len(current) - 1
            )
            current, size, current_start = [], 0, start_line + offset
        current.append(piece[:max_chars])
        size += len(piece) + 1
    if current:
        yield _Block(
            "\n".join(current), block.kind, current_start, current_start + len(current) - 1
        )


def _split_prose(block: _Block, max_chars: int) -> Iterable[_Block]:
    current = ""
    for sentence in _SENTENCE_END.split(block.text):
        pieces = [sentence[i : i + max_chars] for i in range(0, len(sentence), max_chars)]
        for piece in pieces:
            if current and len(current) + len(piece) + 1 > max_chars:
                yield _Block(current, block.kind, block.line_start, block.line_end)
                current = ""
            current = f"{current} {piece}" if current else piece
    if current:
        yield _Block(current, block.kind, block.line_start, block.line_end)


def _pack(
    blocks: Iterable[_Block],
    max_chars: int,
    section_path: tuple[str, ...] = (),
    page: int | None = None,
) -> list[ChunkDraft]:
    """Greedily merge adjacent blocks up to the size limit."""
    chunks: list[ChunkDraft] = []
    pending: list[_Block] = []
    size = 0

    def flush() -> None:
        nonlocal pending, size
        if not pending:
            return
        kinds = {block.kind for block in pending}
        starts = [block.line_start for block in pending if block.line_start is not None]
        ends = [block.line_end for block in pending if block.line_end is not None]
        chunks.append(
            ChunkDraft(
                content="\n\n".join(block.text for block in pending),
                kind=kinds.pop() if len(kinds) == 1 else "mixed",
                section_path=section_path,
                page=page,
                line_start=min(starts) if starts else None,
                line_end=max(ends) if ends else None,
            )
        )
        pending, size = [], 0

    for original in blocks:
        for block in _split_oversized(original, max_chars):
            if pending and size + len(block.text) + 2 > max_chars:
                flush()
            pending.append(block)
            size += len(block.text) + 2
    flush()
    return chunks
