"""The delivery loop, with the weather and the phone both faked out."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gleichnass import config as config_module
from gleichnass import notify, providers, runner
from gleichnass.notify import build as real_build
from gleichnass.models import Forecast, Location, Slot
from gleichnass.state import Store

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)

CONFIG = """
defaults:
  timezone: Europe/Berlin
  language: en
  providers: [fake]
  channel: {type: ntfy, server: https://ntfy.example}
state_path: state.sqlite3
"""

USER = """
id: didi
name: Didi
location: {lat: 47.66, lon: 9.18, name: Konstanz}
channel: {type: ntfy, topic: secret-topic}
rules:
  - preset: imminent
"""


class Recorder:
    """Stands in for a real channel and remembers what it was asked to send."""

    def __init__(self, kind="recorder"):
        self.type = kind
        self.sent = []

    def describe(self):
        return self.type

    def send(self, client, notification):
        self.sent.append(notification)


def fake_provider(*, mm_at: dict[int, float]):
    """A provider whose forecast is `mm` in the hour starting `offset` hours from NOW."""

    class Fake:
        name = "fake"
        label = "fake"

        def fetch(self, client, location, until):
            slots = [
                Slot(NOW + timedelta(hours=h), NOW + timedelta(hours=h + 1), mm, 100.0)
                for h, mm in sorted(mm_at.items())
            ]
            return Forecast("fake", location, slots, NOW + timedelta(hours=12), "fake")

    return Fake


@pytest.fixture
def setup(tmp_path, monkeypatch):
    (tmp_path / "gleichnass.yaml").write_text(CONFIG)
    (tmp_path / "users.d").mkdir()
    (tmp_path / "users.d" / "didi.yaml").write_text(USER)
    config = config_module.load(tmp_path / "gleichnass.yaml")

    recorder = Recorder()
    monkeypatch.setattr(notify, "build", lambda channel_type, settings: recorder)

    def use(forecast_provider):
        monkeypatch.setattr(providers, "build", lambda name: forecast_provider())

    return config, recorder, use


def run(config, store, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    return runner.run_once(config, store, client, NOW, force=True, **kwargs)


def test_rain_reaches_the_phone_once(setup, tmp_path):
    config, recorder, use = setup
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        first = run(config, store)
        assert [d.reason for d in first] == ["sent"]
        assert len(recorder.sent) == 1
        assert "Rain" in recorder.sent[0].title

        # Same shower, a moment later: nothing further.
        second = run(config, store)
        assert [d.reason for d in second] == ["within cooldown"]
        assert len(recorder.sent) == 1


def test_a_genuinely_new_shower_is_announced(setup, tmp_path):
    config, recorder, use = setup
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        run(config, store)
        # An hour on: past the cooldown, and the rain now in view starts 90
        # minutes after the onset we already announced, so it is a new shower.
        use(fake_provider(mm_at={1.5: 2.0}))
        later = datetime(2026, 8, 17, 13, 0, tzinfo=UTC)
        client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
        decisions = runner.run_once(config, store, client, later, force=True)

    assert [d.reason for d in decisions] == ["sent"]
    assert len(recorder.sent) == 2


def test_dry_weather_stays_silent(setup, tmp_path):
    config, recorder, use = setup
    use(fake_provider(mm_at={0: 0.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        decisions = run(config, store)

    assert [d.reason for d in decisions] == ["dry, staying quiet"]
    assert recorder.sent == []


def test_a_dead_provider_does_not_produce_an_all_clear(setup, tmp_path, monkeypatch):
    """An outage must never be reported to a user as 'no rain'."""
    config, recorder, use = setup

    class Broken:
        name = "fake"

        def fetch(self, client, location, until):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(providers, "build", lambda name: Broken())
    with Store(tmp_path / "state.sqlite3") as store:
        decisions = run(config, store)

    assert [d.reason for d in decisions] == ["no provider data"]
    assert recorder.sent == []


def test_a_failed_delivery_is_retried_next_time(setup, tmp_path, monkeypatch):
    config, recorder, use = setup
    use(fake_provider(mm_at={0: 2.0}))

    class Failing(Recorder):
        def send(self, client, notification):
            raise httpx.HTTPError("ntfy is down")

    monkeypatch.setattr(notify, "build", lambda channel_type, settings: Failing())
    with Store(tmp_path / "state.sqlite3") as store:
        failed = run(config, store)
        assert failed[0].reason == "delivery failed"

        # The shower was never actually announced, so it must not be suppressed.
        monkeypatch.setattr(notify, "build", lambda channel_type, settings: recorder)
        retried = run(config, store)

    assert retried[0].reason == "sent"
    assert len(recorder.sent) == 1


def test_dry_run_sends_nothing_and_remembers_nothing(setup, tmp_path):
    config, recorder, use = setup
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        assert run(config, store, dry_run=True)[0].reason == "would send"
        assert store.get("didi", "imminent").last_notified_at is None
        assert run(config, store)[0].reason == "sent"
    assert len(recorder.sent) == 1


def test_one_request_serves_users_who_share_a_place(setup, tmp_path, monkeypatch):
    config, _, _ = setup
    twin = dict(
        id="anna", location={"lat": 47.66, "lon": 9.18, "name": "Konstanz"},
        channel={"type": "ntfy", "topic": "other"}, rules=[{"preset": "imminent"}],
    )
    config_module.write_user(config.users_dir, twin)
    config = config_module.load(config.path)
    assert len(config.users) == 2

    calls = []

    class Counting:
        name = "fake"

        def fetch(self, client, location, until):
            calls.append(location)
            return Forecast("fake", location, [], NOW + timedelta(hours=2), "fake")

    monkeypatch.setattr(providers, "build", lambda name: Counting())
    with Store(tmp_path / "state.sqlite3") as store:
        run(config, store)

    assert len(calls) == 1, "both users are in Konstanz, so one fetch covers them"


def test_delivery_log_records_what_went_out(setup, tmp_path):
    config, _, use = setup
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        run(config, store)
        rows = store.recent_deliveries()

    assert len(rows) == 1
    assert rows[0]["user_id"] == "didi" and rows[0]["ok"] == 1


TWO_CHANNELS = """
id: didi
name: Didi
location: {lat: 47.66, lon: 9.18, name: Konstanz}
channels:
  - {type: ntfy, topic: secret-topic}
  - {type: telegram, chat_id: 42, token: t}
rules:
  - preset: imminent
"""


def two_channel_setup(config, tmp_path, monkeypatch, failing=()):
    """Reload the user with ntfy and Telegram side by side."""
    (tmp_path / "users.d" / "didi.yaml").write_text(TWO_CHANNELS)
    config = config_module.load(config.path)

    class Failing(Recorder):
        def send(self, client, notification):
            raise httpx.HTTPError("channel is down")

    built = {}

    def build(channel_type, settings):
        if channel_type not in built:
            kind = Failing if channel_type in failing else Recorder
            built[channel_type] = kind(channel_type)
        return built[channel_type]

    monkeypatch.setattr(notify, "build", build)
    return config, built


def test_one_notification_reaches_every_channel(setup, tmp_path, monkeypatch):
    config, _, use = setup
    config, built = two_channel_setup(config, tmp_path, monkeypatch)
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        decisions = run(config, store)
        rows = store.recent_deliveries()

    assert [d.reason for d in decisions] == ["sent"]
    assert len(built["ntfy"].sent) == 1
    assert built["ntfy"].sent[0].title == built["telegram"].sent[0].title
    assert {row["channel"] for row in rows} == {"ntfy", "telegram"}


def test_a_broken_channel_does_not_cost_the_working_one(setup, tmp_path, monkeypatch):
    """Telegram being down must not stop ntfy, nor make ntfy repeat itself."""
    config, _, use = setup
    config, built = two_channel_setup(config, tmp_path, monkeypatch, failing={"telegram"})
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        first = run(config, store)
        second = run(config, store)

    assert first[0].sent and "1 of 2" in first[0].reason
    assert "telegram" in first[0].error
    assert len(built["ntfy"].sent) == 1
    assert second[0].reason == "within cooldown", "the shower is not announced twice"


def test_every_channel_failing_is_a_failed_delivery(setup, tmp_path, monkeypatch):
    config, _, use = setup
    config, _built = two_channel_setup(
        config, tmp_path, monkeypatch, failing={"ntfy", "telegram"}
    )
    use(fake_provider(mm_at={0: 2.0}))

    with Store(tmp_path / "state.sqlite3") as store:
        decisions = run(config, store)
        assert decisions[0].reason == "delivery failed"
        assert store.get("didi", "imminent").last_notified_at is None


def test_a_misconfigured_channel_is_reported_not_fatal(setup, tmp_path, monkeypatch):
    """A Telegram entry with no token must not silence the user's ntfy alerts."""
    config, _, use = setup
    (tmp_path / "users.d" / "didi.yaml").write_text(
        TWO_CHANNELS.replace(", token: t", "")
    )
    config = config_module.load(config.path)
    use(fake_provider(mm_at={0: 2.0}))
    # The real channel classes, so this exercises Telegram's own validation.
    # ntfy's POST lands on the mock transport in run().
    monkeypatch.setattr(notify, "build", real_build)

    with Store(tmp_path / "state.sqlite3") as store:
        decisions = run(config, store)

    assert decisions[0].sent and "1 of 2" in decisions[0].reason
    assert "token" in decisions[0].error
