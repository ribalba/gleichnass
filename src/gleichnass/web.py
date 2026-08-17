"""The public site: the landing page, and the form that signs people up.

Registration is open by default, because the whole point is that friends add
themselves without anyone editing YAML. Two things keep that from being a
liability: signups are rate limited per address, and an optional invite code
narrows it to people you handed the code to.

Deliberately built on the standard library's HTTP server. This serves a page
and accepts a handful of form posts a day; a framework would be more machinery
than the rest of the project put together. Put it behind a reverse proxy for
TLS.
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, quote, urlparse

import httpx
import segno

from datetime import UTC, datetime, timedelta

from . import config as config_module
from . import geocode, message, notify, providers, signup, telegram_link
from .models import Location
from .rules import PRESETS
from .notify.telegram import bot_username
from .state import Store

log = logging.getLogger("gleichnass.web")

TEMPLATE = Path(__file__).with_name("templates") / "index.html"
STYLE_BLOCK = re.compile(r"<style>.*?</style>", re.S)
TELEGRAM_BLOCK = re.compile(r"[ \t]*<!--telegram:start-->.*?<!--telegram:end-->\n?", re.S)
TEMPLATE_BOT = "gleichnass_bot"
# Long, because places do not move. Not unlimited, so a name that was
# missing from the geocoder one day can turn up later.
PLACE_CACHE_AGE = timedelta(days=90)

# A bot's username never changes, so ask Telegram once per token and keep it.
_BOT_NAMES: dict[str, str | None] = {}
_BOT_LOCK = Lock()


def telegram_bot_for(config) -> str | None:
    """The bot to offer, worked out from the token rather than configured twice.

    Setting the token used not to be enough: the site also wanted the bot's
    name in the config file, which lives inside the deployment's volume, so a
    token set in the environment quietly did nothing.
    """
    if config.signup.telegram_bot:
        return config.signup.telegram_bot

    token = (config.defaults["channels"].get("telegram") or {}).get("token") or ""
    if not token:
        return None

    with _BOT_LOCK:
        if token in _BOT_NAMES:
            return _BOT_NAMES[token]
    try:
        with httpx.Client(timeout=8.0) as client:
            name = bot_username(client, token)
    except Exception as error:  # noqa: BLE001
        log.warning("could not identify the Telegram bot: %s", error)
        name = None
    with _BOT_LOCK:
        _BOT_NAMES[token] = name
    if name:
        log.info("Telegram enabled as @%s", name)
    return name


TOPIC_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def subscribe_page(topic: str, label: str, server: str) -> str:
    """The page the QR code leads to, whose whole job is to open the app.

    Scanning a plain https://ntfy.sh/<topic> link lands in a browser, which is
    not what anyone wants from a QR code. ntfy has a deep link that opens the
    app and subscribes in one step, but it is Android only, so pointing the QR
    straight at it would leave iPhones with a dead scan. Hence this page:
    Android is sent on to the app immediately, everyone else gets the topic and
    somewhere to put it.
    """
    host = server.split("://", 1)[-1].rstrip("/")
    insecure = "&secure=false" if server.startswith("http://") else ""
    deep = f"ntfy://{host}/{topic}?display={quote(label)}{insecure}"
    web_url = f"{server.rstrip('/')}/{topic}"
    return f"""
<h1 style="margin-bottom:.8rem">Abo einrichten</h1>
<p class="lede">Gleich hast du es. Wenn sich die ntfy-App nicht von selbst
   öffnet, hilft einer der Wege hier.</p>

<div class="signup" style="margin-top:1.8rem">
  <a class="btn btn-go" style="justify-content:center" href="{html.escape(deep)}">
    In der ntfy-App öffnen
  </a>
  <div>
    <strong>iPhone, oder es klappt nicht?</strong>
    <p class="privacy" style="margin-top:.3rem">
      ntfy öffnen, auf <em>+</em> tippen und dieses Thema eintragen:
    </p>
    <p style="margin-top:.5rem"><code>{html.escape(topic)}</code></p>
  </div>
  <div>
    <strong>Lieber im Browser?</strong>
    <p class="privacy" style="margin-top:.3rem">
      <a href="{html.escape(web_url)}">{html.escape(web_url)}</a>
    </p>
  </div>
</div>

<script>
  // Only Android registers the ntfy:// scheme. Trying it on iOS raises an
  // "address is invalid" dialog, so there it is left as a button instead.
  if (/Android/i.test(navigator.userAgent)) {{
    location.replace({json.dumps(deep)});
  }}
</script>
"""


def _template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def _shell(title: str, body: str) -> bytes:
    """A standalone page wearing the landing page's own stylesheet, so the
    welcome screen cannot drift away from the site it belongs to."""
    style = STYLE_BLOCK.search(_template())
    return (
        f'<!doctype html><html lang="de"><head><meta charset="utf-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title>{style.group(0) if style else ''}"
        f'</head><body><div class="page" style="padding-top:3rem;padding-bottom:4rem;'
        f'max-width:38rem">{body}</div></body></html>'
    ).encode()


def landing(error: str = "", invite_needed: bool = False,
            telegram_bot: str | None = None) -> bytes:
    """The landing page, with the form's optional parts filled in.

    The template is valid on its own: both hooks are HTML comments, so opening
    the file directly still renders the finished page.
    """
    page = _template()
    if error:
        page = page.replace("<!--error-->", f'<div class="err">{html.escape(error)}</div>')
    # Telegram is in the template so the page reads correctly on its own. Strip
    # it where no bot is configured, rather than offering something that cannot
    # work, and swap in whichever bot this deployment actually runs.
    if telegram_bot:
        if telegram_bot != TEMPLATE_BOT:
            page = TELEGRAM_BLOCK.sub(
                lambda m: m.group(0).replace(TEMPLATE_BOT, html.escape(telegram_bot)), page
            )
    else:
        page = TELEGRAM_BLOCK.sub("", page)
    if invite_needed:
        page = page.replace(
            "<!--invite-->",
            '<div><label class="field" for="code">Einladungscode</label>'
            '<input type="text" id="code" name="code" required autocomplete="off"></div>',
        )
    return page.encode("utf-8")


def welcome(entry: dict, topic: str, topic_url: str, place: str,
            telegram_bot: str | None = None, code: str | None = None,
            leave_url: str = "") -> str:
    qr = segno.make(topic_url, error="m").svg_inline(scale=4, dark="#14213a", light="#ffffff")
    telegram_step = ""
    if telegram_bot and code:
        link = telegram_link.deep_link(telegram_bot, code)
        # A QR as well as a button: this page is usually open on a computer,
        # while Telegram is on the phone. t.me links open the app on both
        # Android and iOS, so unlike the ntfy one this code needs no fallback.
        tg_qr = segno.make(link, error="m").svg_inline(
            scale=4, dark="#14213a", light="#ffffff"
        )
        telegram_step = f"""<div>
    <strong>3. Telegram verbinden</strong>
    <p class="privacy" style="margin-top:.3rem">
      Scanne diesen Code mit dem Handy: Telegram öffnet sich beim Bot und
      schickt deinen Code ab. Wenige Minuten später kommen die Meldungen auch dort an.
    </p>
    <div style="background:#fff;border-radius:14px;padding:1.1rem;margin-top:.7rem;
                display:flex;justify-content:center">
      <div style="width:190px">{tg_qr}</div>
    </div>
    <p style="margin-top:.9rem">
      <a class="btn btn-quiet" href="{html.escape(link)}">Direkt @{html.escape(telegram_bot)} öffnen</a>
    </p>
    <p class="privacy" style="margin-top:.7rem">
      Geht beides nicht, schreibe dem Bot von Hand:
      <code>/start {html.escape(code)}</code>
    </p>
  </div>"""
    modes = "".join(f"<li>{html.escape(describe_rule(rule))}</li>" for rule in entry["rules"])
    leave_note = ""
    if leave_url:
        leave_note = (
            f'<a href="{html.escape(leave_url)}">Wieder abmelden</a> &nbsp;·&nbsp; '
        )
    return f"""
<h1 style="margin-bottom:.8rem">Fast geschafft, {html.escape(entry['name'])}.</h1>
<p class="lede">Noch zwei Handgriffe auf dem Handy, dann meldet sich
   GleichNass für {html.escape(place)} von selbst.</p>

<div class="signup" style="margin-top:2rem">
  <div>
    <strong>1. ntfy installieren</strong>
    <p class="stores">
            <a class="store" href="https://apps.apple.com/us/app/ntfy/id1625396347"><svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M17.05 12.54c-.02-2.3 1.88-3.4 1.96-3.45-1.07-1.56-2.73-1.78-3.32-1.8-1.41-.14-2.76.83-3.48.83-.72 0-1.82-.81-2.99-.79-1.54.02-2.96.9-3.75 2.28-1.6 2.78-.41 6.89 1.15 9.14.76 1.1 1.67 2.34 2.86 2.29 1.15-.05 1.58-.74 2.97-.74 1.39 0 1.78.74 2.99.72 1.23-.02 2.01-1.12 2.76-2.23.87-1.28 1.23-2.52 1.25-2.58-.03-.01-2.39-.92-2.4-3.67Z"/><path d="M14.79 5.6c.63-.77 1.06-1.83.94-2.9-.91.04-2.01.61-2.66 1.37-.58.68-1.09 1.77-.95 2.81 1.01.08 2.05-.51 2.67-1.28Z"/></svg>iPhone</a>
            <a class="store" href="https://play.google.com/store/apps/details?id=io.heckel.ntfy"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 11.5a5 5 0 0 1 10 0"/><path d="M7 11.5h10v5.2a1.3 1.3 0 0 1-1.3 1.3H8.3A1.3 1.3 0 0 1 7 16.7v-5.2Z"/><path d="M8.6 7.2 7.7 5.8M15.4 7.2l.9-1.4"/><circle cx="9.8" cy="9.6" r=".65" fill="currentColor" stroke="none"/><circle cx="14.2" cy="9.6" r=".65" fill="currentColor" stroke="none"/></svg>Android</a>
            <a class="store" href="https://f-droid.org/packages/io.heckel.ntfy/"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.2 4.5 7v10L12 20.8 19.5 17V7L12 3.2Z"/><path d="M4.5 7 12 10.8 19.5 7M12 10.8v10"/></svg>F-Droid</a>
          </p>
  </div>
  <div>
    <strong>2. Code scannen, die App öffnet sich</strong>
    <div style="background:#fff;border-radius:14px;padding:1.1rem;margin-top:.7rem;
                display:flex;justify-content:center">
      <div style="width:190px">{qr}</div>
    </div>
    <p class="privacy" style="margin-top:.7rem">
      Auf Android landest du direkt in ntfy und bist abonniert. Auf dem iPhone
      öffnet sich eine Seite mit deinem Thema, das du in der App einträgst:
      <code>{html.escape(topic)}</code>.
      Wer diesen Namen kennt, sieht deine Meldungen, also bitte nicht öffentlich teilen.
    </p>
  </div>
  <div>
    <strong>Du bekommst</strong>
    <ul style="margin:.4rem 0 0;padding-left:1.1rem;color:var(--muted)">{modes}</ul>
  </div>
  {telegram_step}
  <form method="post" action="/test">
    <input type="hidden" name="user" value="{html.escape(entry['id'])}">
    <input type="hidden" name="topic" value="{html.escape(topic)}">
    <button class="btn btn-go" type="submit" style="width:100%;justify-content:center">
      Testnachricht schicken
    </button>
  </form>
</div>

<p class="note" style="margin-top:1.5rem">{leave_note}<a href="/">Zurück zur Startseite</a></p>
"""


_WINDOW_WORDS = {"30m": "30 Minuten", "1h": "einer Stunde", "2h": "zwei Stunden",
                 "3h": "drei Stunden"}


def _picked_location(form: dict, place: str) -> Location | None:
    """Coordinates from a suggestion the visitor actually clicked.

    Only trusted when they are well formed; anything else falls back to looking
    the typed text up, so a mangled field cannot put someone in the sea.
    """
    try:
        lat = float(form.get("lat", [""])[0])
        lon = float(form.get("lon", [""])[0])
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return Location(lat=lat, lon=lon, name=place or None)


def _forecast_reaches(client, config, location: Location) -> bool:
    """Ask a provider for real numbers before promising anyone notifications.

    Geocoding will happily return a place the weather APIs have nothing for.
    Better to say so on the form than to sign someone up for silence.
    """
    until = datetime.now(UTC) + timedelta(hours=6)
    for name in config.defaults["providers"]:
        try:
            forecast = providers.build(name).fetch(client, location, until)
        except Exception as error:  # noqa: BLE001
            log.info("probe %s failed for %s: %s", name, location, error)
            continue
        if forecast.slots:
            return True
    return False


def describe_rule(rule: dict, language: str = "de") -> str:
    """The rule as a sentence, worded once in message.py and reused here."""
    return message.describe(rule.get("preset", ""), rule.get("at"), language)


class BadRequest(Exception):
    """A malformed request, answered with a 400 rather than a traceback."""


def _same_secret(given: str, expected: str) -> bool:
    """Constant-time compare that survives an invite code with umlauts in it."""
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class RateLimit:
    """Signups per address per hour. Open registration needs a cheap floor."""

    def __init__(self, per_hour: int):
        self.per_hour = per_hour
        self._seen: dict[str, deque[float]] = {}
        self._lock = Lock()

    def allow(self, who: str, now: float | None = None) -> bool:
        if self.per_hour <= 0:
            return True
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._seen.setdefault(who, deque())
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if len(hits) >= self.per_hour:
                return False
            hits.append(now)
            # Keep the table from growing without bound on a busy day.
            if len(self._seen) > 4096:
                for key in [k for k, v in self._seen.items() if not v][:2048]:
                    self._seen.pop(key, None)
            return True


class Handler(BaseHTTPRequestHandler):
    server_version = "gleichnass"
    settings: dict = {}
    # Without this a client can open a connection, send nothing, and hold a
    # thread for as long as it likes.
    timeout = 20

    def log_message(self, fmt, *args):
        log.info("%s %s", self._client(), fmt % args)

    def _client(self) -> str:
        """The address to hold responsible, which is not always the peer.

        Behind a reverse proxy every request arrives from the proxy, so keying
        rate limits on the peer would put the whole internet in one bucket. The
        proxy appends the address it saw to X-Forwarded-For, so the rightmost
        entry is the one a client cannot forge. Only trusted when the operator
        says there really is a proxy in front, because otherwise the header is
        just something the client made up.
        """
        if self.settings.get("trust_proxy"):
            forwarded = self.headers.get("X-Forwarded-For", "")
            if forwarded.strip():
                return forwarded.split(",")[-1].strip()
        return self.client_address[0]

    # -- routing ---------------------------------------------------------

    def do_GET(self):  # noqa: N802, name fixed by the stdlib
        route = urlparse(self.path).path
        if route == "/":
            config = self._config()
            self._send(200, landing(
                invite_needed=bool(config.signup.invite_code),
                telegram_bot=telegram_bot_for(config),
            ))
        elif route == "/places":
            if not self.settings["browse"].allow(self._client()):
                return self._send(429, b"[]", "application/json; charset=utf-8")
            self._places(parse_qs(urlparse(self.path).query).get("q", [""])[0])
        elif route == "/abbestellen":
            self._leave_form(parse_qs(urlparse(self.path).query))
        elif route.startswith("/abo/"):
            self._subscribe(route[len("/abo/"):], parse_qs(urlparse(self.path).query))
        elif route == "/healthz":
            self._send(200, b"ok", "text/plain; charset=utf-8")
        else:
            self._not_found()

    def do_POST(self):  # noqa: N802
        route = urlparse(self.path).path
        try:
            if route == "/signup":
                self._signup()
            elif route == "/test":
                self._test()
            elif route == "/abbestellen":
                self._leave()
            else:
                self._not_found()
        except BadRequest as error:
            self._send(400, _shell("Ungültige Anfrage", f"<h1>{html.escape(str(error))}</h1>"))

    # -- actions ---------------------------------------------------------

    def _subscribe(self, topic: str, query: dict):
        """Where the QR code points. Opens the app rather than a web page."""
        if not TOPIC_OK.match(topic):
            return self._not_found()
        config = self._config()
        server = str(
            (config.defaults["channels"].get("ntfy") or {}).get("server") or "https://ntfy.sh"
        ).rstrip("/")
        label = (query.get("ort", [""])[0] or "GleichNass")[:40]
        self._send(200, _shell("Abo einrichten", subscribe_page(topic, label, server)))

    def _leaving(self, query: dict):
        """Whoever this link belongs to, if the token really is theirs."""
        config = self._config()
        try:
            user = config.user(query.get("u", [""])[0])
        except KeyError:
            return None, config
        token = query.get("t", [""])[0]
        if not user.unsubscribe or not _same_secret(token, user.unsubscribe):
            return None, config
        return user, config

    def _leave_form(self, query: dict):
        """A GET only asks. Mail clients and chat apps follow links to preview
        them, and a link that deletes on sight would go off by itself."""
        user, _ = self._leaving(query)
        if user is None:
            return self._not_found()
        fields = "".join(
            f'<input type="hidden" name="{k}" value="{html.escape(query[k][0])}">'
            for k in ("u", "t")
        )
        self._send(200, _shell("Abmelden", f"""
<h1 style="margin-bottom:.8rem">Abmelden?</h1>
<p class="lede">Dann hört GleichNass für {html.escape(user.name)} auf, sich zu
   melden, und alles Gespeicherte wird gelöscht.</p>
<form method="post" action="/abbestellen" class="signup" style="margin-top:1.8rem">
  {fields}
  <button class="btn btn-go" type="submit" style="justify-content:center">
    Ja, abmelden
  </button>
  <p class="privacy">Du kannst dich jederzeit wieder anmelden.</p>
</form>
<p class="note" style="margin-top:1.5rem"><a href="/">Doch nicht</a></p>"""))

    def _leave(self):
        user, config = self._leaving(self._form())
        if user is None:
            return self._not_found()

        config_module.delete_user(config, user)
        with Store(config.state_path) as store:
            store.forget(user.id)
        log.info("removed %s on their own request", user.id)
        self._send(200, _shell("Abgemeldet", """
<h1 style="margin-bottom:.8rem">Erledigt.</h1>
<p class="lede">Du bekommst keine Meldungen mehr, und deine Daten sind gelöscht.
   In der ntfy-App kannst du das Thema jetzt auch dort entfernen.</p>
<p class="note" style="margin-top:1.5rem"><a href="/">Zur Startseite</a></p>"""))

    def _base_url(self, config) -> str:
        """Where this site is reachable, for the URL inside the QR code."""
        if config.signup.base_url:
            return config.signup.base_url.rstrip("/")
        host = self.headers.get("Host", "")
        scheme = "http"
        if self.settings.get("trust_proxy"):
            scheme = self.headers.get("X-Forwarded-Proto", "https").split(",")[0].strip()
        return f"{scheme}://{host}"

    def _places(self, query: str):
        """Place suggestions, proxied so the page never calls anyone else, and
        cached because a town's coordinates are not going to change.

        A new connection per request: this runs in a worker thread and SQLite
        connections belong to the thread that opened them.
        """
        query = query[:80]
        config = self._config()
        results = None

        try:
            with Store(config.state_path) as store:
                results = store.cached_places(query, PLACE_CACHE_AGE)
                if results is None:
                    results = self._geocode(query)
                    store.remember_places(query, results)
        except Exception as error:  # noqa: BLE001 - suggestions are a nicety
            log.warning("place cache unavailable (%s), asking the geocoder", error)
            if results is None:
                results = self._geocode(query)

        self._send(200, json.dumps(results).encode(), "application/json; charset=utf-8")

    def _geocode(self, query: str) -> list[dict]:
        try:
            with httpx.Client(timeout=8.0) as client:
                found = geocode.search(client, query)
        except Exception as error:  # noqa: BLE001 - an empty list is a fine answer
            log.warning("place lookup for %r failed: %s", query, error)
            return []
        return [{"name": p.name, "lat": p.lat, "lon": p.lon} for p in found]

    def _signup(self):
        config = self._config()
        if not config.signup.enabled:
            return self._send(403, _shell("Geschlossen", "<h1>Anmeldung ist geschlossen.</h1>"))

        # Counted before anything else, so a wrong invite code costs an attempt
        # and the code cannot be guessed at leisure.
        if not self.settings["limit"].allow(self._client()):
            log.warning("rate limited signup from %s", self._client())
            return self._reject(
                config, "Zu viele Anmeldungen von hier. Bitte später noch einmal versuchen."
            )

        form = self._form()
        invite = config.signup.invite_code
        if invite and not _same_secret(form.get("code", [""])[0], invite):
            return self._reject(config, "Der Einladungscode stimmt nicht.")

        name = (form.get("name", [""])[0] or "").strip()[:60]
        place = (form.get("place", [""])[0] or "").strip()[:80]
        modes = [m for m in form.get("modes", []) if m in PRESETS]
        if not name or not place:
            return self._reject(config, "Bitte Name und Ort ausfüllen.")
        if not modes:
            return self._reject(config, "Bitte mindestens eine Meldung auswählen.")

        # Each mode carries its own time, so people are not stuck with 20:00.
        options = {
            "night": (form.get("at_night", [""])[0] or "").strip(),
            "morning": (form.get("at_morning", [""])[0] or "").strip(),
            "imminent": (form.get("window_imminent", [""])[0] or "").strip(),
        }
        for preset in modes:
            try:
                signup.rule_entry(preset, options.get(preset) or None)
            except ValueError:
                return self._reject(config, (
                    "Die Vorlaufzeit muss zwischen 15 Minuten und 12 Stunden liegen."
                    if preset == "imminent"
                    else "Bitte eine gültige Uhrzeit angeben."
                ))

        bot = telegram_bot_for(config)
        wants_telegram = bool(form.get("telegram")) and bool(bot)
        code = telegram_link.new_code() if wants_telegram else None
        picked = _picked_location(form, place)

        try:
            with httpx.Client(timeout=20.0) as client:
                location = picked or geocode.lookup(client, place)
                if not _forecast_reaches(client, config, location):
                    return self._reject(config, (
                        f"Für „{location.name or place}“ bekomme ich gerade keine "
                        "Wetterdaten. Bitte einen Ort aus der Vorschlagsliste wählen."
                    ))
                created = signup.create(
                    config, name=name, location=location, place=place, presets=modes,
                    options=options, telegram_code=code, client=client,
                )
        except LookupError:
            return self._reject(config, f"Den Ort „{place}“ konnte ich nicht finden.")
        except Exception as error:  # noqa: BLE001
            log.exception("signup failed")
            return self._reject(config, f"Das hat leider nicht geklappt: {error}")

        log.info("signed up %s (%s)", created.entry["id"], created.path)
        server = str(
            config.defaults["channels"].get("ntfy", {}).get("server") or "https://ntfy.sh"
        ).rstrip("/")
        # The QR points at our own /abo page, which forwards into the app.
        where = created.location.name or place
        qr_target = f"{self._base_url(config)}/abo/{created.topic}?ort={quote(where[:40])}"
        self._send(
            200,
            _shell(
                "Fast geschafft",
                welcome(created.entry, created.topic, qr_target, where,
                        telegram_bot=bot, code=code,
                        leave_url=f"{self._base_url(config)}/abbestellen"
                                  f"?u={quote(created.entry['id'])}"
                                  f"&t={quote(created.entry['unsubscribe'])}"),
            ),
        )

    def _test(self):
        config = self._config()
        form = self._form()
        if not self.settings["browse"].allow(self._client()):
            return self._send(429, _shell("Zu viel", "<h1>Zu viele Versuche.</h1>"))

        try:
            user = config.user(form.get("user", [""])[0])
        except KeyError:
            return self._not_found()

        # User ids are people's first names, so knowing one proves nothing.
        # The topic is the secret they were just given, and anyone who has it
        # could publish to that topic directly anyway, so it is the right key
        # for "yes, this really is your own signup".
        topics = [
            c.settings.get("topic", "") for c in user.channels
            if c.type == "ntfy" and c.settings.get("topic")
        ]
        offered = form.get("topic", [""])[0]
        if not topics or not any(_same_secret(offered, t) for t in topics):
            log.warning("rejected /test for %s from %s", user.id, self._client())
            return self._send(403, _shell("Nicht erlaubt", "<h1>Nicht erlaubt.</h1>"))

        try:
            note = message.test_notification(user)
            with httpx.Client(timeout=15.0) as client:
                for spec in user.channels:
                    notify.build(spec.type, spec.settings).send(client, note)
        except Exception as error:  # noqa: BLE001
            log.error("test notification for %s failed: %s", user.id, error)
            return self._send(
                200,
                _shell(
                    "Test fehlgeschlagen",
                    "<h1>Das hat nicht geklappt.</h1>"
                    f'<p class="lede">{html.escape(str(error))}</p>'
                    '<p class="note"><a href="/">Zurück</a></p>',
                ),
            )
        self._send(
            200,
            _shell(
                "Test unterwegs",
                "<h1>Testnachricht ist unterwegs.</h1>"
                '<p class="lede">Kommt sie nicht an, prüfe, ob du das Thema in der '
                "ntfy-App abonniert hast.</p>"
                '<p class="note"><a href="/">Zurück zur Startseite</a></p>',
            ),
        )

    # -- plumbing --------------------------------------------------------

    def _config(self):
        # Re-read per request: a signup from another worker, or a hand edit,
        # should be visible at once, and this is a few requests a day.
        return config_module.load(self.settings["config_path"])

    def _form(self) -> dict[str, list[str]]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 64 * 1024:
            raise BadRequest("form missing or too large")
        return parse_qs(self.rfile.read(length).decode("utf-8", "replace"))

    def _reject(self, config, error: str):
        self._send(400, landing(error, bool(config.signup.invite_code),
                                telegram_bot_for(config)))

    def _not_found(self):
        self._send(404, _shell("Nicht gefunden", '<h1>Nicht gefunden</h1>'
                               '<p class="note"><a href="/">Zur Startseite</a></p>'))

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


def serve(config_path, host: str = "127.0.0.1", port: int = 8080, per_hour: int = 8,
          trust_proxy: bool = False) -> None:
    handler = type("BoundHandler", (Handler,), {
        "settings": {
            "config_path": config_path,
            "limit": RateLimit(per_hour),
            # Looking around is cheap but not free: it proxies the geocoder and
            # can fire notifications, so it gets a looser ceiling of its own.
            "browse": RateLimit(per_hour * 15),
            "trust_proxy": trust_proxy,
        },
    })
    server = ThreadingHTTPServer((host, port), handler)
    log.info("site on http://%s:%s (config %s)", host, port, config_path)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
