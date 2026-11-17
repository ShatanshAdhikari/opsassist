import pytest

from opsassist.integrations.gmail_signals import sender_kind


@pytest.mark.parametrize(
    "headers",
    [
        {"auto-submitted": "auto-generated"},
        {"auto-submitted": "Auto-Replied"},
        {"precedence": "bulk"},
        {"precedence": " Junk "},
        {"list-unsubscribe": "<mailto:unsubscribe@news.example.test>"},
    ],
)
def test_standard_automation_headers_mark_a_message(headers: dict[str, str]) -> None:
    assert sender_kind(headers, "ann@example.test") == "automated"


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
        "notifications-noreply@linkedin.example.test",
        "jobs.noreply@example.test",
    ],
)
def test_automated_sender_addresses(address: str) -> None:
    assert sender_kind({}, address) == "automated"


@pytest.mark.parametrize(
    "address",
    ["ann@example.test", "noreplyann@example.test", "replies@example.test", "notify.me@x.test", ""],
)
def test_people_are_not_marked(address: str) -> None:
    assert sender_kind({}, address) == "person"


@pytest.mark.parametrize(
    "headers",
    [
        {"list-id": "Engineering <eng.example.test>"},
        {"precedence": "list"},
        # Google Groups adds both to messages people write. The list wins over unsubscribe.
        {"list-id": "<eng.example.test>", "list-unsubscribe": "<mailto:u@example.test>"},
    ],
)
def test_mailing_lists_are_kept_apart_from_automated_mail(headers: dict[str, str]) -> None:
    assert sender_kind(headers, "ann@example.test") == "mailing_list"


def test_explicit_automation_outranks_a_list() -> None:
    assert sender_kind({"list-id": "<x>", "auto-submitted": "auto-generated"}, "a@b.test") == (
        "automated"
    )
    assert sender_kind({"list-id": "<x>"}, "noreply@b.test") == "automated"


def test_auto_submitted_no_and_normal_precedence_are_not_automation() -> None:
    headers = {"auto-submitted": "no", "precedence": "first-class"}
    assert sender_kind(headers, "a@b.test") == "person"
    assert sender_kind({"list-unsubscribe": "  "}, "a@b.test") == "person"
