import pytest

from opsassist.integrations.gmail_signals import is_automated


@pytest.mark.parametrize(
    "headers",
    [
        {"auto-submitted": "auto-generated"},
        {"auto-submitted": "Auto-Replied"},
        {"precedence": "bulk"},
        {"precedence": " List "},
        {"precedence": "junk"},
        {"list-unsubscribe": "<mailto:unsubscribe@news.example.test>"},
        {"list-id": "Engineering <eng.example.test>"},
    ],
)
def test_standard_automation_headers_mark_a_message(headers: dict[str, str]) -> None:
    assert is_automated(headers, "ann@example.test") is True


@pytest.mark.parametrize(
    "address",
    [
        "noreply@example.test",
        "no-reply@example.test",
        "No_Reply@example.test",
        "donotreply@example.test",
        "do-not-reply@example.test",
        "mailer-daemon@example.test",
        "postmaster@example.test",
        "bounces+123@example.test",
        "noreply-abc@example.test",
    ],
)
def test_automated_sender_addresses(address: str) -> None:
    assert is_automated({}, address) is True


@pytest.mark.parametrize(
    "address",
    ["ann@example.test", "noreplyann@example.test", "replies@example.test", "notify.me@x.test", ""],
)
def test_people_are_not_marked(address: str) -> None:
    assert is_automated({}, address) is False


def test_auto_submitted_no_and_normal_precedence_are_not_automation() -> None:
    assert is_automated({"auto-submitted": "no", "precedence": "first-class"}, "a@b.test") is False
    assert is_automated({"list-unsubscribe": "  "}, "a@b.test") is False
