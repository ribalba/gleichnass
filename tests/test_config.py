import httpx
import pytest

from gleichnass import config as config_module
from gleichnass import signup
from gleichnass.config import ConfigError
from gleichnass.models import Location

BASE = """
defaults:
  timezone: Europe/Berlin
  language: en
  min_mm_per_hour: 0.5
  providers: [icon-d2, brightsky]
  channels:
    ntfy:
      server: https://ntfy.example
      token: ${TEST_NTFY_TOKEN}
    telegram:
      token: ${TEST_TELEGRAM_TOKEN}
"""

USER = """
id: didi
location: {lat: 47.66, lon: 9.18, name: Konstanz}
channel: {topic: abc}
rules: [{preset: night}]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "gleichnass.yaml").write_text(BASE)
    (tmp_path / "users.d").mkdir()
    (tmp_path / "users.d" / "didi.yaml").write_text(USER)
    return tmp_path / "gleichnass.yaml"


def test_users_inherit_the_defaults(project, monkeypatch):
    monkeypatch.setenv("TEST_NTFY_TOKEN", "sekret")
    user = config_module.load(project).user("didi")

    assert user.language == "en"
    assert str(user.zone) == "Europe/Berlin"
    assert user.rules[0].threshold.min_mm_per_hour == 0.5
    assert user.rules[0].providers == ["icon-d2", "brightsky"]
    assert user.channels[0].type == "ntfy", "the default type when none is named"
    assert user.channels[0].settings["server"] == "https://ntfy.example"
    assert user.channels[0].settings["token"] == "sekret", "${VAR} comes from the environment"


def test_an_unset_environment_variable_does_not_become_an_empty_setting(project):
    """`token: ${TEST_NTFY_TOKEN}` with nothing set must not send an empty token."""
    user = config_module.load(project).user("didi")
    assert "token" not in user.channels[0].settings


def test_each_channel_gets_only_its_own_shared_settings(project, monkeypatch):
    """A Telegram user must never inherit the ntfy server as a stray keyword."""
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "bot-token")
    (project.parent / "users.d" / "anna.yaml").write_text(
        "id: anna\n"
        "location: {lat: 53.55, lon: 9.99}\n"
        "channel: {type: telegram, chat_id: 42}\n"
        "rules: [{preset: night}]\n"
    )
    anna = config_module.load(project).user("anna")
    assert anna.channels[0].type == "telegram"
    assert anna.channels[0].settings == {"chat_id": 42, "token": "bot-token"}


def test_ntfy_and_telegram_run_side_by_side(project, monkeypatch):
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "bot-token")
    (project.parent / "users.d" / "anna.yaml").write_text(
        "id: anna\n"
        "location: {lat: 53.55, lon: 9.99}\n"
        "channels:\n"
        "  - {type: ntfy, topic: abc}\n"
        "  - {type: telegram, chat_id: 42}\n"
        "rules: [{preset: night}]\n"
    )
    anna = config_module.load(project).user("anna")

    assert [c.type for c in anna.channels] == ["ntfy", "telegram"]
    assert anna.channels[0].settings["server"] == "https://ntfy.example"
    assert "server" not in anna.channels[1].settings
    assert anna.channels[1].settings["token"] == "bot-token"


def test_an_unknown_channel_is_caught_at_load_not_at_3am(project):
    (project.parent / "users.d" / "anna.yaml").write_text(
        "id: anna\n"
        "location: {lat: 53.55, lon: 9.99}\n"
        "channel: {type: smoke-signal}\n"
        "rules: [{preset: night}]\n"
    )
    with pytest.raises(ConfigError, match="unknown channel"):
        config_module.load(project)


def test_a_user_may_override_any_default(project):
    (project.parent / "users.d" / "didi.yaml").write_text(
        USER + "language: de\ntimezone: Atlantic/Azores\n"
    )
    user = config_module.load(project).user("didi")
    assert user.language == "de"
    assert str(user.zone) == "Atlantic/Azores"


def test_duplicate_ids_are_refused(project):
    (project.parent / "users.d" / "copy.yaml").write_text(USER)
    with pytest.raises(ConfigError, match="duplicate user id"):
        config_module.load(project)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("id: x\nrules: [{preset: night}]\n", "location"),
        ("id: x\nlocation: {lat: 1, lon: 2}\nchannel: {topic: a}\n", "no rules"),
        ("id: x\nlocation: {lat: 1, lon: 2}\nrules: [{preset: night}]\n", "no channel"),
        ("id: x\nlocation: {lat: 1, lon: 2}\nchannel: {topic: a}\n"
         "timezone: Mars/Olympus\nrules: [{preset: night}]\n", "timezone"),
    ],
)
def test_broken_user_files_say_what_is_wrong(project, body, expected):
    (project.parent / "users.d" / "broken.yaml").write_text(body)
    with pytest.raises(ConfigError, match=expected):
        config_module.load(project)


def test_signup_writes_a_user_that_loads_back(project):
    config = config_module.load(project)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    created = signup.create(
        config, name="Anna Müller", location=Location(53.55, 9.99, "Hamburg"),
        presets=["night", "imminent"], client=client,
    )

    assert created.topic.startswith("gleichnass-") and len(created.topic) > 25

    anna = config_module.load(project).user(created.entry["id"])
    assert anna.name == "Anna Müller", "the name is the label, the id is the identity"
    assert anna.channels[0].settings["topic"] == created.topic
    assert [rule.name for rule in anna.rules] == ["night", "imminent"]


def test_two_people_with_the_same_name_stay_separate(project):
    """Two friends called Alex must not end up as "alex" and "alex-2"."""
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    made = [
        signup.create(config_module.load(project), name="Alex",
                      location=Location(47.66, 9.18, "Konstanz"), client=client)
        for _ in range(2)
    ]

    first, second = (m.entry["id"] for m in made)
    assert first != second
    assert made[0].path != made[1].path, "and neither overwrites the other"

    config = config_module.load(project)
    assert [u.name for u in config.users if u.name == "Alex"] == ["Alex", "Alex"]
    assert config.user(first).name == "Alex" and config.user(second).name == "Alex"


def test_an_explicit_id_is_still_allowed_and_must_be_free(project):
    """Handy for the operator's own entry; the fixture already holds "didi"."""
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    created = signup.create(
        config_module.load(project), name="Anna", user_id="anna",
        location=Location(53.55, 9.99, "Hamburg"), client=client,
    )
    assert created.entry["id"] == "anna"

    with pytest.raises(ValueError, match="already taken"):
        signup.create(
            config_module.load(project), name="Someone Else", user_id="didi",
            location=Location(53.55, 9.99, "Hamburg"), client=client,
        )


def test_signup_rejects_an_unknown_mode(project):
    config = config_module.load(project)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    with pytest.raises(ValueError, match="unknown mode"):
        signup.create(
            config, name="Anna", location=Location(1, 2), presets=["whenever"], client=client
        )


def test_shared_channel_settings_are_expanded_too(project, monkeypatch):
    """Not only the per-user copies: anything reading the defaults directly
    would otherwise get a literal "${VAR}"."""
    monkeypatch.setenv("TEST_TELEGRAM_TOKEN", "12345:secret")
    defaults = config_module.load(project).defaults["channels"]

    assert defaults["telegram"]["token"] == "12345:secret"
    assert "${" not in str(defaults)


def test_an_unset_variable_leaves_no_empty_shared_setting(project):
    defaults = config_module.load(project).defaults["channels"]
    assert "token" not in defaults.get("telegram", {})


def test_the_invite_code_can_come_from_the_environment_alone(project, monkeypatch):
    """The generated config keeps that line commented out, so requiring the
    YAML to mention it made the documented variable a no-op."""
    monkeypatch.setenv("GLEICHNASS_INVITE_CODE", "regen2026")
    assert config_module.load(project).signup.invite_code == "regen2026"


def test_the_yaml_still_wins_when_it_names_one(project, monkeypatch):
    monkeypatch.setenv("GLEICHNASS_INVITE_CODE", "from-env")
    project.write_text(BASE + "signup:\n  invite_code: from-yaml\n")
    assert config_module.load(project).signup.invite_code == "from-yaml"


def test_no_code_anywhere_means_open_registration(project):
    assert config_module.load(project).signup.invite_code is None
