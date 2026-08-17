"""Linking a Telegram chat to a signup, without anyone looking up a chat id."""

import httpx
import pytest

from gleichnass import config as config_module
from gleichnass import telegram_link

BASE = """
defaults:
  timezone: Europe/Berlin
  channels:
    telegram: {token: bot-token}
signup:
  telegram_bot: gleichnass_bot
"""

WAITING = """
id: anna
location: {lat: 53.55, lon: 9.99, name: Hamburg}
channels:
  - {type: ntfy, topic: abc}
rules: [{preset: night}]
telegram_code: ABC123
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "gleichnass.yaml").write_text(BASE)
    (tmp_path / "users.d").mkdir()
    (tmp_path / "users.d" / "anna.yaml").write_text(WAITING)
    return tmp_path / "gleichnass.yaml"


def bot_saw(*texts, chat_id=4242):
    """A client whose getUpdates returns these message bodies."""
    payload = {
        "result": [
            {"message": {"text": t, "chat": {"id": chat_id, "first_name": "Anna"}}}
            for t in texts
        ]
    }
    return httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    )


def test_codes_avoid_characters_people_misread():
    code = telegram_link.new_code()
    assert len(code) == telegram_link.CODE_LENGTH
    assert not set(code) & set("IOSZ0125"), "no letter/digit lookalikes"


def test_the_deep_link_carries_the_code():
    assert telegram_link.deep_link("gleichnass_bot", "ABC123") == (
        "https://t.me/gleichnass_bot?start=ABC123"
    )
    assert telegram_link.deep_link("@gleichnass_bot", "ABC123").count("@") == 0


def test_a_matching_message_attaches_the_chat(project):
    config = config_module.load(project)
    assert telegram_link.pending(config) == {"anna": "ABC123"}

    linked = telegram_link.link_waiting(config, bot_saw("/start ABC123"), "bot-token")
    assert linked == [("anna", 4242)]

    anna = config_module.load(project).user("anna")
    assert [c.type for c in anna.channels] == ["ntfy", "telegram"]
    assert anna.channels[1].settings["chat_id"] == 4242
    assert anna.channels[1].settings["token"] == "bot-token", "shared token still applies"


def test_the_code_is_spent_once_used(project):
    config = config_module.load(project)
    telegram_link.link_waiting(config, bot_saw("/start ABC123"), "bot-token")

    config = config_module.load(project)
    assert telegram_link.pending(config) == {}, "nothing left for a replay to claim"


def test_replaying_the_same_update_does_not_add_a_second_channel(project):
    """getUpdates is read without an offset, so the same message comes back."""
    config = config_module.load(project)
    telegram_link.link_waiting(config, bot_saw("/start ABC123"), "bot-token")

    config = config_module.load(project)
    telegram_link.link_waiting(config, bot_saw("/start ABC123"), "bot-token")

    anna = config_module.load(project).user("anna")
    assert [c.type for c in anna.channels] == ["ntfy", "telegram"]


def test_an_unrelated_message_links_nobody(project):
    config = config_module.load(project)
    assert telegram_link.link_waiting(config, bot_saw("/start", "hallo"), "bot-token") == []
    assert telegram_link.pending(config_module.load(project)) == {"anna": "ABC123"}


def test_the_code_is_matched_whatever_case_it_is_sent_in(project):
    config = config_module.load(project)
    assert telegram_link.link_waiting(config, bot_saw("/start abc123"), "bot-token") == [
        ("anna", 4242)
    ]


def test_a_telegram_outage_is_survivable(project):
    def boom(_):
        raise httpx.ConnectError("telegram unreachable")

    config = config_module.load(project)
    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert telegram_link.link_waiting(config, client, "bot-token") == []
    assert telegram_link.pending(config_module.load(project)) == {"anna": "ABC123"}


def test_nothing_waiting_means_the_bot_is_not_called(project):
    (project.parent / "users.d" / "anna.yaml").write_text(
        WAITING.replace("telegram_code: ABC123", "")
    )
    config = config_module.load(project)

    def boom(_):
        raise AssertionError("should not have called Telegram")

    client = httpx.Client(transport=httpx.MockTransport(boom))
    assert telegram_link.link_waiting(config, client, "bot-token") == []


def test_linking_rewrites_the_file_the_user_came_from(project):
    """A hand-named file must not be duplicated under an id-derived name:
    two files with one id stop the whole config from loading."""
    original = project.parent / "users.d" / "anna.yaml"
    original.write_text(WAITING.replace("id: anna", "id: 1234-5678"))
    config = config_module.load(project)

    telegram_link.link_waiting(config, bot_saw("/start ABC123"), "bot-token")

    assert original.exists(), "still the same file"
    assert not (project.parent / "users.d" / "1234-5678.yaml").exists()
    reloaded = config_module.load(project)          # would raise on a duplicate
    assert [c.type for c in reloaded.user("1234-5678").channels] == ["ntfy", "telegram"]
