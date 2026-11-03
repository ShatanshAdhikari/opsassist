from __future__ import annotations

from opsassist.knowledge.chunking import chunk_document
from opsassist.knowledge.extraction import (
    CONTENT_MARKDOWN,
    CONTENT_PDF,
    CONTENT_TEXT,
    ExtractedDocument,
    ExtractedPage,
)


def markdown(text: str, **kwargs: int) -> list:
    return chunk_document(CONTENT_MARKDOWN, ExtractedDocument(text=text), **kwargs)


def test_markdown_tracks_heading_hierarchy_and_lines() -> None:
    text = (
        "# Top\n\nIntro.\n\n## Child\n\nChild body.\n\n### Deep\n\nDeep body.\n\n## Sibling\n\nEnd."
    )
    chunks = markdown(text)
    assert [c.section_path for c in chunks] == [
        ("Top",),
        ("Top", "Child"),
        ("Top", "Child", "Deep"),
        ("Top", "Sibling"),
    ]
    assert [c.content for c in chunks] == ["Intro.", "Child body.", "Deep body.", "End."]
    assert (chunks[1].line_start, chunks[1].line_end) == (7, 7)
    assert text.split("\n")[chunks[3].line_start - 1] == "End."


def test_heading_syntax_inside_code_fence_is_not_a_heading() -> None:
    text = "# Real\n\n```bash\n# not a heading\nls\n```\n\nAfter."
    chunks = markdown(text)
    assert all(c.section_path == ("Real",) for c in chunks)
    assert "# not a heading" in chunks[0].content


def test_unclosed_fence_keeps_remaining_content() -> None:
    chunks = markdown("# T\n\n```\ncode line\nmore code")
    assert "more code" in chunks[0].content
    assert chunks[0].kind == "code"


def test_small_table_stays_whole() -> None:
    table = "| Service | Owner |\n| --- | --- |\n| billing | ana |\n| auth | ben |"
    chunks = markdown(f"# T\n\n{table}")
    assert chunks[0].kind == "table"
    assert chunks[0].content == table


def test_large_table_repeats_header_and_keeps_rows_whole() -> None:
    header = "| Service | Owner |\n| --- | --- |"
    rows = [f"| service-{i:03d} | owner-{i:03d} |" for i in range(100)]
    chunks = markdown(f"# T\n\n{header}\n" + "\n".join(rows), max_chars=300)
    assert len(chunks) > 1
    seen = []
    for chunk in chunks:
        assert chunk.content.startswith(header)
        body = chunk.content.split("\n")[2:]
        assert all(line in rows for line in body)
        seen.extend(body)
    assert seen == rows


def test_large_table_line_numbers_point_at_rows() -> None:
    header = "| A | B |\n| --- | --- |"
    rows = [f"| row-{i:03d} | value-{i:03d} |" for i in range(50)]
    text = f"# T\n\n{header}\n" + "\n".join(rows)
    source_lines = text.split("\n")
    for chunk in markdown(text, max_chars=200):
        first_row = chunk.content.split("\n")[2]
        assert source_lines[chunk.line_start - 1] == first_row


def test_oversized_prose_splits_within_limit_without_losing_words() -> None:
    sentences = [f"Sentence number {i} has some words." for i in range(200)]
    text = " ".join(sentences)
    chunks = chunk_document(CONTENT_TEXT, ExtractedDocument(text=text), max_chars=300)
    assert all(len(c.content) <= 300 for c in chunks)
    joined = " ".join(c.content for c in chunks)
    assert all(s in joined for s in sentences)


def test_single_giant_word_is_hard_split() -> None:
    chunks = chunk_document(CONTENT_TEXT, ExtractedDocument(text="x" * 1000), max_chars=300)
    assert all(len(c.content) <= 300 for c in chunks)
    assert sum(len(c.content.replace(" ", "")) for c in chunks) == 1000


def test_text_paragraph_line_numbers() -> None:
    chunks = chunk_document(
        CONTENT_TEXT, ExtractedDocument(text="first\nsecond\n\n\nthird"), max_chars=15
    )
    assert [(c.content, c.line_start, c.line_end) for c in chunks] == [
        ("first\nsecond", 1, 2),
        ("third", 5, 5),
    ]


def test_pdf_chunks_keep_page_numbers() -> None:
    document = ExtractedDocument(
        pages=(ExtractedPage(1, "Page one text."), ExtractedPage(3, "Page three.\n\nMore."))
    )
    chunks = chunk_document(CONTENT_PDF, document)
    assert [(c.page, c.content) for c in chunks] == [
        (1, "Page one text."),
        (3, "Page three.\n\nMore."),
    ]


def test_heading_only_document_produces_no_chunks() -> None:
    assert markdown("# Only\n\n## Headings") == []


def test_crlf_free_blank_input_produces_no_chunks() -> None:
    assert chunk_document(CONTENT_TEXT, ExtractedDocument(text="  \n \n")) == []
