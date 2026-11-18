import pytest

from opsassist.integrations.gmail_provider import gmail_query


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("is:unread newer_than:1w", "is:unread newer_than:7d"),
        ("older_than:2W in:inbox", "older_than:14d in:inbox"),
        ("newer_than:7d", "newer_than:7d"),
        ("newer_than:3m older_than:1y", "newer_than:3m older_than:1y"),
        # Only the operator's value changes, not words that happen to look alike.
        ("subject:1w newer_than:1week", "subject:1w newer_than:1week"),
    ],
)
def test_week_units_become_days(query: str, expected: str) -> None:
    result = gmail_query(query)
    assert result.text == expected
    assert result.dropped == ()


def test_unknown_operators_are_dropped_and_reported() -> None:
    # llama3.2:3b wrote this on 2026-11-18. Gmail would search for the text "awaits:reply".
    result = gmail_query("in:inbox newer_than:7d awaits:reply")
    assert result.text == "in:inbox newer_than:7d"
    assert result.dropped == ("awaits:reply",)


@pytest.mark.parametrize(
    "query",
    [
        "from:ann -label:spam",
        "From:ann IS:unread",
        "deploy delay https://example.test/a:b",
        '"error: timeout" subject:outage',
        "category:updates has:attachment larger:5M",
    ],
)
def test_known_operators_urls_and_plain_text_are_kept(query: str) -> None:
    result = gmail_query(query)
    assert result.text == query
    assert result.dropped == ()


def test_negated_unknown_operator_is_dropped_whole() -> None:
    result = gmail_query("-needs:reply from:ann")
    assert result.text == "from:ann"
    assert result.dropped == ("-needs:reply",)
