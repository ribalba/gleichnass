"""The public site: registration is open, so the guard rails matter."""

from types import SimpleNamespace

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
    assert "Jeden Abend um 20:00" in page, "same wording as the notification itself"


def test_telegram_is_offered_only_where_a_bot_is_configured():
    """Better no option than one that cannot work."""
    without = web.landing().decode()
    assert 'value="telegram"' not in without
    assert "Telegram" not in without, "and nothing anywhere pointing at a missing choice"

    with_bot = web.landing(telegram_bot="gleichnass_bot").decode()
    assert 'value="telegram"' in with_bot
    assert "@gleichnass_bot" in with_bot


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


def test_the_qr_page_opens_the_app_rather_than_a_web_page():
    """A QR that lands in a browser is the thing this page exists to avoid."""
    page = web.subscribe_page("gleichnass-abc", "Konstanz", "https://ntfy.sh")

    assert "ntfy://ntfy.sh/gleichnass-abc?display=Konstanz" in page
    assert "location.replace" in page, "Android is forwarded straight into the app"
    assert "/Android/i" in page, "and only Android, since iOS has no ntfy:// handler"


def test_the_qr_page_still_works_where_the_deep_link_does_not():
    page = web.subscribe_page("gleichnass-abc", "Konstanz", "https://ntfy.sh")
    assert "gleichnass-abc" in page, "the topic, to type in by hand"
    assert "https://ntfy.sh/gleichnass-abc" in page, "and the web app as a last resort"


def test_a_self_hosted_http_server_is_flagged_as_insecure_in_the_deep_link():
    page = web.subscribe_page("t", "Ort", "http://ntfy.example.test")
    assert "ntfy://ntfy.example.test/t?display=Ort&secure=false" in page


def test_the_telegram_bot_is_read_from_the_token(monkeypatch):
    """Setting only the token used to leave Telegram silently switched off."""
    calls = []

    def fake_username(client, token, server="https://api.telegram.org"):
        calls.append(token)
        return "GleichNass_bot"

    monkeypatch.setattr(web, "bot_username", fake_username)
    web._BOT_NAMES.clear()

    config = SimpleNamespace(
        signup=SimpleNamespace(telegram_bot=None),
        defaults={"channels": {"telegram": {"token": "12345:secret"}}},
    )
    assert web.telegram_bot_for(config) == "GleichNass_bot"
    assert web.telegram_bot_for(config) == "GleichNass_bot"
    assert calls == ["12345:secret"], "asked once, then remembered"


def test_no_token_means_no_telegram(monkeypatch):
    monkeypatch.setattr(web, "bot_username", lambda *a, **k: pytest.fail("should not ask"))
    config = SimpleNamespace(
        signup=SimpleNamespace(telegram_bot=None), defaults={"channels": {}}
    )
    assert web.telegram_bot_for(config) is None


def test_the_qr_encodes_our_own_subscribe_url():
    """The target is inside the QR modules, not the page text, so compare it
    against an independently generated code for the URL we expect."""
    import segno

    target = "https://gleichnass.de/abo/gleichnass-abc?ort=Konstanz"
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "gleichnass-abc", target, "Konstanz")

    expected = segno.make(target, error="m").svg_inline(
        scale=4, dark="#14213a", light="#ffffff"
    )
    assert expected in page


def test_the_public_url_comes_from_the_proxy_headers():
    handler = FakeRequest(
        {"trust_proxy": True},
        {"Host": "gleichnass.de", "X-Forwarded-Proto": "https"},
    )
    handler._base_url = web.Handler._base_url.__get__(handler)
    config = SimpleNamespace(signup=SimpleNamespace(base_url=""))
    assert handler._base_url(config) == "https://gleichnass.de"


def test_a_configured_base_url_wins():
    handler = FakeRequest({"trust_proxy": True}, {"Host": "internal:8080"})
    handler._base_url = web.Handler._base_url.__get__(handler)
    config = SimpleNamespace(signup=SimpleNamespace(base_url="https://gleichnass.de/"))
    assert handler._base_url(config) == "https://gleichnass.de"


def test_the_telegram_step_offers_a_qr_as_well_as_a_link():
    """The confirmation page is usually on a computer; Telegram is on a phone."""
    import segno

    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "topic", "https://x.test/abo/topic", "Konstanz",
                       telegram_bot="GleichNass_bot", code="ABC123")

    link = "https://t.me/GleichNass_bot?start=ABC123"
    assert link in page, "the button, for when the page is already on the phone"
    expected = segno.make(link, error="m").svg_inline(
        scale=4, dark="#14213a", light="#ffffff"
    )
    assert expected in page, "and a QR encoding the same link"
    assert "/start ABC123" in page, "and the code to type as a last resort"


def test_there_is_no_telegram_step_without_a_bot():
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "topic", "https://x.test/abo/topic", "Konstanz")
    assert "t.me/" not in page


UNSUB = """
id: 11111111-2222-3333-4444-555555555555
name: Anna
location: {lat: 53.55, lon: 9.99}
channels: [{type: ntfy, topic: abc}]
unsubscribe: leave-me-alone
rules: [{preset: night}]
"""


def test_a_notification_carries_a_way_out(project):
    """ntfy never reports an unsubscribe, so the only way anyone can leave is a
    link they were given."""
    from gleichnass import message as message_module

    (project.parent / "users.d" / "anna.yaml").write_text(UNSUB)
    project.write_text(BASE + "signup:\n  base_url: https://gleichnass.de\n")
    anna = config_module.load(project).user("11111111-2222-3333-4444-555555555555")

    assert anna.unsubscribe_url == (
        "https://gleichnass.de/abbestellen"
        "?u=11111111-2222-3333-4444-555555555555&t=leave-me-alone"
    )
    action = message_module._leaving(anna, message_module.texts("de"))[0]
    assert action["label"] == "Abmelden"
    assert action["url"] == anna.unsubscribe_url


def test_without_a_base_url_there_is_no_link_to_offer(project):
    from gleichnass import message as message_module

    (project.parent / "users.d" / "anna.yaml").write_text(UNSUB)
    anna = config_module.load(project).user("11111111-2222-3333-4444-555555555555")
    assert anna.unsubscribe_url == ""
    assert message_module._leaving(anna, message_module.texts("de")) == []


def test_leaving_deletes_the_whole_record(project):
    (project.parent / "users.d" / "anna.yaml").write_text(UNSUB)
    config = config_module.load(project)
    anna = config.user("11111111-2222-3333-4444-555555555555")

    assert config_module.delete_user(config, anna)
    assert not (project.parent / "users.d" / "anna.yaml").exists()


def test_a_blocked_telegram_chat_is_dropped_but_ntfy_survives(project):
    (project.parent / "users.d" / "anna.yaml").write_text(
        UNSUB.replace("channels: [{type: ntfy, topic: abc}]",
                      "channels: [{type: ntfy, topic: abc}, "
                      "{type: telegram, chat_id: 42, token: t}]")
    )
    config = config_module.load(project)
    anna = config.user("11111111-2222-3333-4444-555555555555")

    assert config_module.drop_channel(config, anna, "telegram", "42")
    left = config_module.load(project).user("11111111-2222-3333-4444-555555555555")
    assert [c.type for c in left.channels] == ["ntfy"]


def test_losing_the_last_channel_removes_the_user(project):
    (project.parent / "users.d" / "anna.yaml").write_text(
        UNSUB.replace("channels: [{type: ntfy, topic: abc}]",
                      "channels: [{type: telegram, chat_id: 42, token: t}]")
    )
    config = config_module.load(project)
    anna = config.user("11111111-2222-3333-4444-555555555555")

    assert config_module.drop_channel(config, anna, "telegram", "42")
    assert not (project.parent / "users.d" / "anna.yaml").exists(), (
        "nothing left to deliver to, so nothing left to keep"
    )


def test_the_impressum_carries_the_operator_details(project):
    from gleichnass.config import Impressum

    legal = config_module.load(project).impressum
    assert legal.name == "RebelProject UG (haftungsbeschränkt)"
    assert legal.email == "gleichnass@rebelproject.org"
    assert not legal.complete, (
        "a UG must also name its Geschäftsführer and register entry"
    )

    filled = Impressum(represented_by="A. Person", register="Amtsgericht Potsdam, HRB 1")
    assert filled.complete


def test_the_config_can_override_the_operator(project):
    project.write_text(
        BASE + "impressum:\n  name: Someone Else\n  email: hi@example.test\n"
    )
    legal = config_module.load(project).impressum
    assert legal.name == "Someone Else"
    assert legal.email == "hi@example.test"


def test_the_landing_page_links_to_it():
    assert 'href="/impressum"' in web.landing().decode()


def test_link_previews_are_absolute_when_we_know_the_address():
    """Crawlers for chat previews generally will not follow a relative image."""
    relative = web.landing().decode()
    assert 'property="og:image" content="/og.png"' in relative

    absolute = web.landing(base_url="https://gleichnass.de").decode()
    assert 'property="og:image" content="https://gleichnass.de/og.png"' in absolute
    assert 'property="og:url" content="https://gleichnass.de/"' in absolute


def test_the_page_declares_a_viewport():
    """Without it a phone lays the page out at 980px and zooms out, so none of
    the mobile styling applies."""
    assert 'name="viewport"' in web.landing().decode()


# -- one screen or two ----------------------------------------------------


def test_the_browser_is_taken_at_its_word_about_what_it_runs_on():
    assert web.platform_of("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)") == "ios"
    assert web.platform_of("Mozilla/5.0 (iPad; CPU OS 16_0 like Mac OS X)") == "ios"
    assert web.platform_of("Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile") == "android"
    assert web.platform_of("Mozilla/5.0 (Linux; U) Mobile Safari") == "mobile"
    assert web.platform_of("Mozilla/5.0 (X11; Linux x86_64) Chrome/126") == "desktop"
    assert web.platform_of("") == "desktop", "a page still has to render without one"


def test_a_phone_gets_a_button_where_a_computer_gets_a_code():
    """Scanning a code with the device it is displayed on is not a thing."""
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    args = (entry, "gleichnass-abc", "https://x.test/abo/gleichnass-abc", "Konstanz")

    phone = web.welcome(*args, platform="android")
    assert 'class="segno"' not in phone, "no QR code on the screen doing the scanning"
    assert "ntfy://ntfy.sh/gleichnass-abc?display=Konstanz" in phone

    computer = web.welcome(*args, platform="desktop")
    assert 'class="segno"' in computer
    assert "https://x.test/abo/gleichnass-abc" in computer, "and a link for a touch iPad"


def test_the_iphone_is_never_shown_a_link_it_cannot_follow():
    """iOS registers no ntfy:// handler: tapping it raises an error dialog."""
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "gleichnass-abc", "https://x.test/abo/gleichnass-abc",
                       "Konstanz", platform="ios")
    assert "ntfy://" not in page
    assert 'data-copy="gleichnass-abc"' in page, "one tap to the clipboard instead"
    assert "https://ntfy.sh/gleichnass-abc" in page, "and the web app, which needs no app"

    scanned = web.subscribe_page("gleichnass-abc", "Konstanz", "https://ntfy.sh", "ios")
    assert "ntfy://" not in scanned
    assert "location.replace" not in scanned, "and nothing forwarding into a dead scheme"
    assert 'data-copy="gleichnass-abc"' in scanned


def test_an_unknown_device_is_still_offered_everything():
    """Guessing wrong should never take an option away."""
    page = web.subscribe_page("t", "Ort", "https://ntfy.sh")
    assert "ntfy://" in page and "data-copy" in page and "https://ntfy.sh/t" in page


def test_a_phone_is_only_shown_the_shop_it_can_install_from():
    assert "apps.apple.com" in web.store_links("ios")
    assert "play.google" not in web.store_links("ios")
    assert "f-droid" in web.store_links("android")
    assert "apps.apple.com" not in web.store_links("android")
    for shop in ("apps.apple.com", "play.google", "f-droid"):
        assert shop in web.store_links("desktop")


def test_the_landing_page_stops_talking_about_scanning_on_a_phone():
    phone = web.landing(platform="ios").decode()
    assert "QR-Code scannen und testen" not in phone
    assert "Abonnieren und testen" in phone
    assert "play.google" not in phone, "including in the install step"

    computer = web.landing(platform="desktop").decode()
    assert "QR-Code scannen und testen" in computer


def test_telegram_is_a_button_on_a_phone_and_a_code_on_a_computer():
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    args = (entry, "topic", "https://x.test/abo/topic", "Konstanz")
    link = "https://t.me/GleichNass_bot?start=ABC123"

    phone = web.welcome(*args, telegram_bot="GleichNass_bot", code="ABC123", platform="ios")
    assert link in phone
    assert 'class="segno"' not in phone

    computer = web.welcome(*args, telegram_bot="GleichNass_bot", code="ABC123")
    assert link in computer
    assert 'class="segno"' in computer


def test_the_handler_asks_the_request_who_is_looking():
    request = FakeRequest({}, {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0)"})
    request._platform = web.Handler._platform.__get__(request)
    assert request._platform() == "ios"


# -- which way in ---------------------------------------------------------


def _checked_ways(page: str) -> list[str]:
    import re

    return [m.group(1) for m in
            re.finditer(r'<input type="radio" name="way" value="(\w+)"[^>]* checked>', page)]


def test_the_first_step_is_a_choice_between_three_ways():
    """Two of them need no app at all, and nobody can guess that from a page
    that only ever mentions ntfy."""
    page = web.landing(telegram_bot="gleichnass_bot").decode()

    for way in web.WAYS:
        assert f'<input type="radio" name="way" value="{way}"' in page
    assert page.count('<input type="radio"') == 3, "one choice, not a pile of checkboxes"
    # The radios live outside the form and are pulled in by the form attribute,
    # so step 1 does not have to wrap the whole of step 2.
    assert page.count('form="anmelden"') == 3


def test_every_way_says_what_it_costs_as_well_as_what_it_gives():
    page = web.landing(telegram_bot="gleichnass_bot").decode()
    assert page.count('class="pro"') >= 6, "two upsides each"
    assert page.count('class="con"') >= 3, "and at least one honest downside"


def test_the_first_way_is_picked_for_anyone_who_never_chooses():
    """No JavaScript, no interaction: the form still submits something valid."""
    page = web.landing().decode()
    assert 'name="way" value="ntfy" id="way-ntfy" form="anmelden" checked>' in page
    assert _checked_ways(page) == ["ntfy"]


def test_a_rejected_form_comes_back_on_the_way_that_was_chosen():
    """Otherwise an error message quietly moves someone back to the app."""
    page = web.landing("Bitte Name und Ort ausfüllen.",
                       telegram_bot="gleichnass_bot", way="telegram").decode()
    assert 'value="telegram" id="way-telegram" form="anmelden" checked>' in page
    assert _checked_ways(page) == ["telegram"]


def test_a_way_we_do_not_offer_falls_back_to_the_first():
    assert web.clean_way("") == "ntfy"
    assert web.clean_way("carrier-pigeon") == "ntfy"
    assert web.clean_way("web") == "web"
    assert web.clean_way("telegram") == "telegram"


def test_telegram_is_refused_rather_than_quietly_swapped():
    """A form saying telegram where no bot exists was forged, not stale, and
    signing that person up for ntfy instead would be a lie."""
    with pytest.raises(ValueError):
        web.clean_way("telegram", telegram=False)


def test_the_browser_way_never_asks_anyone_to_install_anything():
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "gleichnass-abc", "https://x.test/abo/gleichnass-abc",
                       "Konstanz", way="web")

    assert "apps.apple.com" not in page and "play.google" not in page
    assert "https://ntfy.sh/gleichnass-abc" in page, "the web app subscribes on open"
    assert "ntfy://" not in page, "and nothing pointing at an app they did not install"
    assert "Mitteilungen erlauben" in page, "the part that actually decides whether it works"


def test_the_browser_way_says_what_the_iphone_needs():
    """Safari refuses a page notification permission until it has been added to
    the home screen, so leaving that out would just look broken."""
    entry = {"id": "u", "name": "Didi", "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "abc", "https://x.test/abo/abc", "Konstanz",
                       way="web", platform="ios")
    assert "Zum Home-Bildschirm" in page


def test_a_telegram_signup_has_no_ntfy_to_set_up():
    entry = {"id": "u", "name": "Didi", "unsubscribe": "leave-me",
             "rules": [{"preset": "night"}]}
    page = web.welcome(entry, "", "", "Konstanz", telegram_bot="GleichNass_bot",
                       code="ABC123", way="telegram")

    assert "https://t.me/GleichNass_bot?start=ABC123" in page
    assert "ntfy" not in page.lower(), "no app, no topic, no store links"
    assert "<strong>1. Telegram verbinden</strong>" in page, "and it is step one, not three"


def test_the_test_button_proves_itself_with_whatever_secret_exists():
    """The topic is the secret an ntfy signup holds. A Telegram-only one has no
    topic, so it falls back to the token it was given for leaving."""
    entry = {"id": "u", "name": "Didi", "unsubscribe": "leave-me",
             "rules": [{"preset": "night"}]}

    with_topic = web.welcome(entry, "abc", "https://x.test/abo/abc", "Konstanz")
    assert 'name="topic" value="abc"' in with_topic
    assert 'name="token"' not in with_topic

    without = web.welcome(entry, "", "", "Konstanz",
                          telegram_bot="b", code="ABC123", way="telegram")
    assert 'name="token" value="leave-me"' in without


def test_an_unlinked_telegram_signup_is_told_why_nothing_arrived():
    """Nothing sent and nothing failed is not a success, and "Das hat nicht
    geklappt" over an empty list explains nothing."""
    page = web._test_result([], [], waiting_for_telegram=True)
    assert "Noch nichts zu schicken" in page
    assert "Telegram ist noch nicht verbunden" in page


def test_a_working_ntfy_test_is_not_nagged_about_telegram():
    page = web._test_result(["ntfy"], [], waiting_for_telegram=False)
    assert "Telegram" not in page


def test_installing_the_app_is_a_step_of_its_own():
    """The shop buttons used to hang off the end of the choice with no heading
    on them, which read as decoration rather than as something to do."""
    page = web.landing().decode()
    step = page[page.index('id="step-install"'):page.index('id="step-form"')]

    assert "ntfy installieren" in step
    assert "apps.apple.com" in step and "play.google" in step
    assert 'id="app-done"' in step, "and the confirmation that unlocks the form"


def test_the_install_step_belongs_to_the_app_way_alone():
    page = web.landing().decode()
    opening = page[page.index('<li class="flow-step way-then"'):]
    assert opening[:120].count('data-way="ntfy"') == 1, (
        "the whole step is displayed away for the other two ways"
    )


def test_the_step_numbers_are_counted_rather_than_written_down():
    """A step that is not this way's takes its number with it, so writing 1, 2,
    3 into the markup would leave a gap for Telegram and the browser."""
    page = web.landing().decode()
    assert "counter-increment: step" in page
    assert "content: counter(step)" in page
    assert "<b>1</b>" not in page and "<b>2</b>" not in page and "<b>3</b>" not in page
