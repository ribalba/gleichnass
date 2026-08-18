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
FAVICON = Path(__file__).with_name("templates") / "favicon.svg"
OG_IMAGE = Path(__file__).with_name("templates") / "og.png"
# Inlined so a page built here needs no second request to show it.
ICON_TAG = '<link rel="icon" href="data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAzMiAzMiI+PHJlY3Qgd2lkdGg9IjMyIiBoZWlnaHQ9IjMyIiByeD0iNyIgZmlsbD0iIzFhNWZkMCIvPjxwYXRoIGQ9Ik0xNiA1LjVjNC4yIDUuOCA2LjQgOS43IDYuNCAxMi4zYTYuNCA2LjQgMCAxIDEtMTIuOCAwQzkuNiAxNS4yIDExLjggMTEuMyAxNiA1LjVaIiBmaWxsPSIjZmZmZmZmIi8+PC9zdmc+">'
STYLE_BLOCK = re.compile(r"<style>.*?</style>", re.S)
TELEGRAM_BLOCK = re.compile(r"[ \t]*<!--telegram:start-->.*?<!--telegram:end-->\n?", re.S)
STORES_BLOCK = re.compile(r"<!--stores:start-->.*?<!--stores:end-->", re.S)
STEP3_BLOCK = re.compile(r"<!--step3:start-->.*?<!--step3:end-->", re.S)
WAY_INPUT = re.compile(r'(<input type="radio" name="way" value=")([a-z]+)("[^>]*?)( checked)?(>)')
TEMPLATE_BOT = "gleichnass_bot"
# Long, because places do not move. Not unlimited, so a name that was
# missing from the geocoder one day can turn up later.
PLACE_CACHE_AGE = timedelta(days=90)

# How someone wants the alerts to reach them, chosen as the first step of the
# flow. "ntfy" and "web" are the same channel - the app and ntfy's own web app
# both listen on one topic - and differ only in what the pages after signup
# tell you to do with it. "telegram" is a channel of its own, and the only one
# that cannot be set up before the person has messaged the bot.
WAYS = ("ntfy", "telegram", "web")
DEFAULT_WAY = "ntfy"


def clean_way(value: str, telegram: bool = True) -> str:
    """The way a visitor picked, or the default if the form said something we
    do not offer. Telegram is refused rather than defaulted when no bot is
    configured, since that is a forged form, not a stale one."""
    way = (value or "").strip()
    if way == "telegram" and not telegram:
        raise ValueError("telegram is not set up here")
    return way if way in WAYS else DEFAULT_WAY


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

# -- who is looking ------------------------------------------------------

_UA_IOS = re.compile(r"iPhone|iPad|iPod", re.I)
_UA_ANDROID = re.compile(r"Android", re.I)
_UA_MOBILE = re.compile(r"Mobi|Android|iPhone|iPad|iPod|Windows Phone|IEMobile", re.I)


def platform_of(user_agent: str) -> str:
    """Which sort of device is asking: ios, android, mobile or desktop.

    A QR code moves something from one screen to another and is useless when
    both are the same screen, so a phone gets buttons instead. Sniffing the
    user agent is crude, and an iPad in its default desktop guise says
    "Macintosh" and lands here as a computer. That is why every page keeps a
    direct link next to the code: the worst a wrong guess costs is a QR nobody
    scans, never a dead end.
    """
    agent = user_agent or ""
    if _UA_IOS.search(agent):
        return "ios"
    if _UA_ANDROID.search(agent):
        return "android"
    if _UA_MOBILE.search(agent):
        return "mobile"
    return "desktop"


# -- the pieces the pages are built from ---------------------------------

STORE_IOS = ('<a class="store" href="https://apps.apple.com/us/app/ntfy/id1625396347">'
             '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor" '
             'aria-hidden="true"><path d="M17.05 12.54c-.02-2.3 1.88-3.4 1.96-3.45-1.07-1.56'
             '-2.73-1.78-3.32-1.8-1.41-.14-2.76.83-3.48.83-.72 0-1.82-.81-2.99-.79-1.54.02'
             '-2.96.9-3.75 2.28-1.6 2.78-.41 6.89 1.15 9.14.76 1.1 1.67 2.34 2.86 2.29 1.15'
             '-.05 1.58-.74 2.97-.74 1.39 0 1.78.74 2.99.72 1.23-.02 2.01-1.12 2.76-2.23.87'
             '-1.28 1.23-2.52 1.25-2.58-.03-.01-2.39-.92-2.4-3.67Z"/><path d="M14.79 5.6c.63'
             '-.77 1.06-1.83.94-2.9-.91.04-2.01.61-2.66 1.37-.58.68-1.09 1.77-.95 2.81 1.01'
             '.08 2.05-.51 2.67-1.28Z"/></svg>iPhone</a>')
STORE_PLAY = ('<a class="store" '
              'href="https://play.google.com/store/apps/details?id=io.heckel.ntfy">'
              '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
              'stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" '
              'aria-hidden="true"><path d="M7 11.5a5 5 0 0 1 10 0"/><path d="M7 11.5h10v5.2a1.3 '
              '1.3 0 0 1-1.3 1.3H8.3A1.3 1.3 0 0 1 7 16.7v-5.2Z"/><path d="M8.6 7.2 7.7 '
              '5.8M15.4 7.2l.9-1.4"/><circle cx="9.8" cy="9.6" r=".65" fill="currentColor" '
              'stroke="none"/><circle cx="14.2" cy="9.6" r=".65" fill="currentColor" '
              'stroke="none"/></svg>Android</a>')
STORE_FDROID = ('<a class="store" href="https://f-droid.org/packages/io.heckel.ntfy/">'
                '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" '
                'stroke="currentColor" stroke-width="1.8" stroke-linejoin="round" '
                'aria-hidden="true"><path d="M12 3.2 4.5 7v10L12 20.8 19.5 17V7L12 3.2Z"/>'
                '<path d="M4.5 7 12 10.8 19.5 7M12 10.8v10"/></svg>F-Droid</a>')


def store_links(platform: str = "desktop") -> str:
    """Only the shops this device can actually install from, so a phone is not
    asked to pick its own operating system out of a row of three."""
    if platform == "ios":
        return STORE_IOS
    if platform == "android":
        return STORE_PLAY + "\n" + STORE_FDROID
    return STORE_IOS + "\n" + STORE_PLAY + "\n" + STORE_FDROID


def deep_link(topic: str, label: str, server: str) -> str:
    """ntfy's own link into the app. Android only; see subscribe_page."""
    host = server.split("://", 1)[-1].rstrip("/")
    insecure = "&secure=false" if server.startswith("http://") else ""
    return f"ntfy://{host}/{topic}?display={quote(label)}{insecure}"


# Written as a listener on the page rather than an onclick, so the topic
# travels in an attribute and never has to survive quoting into JavaScript.
COPY_JS = """
<script>
  document.addEventListener("click", function (event) {
    var button = event.target.closest && event.target.closest("[data-copy]");
    if (!button) { return; }
    var text = button.getAttribute("data-copy");
    var before = button.textContent;
    function said(message) {
      button.textContent = message;
      setTimeout(function () { button.textContent = before; }, 5000);
    }
    // Without a secure context there is no clipboard API, so the topic is
    // selected instead and a long press offers "Kopieren".
    function select() {
      var shown = document.getElementById(button.getAttribute("data-shows") || "");
      if (!shown) { return said("Kopieren geht hier nicht"); }
      var range = document.createRange();
      range.selectNodeContents(shown);
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      said("Markiert \u2013 lange dr\u00fccken, dann \u201eKopieren\u201c");
    }
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(function () { said("Kopiert \u2713"); }, select);
    } else {
      select();
    }
  });
</script>
"""


def copy_button(topic: str, label: str = "Thema kopieren",
                quiet: bool = False, shows: str = "topic-name") -> str:
    kind = "btn-quiet" if quiet else "btn-go"
    return (f'<button class="btn {kind} btn-block" type="button" '
            f'data-copy="{html.escape(topic, quote=True)}" data-shows="{shows}">'
            f"{html.escape(label)}</button>")


def _topic_note(topic: str) -> str:
    return (f'Dein Thema: <code id="topic-name">{html.escape(topic)}</code>. '
            "Wer diesen Namen kennt, sieht deine Meldungen, also bitte nicht "
            "öffentlich teilen.")


def subscribe_page(topic: str, label: str, server: str, platform: str = "") -> str:
    """The page the QR code leads to, whose whole job is to open the app.

    Scanning a plain https://ntfy.sh/<topic> link lands in a browser, which is
    not what anyone wants from a QR code. ntfy has a deep link that opens the
    app and subscribes in one step, but it is Android only: the iOS app
    registers no URL scheme and claims no https address either, so nothing can
    hand it a topic. Android is therefore sent on to the app immediately, and
    an iPhone gets the shortest route that does exist - one tap to the
    clipboard, or the web app, which subscribes on its own when opened.
    """
    deep = deep_link(topic, label, server)
    web_url = f"{server.rstrip('/')}/{topic}"
    # An unknown device still gets everything, since guessing wrong should not
    # take an option away.
    app_button = "" if platform == "ios" else f"""
  <a class="btn btn-go btn-block" href="{html.escape(deep)}">
    In der ntfy-App öffnen
  </a>"""
    forward = "" if platform == "ios" else f"""
<script>
  // Only Android registers the ntfy:// scheme. Trying it on iOS raises an
  // "address is invalid" dialog, so there it is left out entirely.
  if (/Android/i.test(navigator.userAgent)) {{
    location.replace({json.dumps(deep)});
  }}
</script>"""
    lede = ("Auf dem iPhone kann keine Seite die ntfy-App füttern. Zwei Wege "
            "bleiben, beide ohne Tippen."
            if platform == "ios" else
            "Gleich hast du es. Wenn sich die ntfy-App nicht von selbst "
            "öffnet, hilft einer der Wege hier.")
    return f"""
<h1 style="margin-bottom:.8rem">Abo einrichten</h1>
<p class="lede">{lede}</p>

<div class="signup" style="margin-top:1.8rem">{app_button}
  <div>
    <strong>Thema kopieren, in ntfy einfügen</strong>
    <p class="privacy" style="margin-top:.3rem">
      Tippe auf den Knopf, öffne ntfy, tippe auf <em>+</em> und füge den Namen
      ins Feld „Thema“ ein.
    </p>
    <p style="margin-top:.8rem">{copy_button(topic)}</p>
    <p class="privacy" style="margin-top:.7rem">{_topic_note(topic)}</p>
  </div>
  <div>
    <strong>Oder ganz ohne App</strong>
    <p class="privacy" style="margin-top:.3rem">
      <a href="{html.escape(web_url)}">{html.escape(web_url)}</a> abonniert dich
      beim Öffnen von selbst. Damit die Meldungen auch bei geschlossenem
      Browser ankommen, die Seite über „Teilen“ zum Home-Bildschirm hinzufügen
      und dort Mitteilungen erlauben.
    </p>
  </div>
</div>
{COPY_JS}{forward}
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
        f"{ICON_TAG}"
        f"<title>{html.escape(title)}</title>{style.group(0) if style else ''}"
        f'</head><body><div class="page" style="padding-top:3rem;padding-bottom:4rem;'
        f'max-width:38rem">{body}</div></body></html>'
    ).encode()


def _check_way(page: str, way: str) -> str:
    """Move the tick to the way that was picked.

    Only matters when the form comes back with an error on it: the page is
    rendered again from the template, and someone who chose Telegram should not
    find themselves back on the app.
    """
    def swap(match):
        picked = " checked" if match.group(2) == way else ""
        return match.group(1) + match.group(2) + match.group(3) + picked + match.group(5)

    return WAY_INPUT.sub(swap, page)


def landing(error: str = "", invite_needed: bool = False,
            telegram_bot: str | None = None, base_url: str = "",
            platform: str = "desktop", way: str = DEFAULT_WAY) -> bytes:
    """The landing page, with the form's optional parts filled in.

    The template is valid on its own: every hook is an HTML comment, so opening
    the file directly still renders the finished page - on a computer, which is
    what the wording assumes and what a phone gets swapped out here.
    """
    page = _template()
    if way != DEFAULT_WAY:
        page = _check_way(page, way)
    if platform != "desktop":
        page = STORES_BLOCK.sub(store_links(platform), page)
        page = STEP3_BLOCK.sub(
            "<h3>Abonnieren und testen</h3>"
            "<p>Gleich nach dem Anmelden bekommst du einen Knopf, der dich "
            "abonniert. Testnachricht schicken, fertig. Ab dann meldet sich "
            "GleichNass von selbst.</p>",
            page,
        )
    if base_url:
        # Crawlers for chat previews generally will not follow a relative
        # og:image, so give them the whole address.
        base = base_url.rstrip("/")
        page = page.replace('property="og:url" content="/"',
                            f'property="og:url" content="{html.escape(base)}/"')
        page = page.replace('property="og:image" content="/og.png"',
                            f'property="og:image" content="{html.escape(base)}/og.png"')
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


def _qr(target: str) -> str:
    """A code, on a white card so it scans in the dark theme too."""
    code = segno.make(target, error="m").svg_inline(scale=4, dark="#14213a", light="#ffffff")
    return (f'<div style="background:#fff;border-radius:14px;padding:1.1rem;margin-top:.7rem;'
            f'display:flex;justify-content:center"><div style="width:190px">{code}</div></div>')


def _ntfy_step(topic: str, abo_url: str, server: str, platform: str, place: str,
               telegram: bool = False, n: int = 2) -> str:
    """The topic, into the app, by whichever route this device has."""
    if platform == "ios":
        # No page can hand a topic to the iOS app - it registers no ntfy://
        # scheme and claims no https address - so the clipboard is the shortest
        # honest route, with the web app for anyone who would rather not paste.
        web_url = f"{server.rstrip('/')}/{topic}"
        instead = ('<p class="privacy" style="margin-top:.7rem">Zu umständlich? '
                   "Auf dem iPhone ist Telegram der kürzere Weg, dafür "
                   "genügt der nächste Schritt.</p>") if telegram else ""
        return f"""<div>
    <strong>{n}. Thema in ntfy eintragen</strong>
    <p class="privacy" style="margin-top:.3rem">
      Tippe auf den Knopf, öffne ntfy, tippe auf <em>+</em> und füge den Namen
      ins Feld „Thema“ ein. Sonst nichts ändern.
    </p>
    <p style="margin-top:.8rem">{copy_button(topic)}</p>
    <p class="privacy" style="margin-top:.7rem">{_topic_note(topic)}</p>
    <p class="privacy" style="margin-top:.7rem">
      Ohne App geht es auch: <a href="{html.escape(web_url)}">im Browser abonnieren</a>.
      Die Seite abonniert dich beim Öffnen selbst; über „Teilen → Zum
      Home-Bildschirm“ kommen die Meldungen auch dort als Mitteilung an.
    </p>
    {instead}
  </div>"""
    if platform in ("android", "mobile"):
        deep = deep_link(topic, place[:40] or "GleichNass", server)
        return f"""<div>
    <strong>{n}. Thema abonnieren</strong>
    <p class="privacy" style="margin-top:.3rem">
      Einmal tippen: ntfy öffnet sich und ist danach abonniert.
    </p>
    <p style="margin-top:.8rem">
      <a class="btn btn-go btn-block" href="{html.escape(deep)}">In der ntfy-App abonnieren</a>
    </p>
    <p class="privacy" style="margin-top:.7rem">
      Passiert nichts, fehlt noch die App. Oder trage das Thema von Hand ein:
    </p>
    <p style="margin-top:.6rem">{copy_button(topic, quiet=True)}</p>
    <p class="privacy" style="margin-top:.7rem">{_topic_note(topic)}</p>
  </div>"""
    return f"""<div>
    <strong>{n}. Code scannen, die App öffnet sich</strong>
    {_qr(abo_url)}
    <p class="privacy" style="margin-top:.7rem">
      Auf Android landest du direkt in ntfy und bist abonniert. Auf dem iPhone
      öffnet sich eine Seite, die dir das Thema in die Zwischenablage legt.
      {_topic_note(topic)}
    </p>
    <p class="privacy" style="margin-top:.7rem">
      Liest du das schon auf dem Handy?
      <a href="{html.escape(abo_url)}">Dann hier entlang</a>.
    </p>
  </div>"""


def _webapp_steps(topic: str, server: str, platform: str) -> str:
    """The way in that installs nothing: ntfy's own web app.

    Opening https://ntfy.sh/<topic> subscribes the browser to that topic on its
    own, which is the whole of the first step. The second step is the honest
    part - a plain tab keeps receiving only for as long as the browser feels
    like it, and on iOS a page is refused notification permission entirely
    until it has been added to the home screen.
    """
    url = f"{server.rstrip('/')}/{topic}"
    if platform == "desktop":
        open_it = f"""{_qr(url)}
    <p class="privacy" style="margin-top:.7rem">
      Scanne den Code mit dem Handy, oder öffne
      <a href="{html.escape(url)}">{html.escape(url)}</a> gleich hier.
    </p>"""
    else:
        open_it = f"""<p style="margin-top:.8rem">
      <a class="btn btn-go btn-block" href="{html.escape(url)}">Im Browser abonnieren</a>
    </p>"""
    if platform == "ios":
        pin = ("Tippe unten auf „Teilen“, dann auf „Zum Home-Bildschirm“. Öffne "
               "die Seite einmal von dort und erlaube Mitteilungen. Ohne diesen "
               "Schritt darf dir eine Seite auf dem iPhone nichts schicken.")
    else:
        pin = ("Erlaube der Seite Mitteilungen, wenn sie danach fragt. Über das "
               "Browser-Menü „Zum Startbildschirm hinzufügen“ bleibt das Abo "
               "auch dann bestehen, wenn du den Tab schließt.")
    return f"""<div>
    <strong>1. Abo im Browser öffnen</strong>
    <p class="privacy" style="margin-top:.3rem">
      Die Seite abonniert dich beim Öffnen von selbst. Nichts installieren,
      kein Konto.
    </p>
    {open_it}
    <p class="privacy" style="margin-top:.7rem">{_topic_note(topic)}</p>
  </div>
  <div>
    <strong>2. Mitteilungen erlauben</strong>
    <p class="privacy" style="margin-top:.3rem">{pin}</p>
  </div>"""


def _telegram_step(link: str, bot: str, code: str, platform: str, n: int = 3) -> str:
    """The step where Telegram is wanted. t.me links open the app on both
    phones, so unlike ntfy this needs no per-platform detour - only the code
    is pointless on the device Telegram is not on."""
    if platform == "desktop":
        return f"""<div>
    <strong>{n}. Telegram verbinden</strong>
    <p class="privacy" style="margin-top:.3rem">
      Scanne diesen Code mit dem Handy: Telegram öffnet sich beim Bot und
      schickt deinen Code ab. Wenige Minuten später kommen die Meldungen auch dort an.
    </p>
    {_qr(link)}
    <p style="margin-top:.9rem">
      <a class="btn btn-quiet" href="{html.escape(link)}">Direkt @{html.escape(bot)} öffnen</a>
    </p>
    <p class="privacy" style="margin-top:.7rem">
      Geht beides nicht, schreibe dem Bot von Hand:<br>
      <code>/start {html.escape(code)}</code>
    </p>
  </div>"""
    return f"""<div>
    <strong>{n}. Telegram verbinden</strong>
    <p class="privacy" style="margin-top:.3rem">
      Einmal tippen: Telegram öffnet sich beim Bot und schickt deinen Code ab.
      Wenige Minuten später kommen die Meldungen auch dort an.
    </p>
    <p style="margin-top:.8rem">
      <a class="btn btn-go btn-block" href="{html.escape(link)}">@{html.escape(bot)} öffnen</a>
    </p>
    <p class="privacy" style="margin-top:.7rem">
      Geht das nicht, schreibe dem Bot von Hand:<br>
      <code>/start {html.escape(code)}</code>
    </p>
  </div>"""


# How much is left to do per way, and whether it is worth adding "auf dem
# Handy" when the page is on a computer. The browser way never says it: it is
# usually finished on whichever screen is already reading this.
_HANDS = {
    "ntfy": ("Noch zwei Handgriffe", True),
    "web": ("Noch zwei Handgriffe", False),
    "telegram": ("Noch ein Handgriff", True),
}


def welcome(entry: dict, topic: str, topic_url: str, place: str,
            telegram_bot: str | None = None, code: str | None = None,
            leave_url: str = "", platform: str = "desktop",
            server: str = "https://ntfy.sh", way: str = DEFAULT_WAY) -> str:
    """The page after signing up, tailored to the way chosen and to whatever is
    reading it.

    On a computer the phone is a second screen and the QR code bridges the two.
    On a phone there is nothing to bridge, so the codes give way to buttons
    that go straight where the code would have pointed.
    """
    steps = []
    done = 0
    if topic and way == "web":
        steps.append(_webapp_steps(topic, server, platform))
        done = 2
    elif topic:
        steps.append(f"""<div>
    <strong>1. ntfy installieren</strong>
    <p class="stores">
            {store_links(platform)}
          </p>
  </div>""")
        steps.append(_ntfy_step(topic, topic_url, server, platform, place,
                                bool(telegram_bot and code), n=2))
        done = 2
    if telegram_bot and code:
        # Telegram is a way of its own now, so it is usually the only step on
        # the page and has to be able to be step one.
        steps.append(_telegram_step(telegram_link.deep_link(telegram_bot, code),
                                    telegram_bot, code, platform, n=done + 1))

    modes = "".join(f"<li>{html.escape(describe_rule(rule))}</li>" for rule in entry["rules"])
    leave_note = ""
    if leave_url:
        leave_note = (
            f'<a href="{html.escape(leave_url)}">Wieder abmelden</a> &nbsp;·&nbsp; '
        )
    # The test button proves the person is who the page was made for. The topic
    # is the secret an ntfy signup was just handed; a Telegram-only one has no
    # topic, so their own leaving token stands in - it is equally theirs, and
    # equally already on this page.
    proof = (f'<input type="hidden" name="topic" value="{html.escape(topic)}">' if topic
             else '<input type="hidden" name="token" '
                  f'value="{html.escape(str(entry.get("unsubscribe", "")))}">')
    words, on_phone = _HANDS.get(way, ("Noch zwei Handgriffe", False))
    hands = f"{words} auf dem Handy" if on_phone and platform == "desktop" else words
    return f"""
<h1 style="margin-bottom:.8rem">Fast geschafft, {html.escape(entry['name'])}.</h1>
<p class="lede">{hands}, dann meldet sich
   GleichNass für {html.escape(place)} von selbst.</p>

<div class="signup" style="margin-top:2rem">
  {"".join(steps)}
  <div>
    <strong>Du bekommst</strong>
    <ul style="margin:.4rem 0 0;padding-left:1.1rem;color:var(--muted)">{modes}</ul>
  </div>
  <form method="post" action="/test">
    <input type="hidden" name="user" value="{html.escape(entry['id'])}">
    {proof}
    <button class="btn btn-go" type="submit" style="width:100%;justify-content:center">
      Testnachricht schicken
    </button>
  </form>
</div>

<p class="note" style="margin-top:1.5rem">{leave_note}<a href="/">Zurück zur Startseite</a></p>
{COPY_JS}
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


_CHANNEL_WORDS = {"ntfy": "die ntfy-App", "telegram": "Telegram"}


def _test_result(sent: list[str], failed: list[tuple[str, str]],
                 waiting_for_telegram: bool = False) -> str:
    """Say where the test actually went. "Unterwegs" is not much help when
    somebody is standing there wondering why Telegram stayed quiet."""
    where = " und ".join(_CHANNEL_WORDS.get(c, c) for c in sent)
    telegram_note = ('<p class="note" style="margin-top:1rem">Telegram ist noch nicht '
                     "verbunden. Öffne dafür den Bot über den Knopf oder den QR-Code "
                     "auf der vorigen Seite, dann probiere es noch einmal.</p>"
                     ) if waiting_for_telegram else ""

    if not sent and not failed:
        # A Telegram signup has no channel at all until the bot has heard from
        # them, so there was nothing to fail - and nothing to send either.
        return ("<h1>Noch nichts zu schicken.</h1>"
                '<p class="lede">Es hängt noch keine Zustellung an deiner Anmeldung.</p>'
                f"{telegram_note}"
                '<p class="note" style="margin-top:1.2rem">'
                '<a href="/">Zurück zur Startseite</a></p>')

    if sent and not failed:
        return (f"<h1>Unterwegs an {html.escape(where)}.</h1>"
                '<p class="lede">Kommt nichts an, prüfe, ob du das Thema in der App '
                f"wirklich abonniert hast.</p>{telegram_note}"
                '<p class="note" style="margin-top:1.2rem">'
                '<a href="/">Zurück zur Startseite</a></p>')

    problems = "".join(
        f"<li>{html.escape(_CHANNEL_WORDS.get(c, c))}: {html.escape(why)}</li>"
        for c, why in failed
    )
    head = (f"<h1>Teilweise geklappt.</h1><p class=\"lede\">Unterwegs an "
            f"{html.escape(where)}.</p>") if sent else "<h1>Das hat nicht geklappt.</h1>"
    return (f"{head}<ul class=\"note\" style=\"margin-top:1rem\">{problems}</ul>"
            f"{telegram_note}"
            '<p class="note" style="margin-top:1.2rem"><a href="/">Zurück</a></p>')


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

    def _platform(self) -> str:
        """Phone or computer, as far as the browser is willing to say."""
        return platform_of(self.headers.get("User-Agent", ""))

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
                base_url=self._base_url(config),
                platform=self._platform(),
            ))
        elif route == "/places":
            if not self.settings["browse"].allow(self._client()):
                return self._send(429, b"[]", "application/json; charset=utf-8")
            self._places(parse_qs(urlparse(self.path).query).get("q", [""])[0])
        elif route == "/abbestellen":
            self._leave_form(parse_qs(urlparse(self.path).query))
        elif route.startswith("/abo/"):
            self._subscribe(route[len("/abo/"):], parse_qs(urlparse(self.path).query))
        elif route == "/impressum":
            self._impressum()
        elif route == "/og.png":
            self._send(200, OG_IMAGE.read_bytes(), "image/png")
        elif route == "/favicon.svg":
            self._send(200, FAVICON.read_bytes(), "image/svg+xml")
        elif route == "/favicon.ico":
            # Browsers still ask for this by name; the SVG answers for both.
            self._send(200, FAVICON.read_bytes(), "image/svg+xml")
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
        self._send(200, _shell("Abo einrichten",
                               subscribe_page(topic, label, server, self._platform())))

    def _impressum(self):
        config = self._config()
        legal = config.impressum
        if not legal.name:
            return self._not_found()

        def row(label, value):
            return (f'<p style="margin-top:.9rem"><strong>{html.escape(label)}</strong><br>'
                    f'{value}</p>') if value else ""

        address = "<br>".join(
            html.escape(part) for part in (legal.street, legal.city, legal.country) if part
        )
        mail = (f'<a href="mailto:{html.escape(legal.email)}">{html.escape(legal.email)}</a>'
                if legal.email else "")

        self._send(200, _shell("Impressum", f"""
<h1 style="margin-bottom:.8rem">Impressum</h1>
<p class="lede">Angaben gemäß § 5 DDG.</p>

<div class="signup" style="margin-top:1.8rem">
  <div>
    {row("Anbieter", html.escape(legal.name) + "<br>" + address)}
    {row("Vertreten durch", html.escape(legal.represented_by))}
    {row("Kontakt", mail)}
    {row("Registereintrag", html.escape(legal.register))}
    {row("Umsatzsteuer-ID", html.escape(legal.vat_id))}
  </div>
</div>

<h2 style="margin-top:2.5rem">Datenschutz</h2>
<div class="signup" style="margin-top:1rem">
  <div>
    <p><strong>Was gespeichert wird</strong></p>
    <p class="privacy" style="margin-top:.3rem">
      Dein Name, dein Ort mit Koordinaten, die von dir gewählten Zeiten und der
      Kanal, über den du die Meldungen bekommst: ein ntfy-Thema oder eine
      Telegram-Chat-ID. Keine E-Mail-Adresse, keine Telefonnummer, kein Passwort.
    </p>
  </div>
  <div>
    <p><strong>Wofür</strong></p>
    <p class="privacy" style="margin-top:.3rem">
      Ausschließlich dafür, dir die Regenmeldungen zu schicken, die du
      ausgewählt hast. Keine Werbung, keine Weitergabe, keine Auswertung.
    </p>
  </div>
  <div>
    <p><strong>Wer die Daten sonst noch sieht</strong></p>
    <p class="privacy" style="margin-top:.3rem">
      Für die Vorhersage werden deine Koordinaten an den
      <a href="https://brightsky.dev">Bright-Sky</a>-Dienst,
      <a href="https://open-meteo.com">Open-Meteo</a> und
      <a href="https://www.met.no">MET Norway</a> übermittelt. Die Zustellung
      läuft über <a href="https://ntfy.sh">ntfy</a> beziehungsweise Telegram.
    </p>
  </div>
  <div>
    <p><strong>Löschen</strong></p>
    <p class="privacy" style="margin-top:.3rem">
      In jeder Meldung steckt ein Abmelde-Link. Ein Klick darauf löscht alles
      zu dir Gespeicherte, sofort und vollständig. Oder schreib an {mail}.
    </p>
  </div>
</div>

<p class="note" style="margin-top:1.5rem"><a href="/">Zurück zur Startseite</a></p>"""))

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
        bot = telegram_bot_for(config)
        try:
            way = clean_way(form.get("way", [""])[0], bool(bot))
        except ValueError:
            return self._reject(config, "Telegram ist hier nicht eingerichtet.")

        invite = config.signup.invite_code
        if invite and not _same_secret(form.get("code", [""])[0], invite):
            return self._reject(config, "Der Einladungscode stimmt nicht.", way)

        name = (form.get("name", [""])[0] or "").strip()[:60]
        place = (form.get("place", [""])[0] or "").strip()[:80]
        modes = [m for m in form.get("modes", []) if m in PRESETS]
        if not name or not place:
            return self._reject(config, "Bitte Name und Ort ausfüllen.", way)
        if not modes:
            return self._reject(config, "Bitte mindestens eine Meldung auswählen.", way)

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
                ), way)

        code = telegram_link.new_code() if way == "telegram" else None
        # ntfy and its web app are one channel listening on one topic; Telegram
        # has nothing to write down until the person has messaged the bot, so
        # they start with no channel and the code claims one later.
        channels = [] if way == "telegram" else [{"type": "ntfy"}]
        picked = _picked_location(form, place)

        try:
            with httpx.Client(timeout=20.0) as client:
                location = picked or geocode.lookup(client, place)
                if not _forecast_reaches(client, config, location):
                    return self._reject(config, (
                        f"Für „{location.name or place}“ bekomme ich gerade keine "
                        "Wetterdaten. Bitte einen Ort aus der Vorschlagsliste wählen."
                    ), way)
                created = signup.create(
                    config, name=name, location=location, place=place, presets=modes,
                    options=options, telegram_code=code, channels=channels, client=client,
                )
        except LookupError:
            return self._reject(config, f"Den Ort „{place}“ konnte ich nicht finden.", way)
        except Exception as error:  # noqa: BLE001
            log.exception("signup failed")
            return self._reject(config, f"Das hat leider nicht geklappt: {error}", way)

        log.info("signed up %s (%s)", created.entry["id"], created.path)
        server = str(
            config.defaults["channels"].get("ntfy", {}).get("server") or "https://ntfy.sh"
        ).rstrip("/")
        # The QR points at our own /abo page, which forwards into the app.
        where = created.location.name or place
        qr_target = (f"{self._base_url(config)}/abo/{created.topic}?ort={quote(where[:40])}"
                     if created.topic else "")
        self._send(
            200,
            _shell(
                "Fast geschafft",
                welcome(created.entry, created.topic or "", qr_target, where,
                        telegram_bot=bot, code=code, platform=self._platform(),
                        server=server, way=way,
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

        # User ids are people's first names, so knowing one proves nothing. The
        # topic is the secret an ntfy signup was just given, and anyone holding
        # it could publish to that topic directly anyway. A Telegram-only signup
        # has no topic, so their leaving token stands in: it is equally theirs,
        # and it is already on the page this button sits on.
        proofs = [
            c.settings.get("topic", "") for c in user.channels
            if c.type == "ntfy" and c.settings.get("topic")
        ]
        if user.unsubscribe:
            proofs.append(user.unsubscribe)
        offered = form.get("topic", [""])[0] or form.get("token", [""])[0]
        if not offered or not any(_same_secret(offered, p) for p in proofs):
            log.warning("rejected /test for %s from %s", user.id, self._client())
            return self._send(403, _shell("Nicht erlaubt", "<h1>Nicht erlaubt.</h1>"))

        # Registration only stores a Telegram code; the channel is attached when
        # the code reaches the bot. Claim it here, or pressing this button right
        # after scanning would quietly test ntfy alone - or, for a Telegram-only
        # signup, find nothing to test at all.
        user, waiting = self._claim_telegram(config, user)

        sent, failed = [], []
        note = message.test_notification(user)
        with httpx.Client(timeout=15.0) as client:
            for spec in user.channels:
                try:
                    notify.build(spec.type, spec.settings).send(client, note)
                except Exception as error:  # noqa: BLE001
                    log.error("test to %s via %s failed: %s", user.id, spec.type, error)
                    failed.append((spec.type, str(error)))
                else:
                    sent.append(spec.type)

        self._send(200, _shell("Test", _test_result(sent, failed, waiting)))

    def _claim_telegram(self, config, user):
        """Attach a Telegram chat that has just messaged the bot.

        Returns the user as they now stand, and whether a code of theirs is
        still unclaimed - which is the difference between "nothing arrived" and
        "you have not opened the bot yet".
        """
        token = (config.defaults["channels"].get("telegram") or {}).get("token")
        if not token or user.id not in telegram_link.pending(config):
            return user, False
        try:
            with httpx.Client(timeout=10.0) as client:
                telegram_link.link_waiting(config, client, token)
        except Exception as error:  # noqa: BLE001
            log.warning("could not claim Telegram links: %s", error)
            return user, True
        config = self._config()
        try:
            user = config.user(user.id)
        except KeyError:
            return user, True
        return user, user.id in telegram_link.pending(config)

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

    def _reject(self, config, error: str, way: str = DEFAULT_WAY):
        self._send(400, landing(error, bool(config.signup.invite_code),
                                telegram_bot_for(config), platform=self._platform(),
                                way=way))

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
