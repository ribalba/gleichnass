"""The public site: registration is open, so the guard rails matter."""

import pytest

from gleichnass import config as config_module
from gleichnass import web

BASE = """
defaults:
  timezone: Europe/Berlin
  language: de
"""

USER = """
id: didi
location: {lat: 47.66, lon: 9.18, name: Konstanz}
channel: {type: ntfy, topic: abc}
rules: [{preset: night}]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "gleichnass.yaml").write_text(BASE)
    (tmp_path / "users.d").mkdir()
    (tmp_path / "users.d" / "didi.yaml").write_text(USER)
    return tmp_path / "gleichnass.yaml"


def test_registration_is_open_without_being_configured(project):
    """The point of the service is that nobody edits a file to add a friend."""
    signup = config_module.load(project).signup
    assert signup.enabled
    assert signup.invite_code is None
    assert signup.per_hour > 0, "open registration still needs a ceiling"


def test_registration_can_be_narrowed_or_closed(project, monkeypatch):
    monkeypatch.setenv("CODE", "regen2026")
    project.write_text(
        BASE + "signup:\n  enabled: false\n  per_hour: 3\n  invite_code: ${CODE}\n"
    )
    signup = config_module.load(project).signup
    assert not signup.enabled
    assert signup.invite_code == "regen2026"
    assert signup.per_hour == 3


def test_the_landing_page_carries_a_working_form():
    page = web.landing().decode()
    assert 'action="/signup"' in page
    for field in ('name="name"', 'name="place"', 'name="modes"'):
        assert field in page
    # Both hooks stay as comments until the server fills them, so the file on
    # disk is a finished page in its own right.
    assert "<!--error-->" in page and "<!--invite-->" in page
    assert 'class="err"' not in page


def test_an_error_is_shown_on_the_form_itself():
    page = web.landing("Den Ort konnte ich nicht finden.").decode()
    assert 'class="err">Den Ort konnte ich nicht finden.' in page


def test_the_invite_field_appears_only_when_a_code_is_set():
    assert 'id="code"' not in web.landing().decode()
    assert 'id="code"' in web.landing(invite_needed=True).decode()


def test_error_text_cannot_inject_markup():
    page = web.landing('<script>alert("x")</script>').decode()
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page


def test_the_rate_limit_counts_every_attempt_then_stops():
    limit = web.RateLimit(per_hour=3)
    assert [limit.allow("1.2.3.4", now=0) for _ in range(4)] == [True, True, True, False]


def test_one_visitor_hitting_the_limit_does_not_block_anyone_else():
    limit = web.RateLimit(per_hour=1)
    assert limit.allow("1.2.3.4", now=0)
    assert not limit.allow("1.2.3.4", now=10)
    assert limit.allow("5.6.7.8", now=10)


def test_the_window_rolls_forward():
    limit = web.RateLimit(per_hour=1)
    assert limit.allow("1.2.3.4", now=0)
    assert not limit.allow("1.2.3.4", now=3599)
    assert limit.allow("1.2.3.4", now=3601)


def test_the_welcome_page_shows_the_topic_and_a_scannable_code():
    entry = {"id": "anna", "name": "Anna", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "regen-secret", "https://ntfy.sh/regen-secret", "Hamburg")

    assert "regen-secret" in page
    assert "<svg" in page, "the QR code is rendered inline, not fetched"
    assert "Hamburg" in page
    assert "Abends um 20:00" in page


def test_telegram_is_offered_only_where_a_bot_is_configured():
    """Better no option than one that cannot work."""
    without = web.landing().decode()
    assert 'name="telegram"' not in without
    assert "Lieber Telegram" not in without, "and no card pointing at a missing box"

    with_bot = web.landing(telegram_bot="gleichnass_bot").decode()
    assert 'name="telegram"' in with_bot
    assert "@gleichnass_bot" in with_bot
    assert "Lieber Telegram" in with_bot


def test_the_configured_bot_replaces_the_one_in_the_template():
    page = web.landing(telegram_bot="wetter_bot").decode()
    assert "@wetter_bot" in page
    assert "gleichnass_bot" not in page


def test_the_hero_sends_people_to_the_start_of_the_flow():
    """Step 2 is locked until step 1 is confirmed, so jumping there is a dead end."""
    page = web.landing().decode()
    assert 'href="#mitmachen">Jetzt mitmachen' in page


class FakeRequest:
    """Just enough of a handler to exercise the address and secret checks."""

    def __init__(self, settings, headers=None, peer="10.0.0.5"):
        self.settings = settings
        self.headers = headers or {}
        self.client_address = (peer, 51000)

    _client = web.Handler._client


def test_the_peer_is_used_when_there_is_no_proxy():
    request = FakeRequest({"trust_proxy": False},
                          {"X-Forwarded-For": "203.0.113.9"}, peer="10.0.0.5")
    assert request._client() == "10.0.0.5", "an untrusted header is just client input"


def test_behind_a_proxy_the_address_the_proxy_saw_wins():
    """Traefik appends the peer it saw, so the rightmost entry is unforgeable."""
    request = FakeRequest({"trust_proxy": True},
                          {"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})
    assert request._client() == "203.0.113.9"


def test_a_spoofed_header_cannot_pick_its_own_bucket():
    request = FakeRequest({"trust_proxy": True}, {"X-Forwarded-For": "9.9.9.9"})
    assert request._client() == "9.9.9.9"

    everyone = web.RateLimit(per_hour=2)
    # A proxy that forgets to set the header must not collapse to one bucket
    # silently; with no header we fall back to the peer.
    bare = FakeRequest({"trust_proxy": True}, {})
    assert bare._client() == "10.0.0.5"
    assert everyone.allow(bare._client())


def test_an_invite_code_with_umlauts_does_not_crash_the_comparison():
    assert web._same_secret("geheim-ü", "geheim-ü")
    assert not web._same_secret("geheim-ü", "anders")


def test_a_person_is_found_by_name_or_by_part_of_their_id(project):
    from gleichnass import cli

    config = config_module.load(project)
    didi = config.users[0]

    assert cli.resolve_user(config, didi.id) is didi
    assert cli.resolve_user(config, didi.id[:6]) is didi
    assert cli.resolve_user(config, "didi") is didi, "by name, whatever the id is"

    with pytest.raises(KeyError):
        cli.resolve_user(config, "nobody")


def test_two_people_with_one_name_are_reported_not_guessed(project):
    from gleichnass import cli

    for n in (1, 2):
        (project.parent / "users.d" / f"alex{n}.yaml").write_text(
            f"id: 0000000{n}-aaaa-bbbb-cccc-dddddddddddd\n"
            "name: Alex\n"
            "location: {lat: 47.66, lon: 9.18}\n"
            "channel: {type: ntfy, topic: abc}\n"
            "rules: [{preset: night}]\n"
        )
    config = config_module.load(project)

    with pytest.raises(ValueError, match="more than one person"):
        cli.resolve_user(config, "Alex")
    # ...but each is still reachable by their own id.
    assert cli.resolve_user(config, "00000001").name == "Alex"
