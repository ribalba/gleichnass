# GleichNass

A little app that notifies you when it about to rain.

Push notifications to Android and iPhone, free, with no accounts and nothing in
an app store. Germany-focused: the data comes from the DWD, including its radar
nowcast, which is what makes "rain in 40 minutes" worth trusting.

```text
» Regen in 45 Min
    Konstanz
    ab 14:45 · 1.8 mm, Spitze 3.2 mm/h
    2 von 3 Diensten
```

It runs as a service: `gleichnass serve` puts up a website where friends register themselves, so you never edit a file to add someone.

## How it works

```text
cron, every 5 min  →  fetch forecasts  →  evaluate rules  →  push
                        (cached per place)   (SQLite remembers what was sent)
```

Three notification modes, which are all the same code path with different
parameters: *when to check* and *how far ahead to look*.

| Preset | Trigger | Looks ahead | |
|---|---|---|---|
| `night` | 20:00 | 12 h | Will it rain overnight? |
| `morning` | 08:00 | 12 h | Will it rain today? |
| `imminent` | every 15 min | 1 h | It is about to rain. |

Anything else, a commute alert or a weekend outlook, is another line in the
config rather than new code.

## Quick start

```sh
python3 -m venv .venv && .venv/bin/pip install -e .
.venv/bin/gleichnass init
.venv/bin/gleichnass add-user --name "Didi" --place Konstanz
```

That writes `users.d/didi.yaml`, generates a private ntfy topic and prints a QR
code. Install [ntfy](https://ntfy.sh) on the phone, scan it, then:

```sh
.venv/bin/gleichnass test didi              # does the phone buzz?
.venv/bin/gleichnass run --dry-run --force  # what would it say right now?
.venv/bin/gleichnass run                    # what cron will call
```

Everything takes `-c/--config` or `GLEICHNASS_CONFIG`.

## Running it for real

```sh
docker compose up -d
```

That is the whole deployment. One compose file, two containers off the same
image sharing one volume: `site` serves the website and takes registrations,
`melder` checks the weather and sends the notifications.

**No cron, no scheduler, no timer.** The melder loops on its own every five
minutes (`GLEICHNASS_INTERVAL`), and each rule works out for itself whether it
is due. A missed tick, a restart or a host that was off all afternoon costs
nothing, and a digest that missed its slot by more than an hour is dropped
rather than delivered stale.

Nothing needs preparing first: on an empty volume the container writes a
starter config, so a fresh host comes up working. The only thing to back up, and
the only thing that must survive a redeploy, is the `gleichnass-data` volume,
which holds the config, `users.d/` and the SQLite state.

There is no reverse proxy in the compose file. Whatever you already run for TLS
(Coolify, Traefik, Caddy, nginx, a tunnel) points at `site` on port 8080.

### On Coolify

Create a **Docker Compose** resource pointing at this repository, leave the
compose path as `docker-compose.yml`, and set a domain for the `site` service
(either in the UI, or let `SERVICE_FQDN_SITE_8080` generate one). Coolify builds
the image, runs both containers, terminates TLS and routes to `site`.

Two things worth getting right:

- **Do not add a `ports:` mapping.** Published ports bypass Coolify's proxy.
  `site` only declares `expose`, which is what lets Coolify route to it.
- **Keep the named volume.** A bind mount into the repository checkout is wiped
  on redeploy, and every registration lives in there.
- **`--trust-proxy` is already in the compose command**, and matters: behind a
  proxy every request arrives from the proxy, so without it the sign-up rate
  limit would put every visitor in one bucket and lock out the eighth person of
  the hour. Drop the flag only if you expose the port directly, since the header
  is otherwise just something the client made up.

Set `GLEICHNASS_TELEGRAM_TOKEN` (and optionally `GLEICHNASS_INVITE_CODE`) as
environment variables in the Coolify UI rather than in the file.

### Without Docker

```sh
sudo cp deploy/gleichnass.{service,timer} /etc/systemd/system/
sudo systemctl enable --now gleichnass.timer     # or: see deploy/crontab.example
```

Those run `gleichnass run` on a timer instead of the container's loop, and you
start `gleichnass serve` yourself. They exist for installs without Docker; with
the compose file you need neither.

## Adding friends

Run the site and send them the link. That is the whole procedure.

```sh
gleichnass serve --host 0.0.0.0        # or: docker compose up -d
```

It serves the landing page with a registration form on it. The first step is a
choice of how the alerts should reach them - the ntfy app, Telegram, or ntfy's
web app in the browser - each card listing what that way gives and what it
costs. Everything after it follows from that choice. Then they fill in their
name, their town and which alerts they want; the server geocodes the town,
generates a private ntfy topic where one is needed, writes
`users.d/<name>.yaml` and hands them their subscription: a QR code on a
computer, a button on a phone. Nothing else is touched, so the config you
maintain by hand keeps its comments.

The way chosen decides both how many steps the flow has and what the
confirmation page does with them:

| Way | Steps before the form | Channel written | What the confirmation page does |
| --- | --- | --- | --- |
| `ntfy` | choose, then install the app | `ntfy` with a fresh topic | The topic into the app - deep link, QR or clipboard, by device |
| `web` | choose | `ntfy` with a fresh topic | The topic's own URL, which subscribes on open, plus how to keep it pushing |
| `telegram` | choose | none yet, only `telegram_code` | The `t.me` link that hands the bot the code |

Installing the app is a step of its own, with the shops under a heading that
says what they are for, and a confirmation that unlocks the form. The other two
ways have nothing to install, so that step is displayed away entirely - which is
why the numbers in the flow are a CSS counter and not written into the markup:
a step that is not this way's takes its number with it, and the rest close up
behind it.

**Registration is open by default.** Two things keep that from being a
liability:

- Signups are rate limited per address (`signup.per_hour`, default 8). The
  limit counts every attempt, not just successful ones, so a script hammering
  the form is stopped before it fills `users.d/`.
- Set `signup.invite_code` (or `GLEICHNASS_INVITE_CODE`) to narrow
  registration to people you gave the code to. Leave it unset for anyone with
  the link. `signup.enabled: false` closes registration entirely.

Put a reverse proxy in front for TLS; [website/](website/) is a compose file
that bundles one.

You can also add someone from the command line:

```sh
gleichnass add-user --name "Didi" --place Konstanz
```

### Telegram

Set your bot's username and the site offers Telegram alongside ntfy:

```yaml
signup:
  telegram_bot: gleichnass_bot     # the handle, without the @
defaults:
  channels:
    telegram:
      token: ${GLEICHNASS_TELEGRAM_TOKEN}
```

People pick "Telegram" as their way in when they register. The confirmation
page then shows a button that opens the bot with a one-time code already filled in
(`https://t.me/<bot>?start=CODE`); the next `gleichnass run` sees the code in the
bot's updates and writes the chat id into their user file. Nobody looks up a
chat id and nobody edits YAML.

The code lives in the user's own file as `telegram_code`, so a half-finished
link survives a restart and is obvious when you look. `gleichnass telegram-link`
runs the same step by hand, and `gleichnass telegram-ids` still lists chat ids
if you would rather add someone yourself:

```sh
gleichnass add-user --name "Anna" --place Hamburg --telegram-chat 12345678
```

A Telegram signup is written with no channel at all, only its `telegram_code`:
there is nothing to deliver to until the person has messaged the bot. That is a
normal half-finished signup rather than a broken file, so it does not stop the
config loading, and the run that claims the code fills the channel in.

**ntfy and Telegram run in parallel.** Every notification goes to each channel a
person has, so someone added by hand can have both - useful to reach a tablet as
well as a phone. If one channel is down or misconfigured the other still gets
the alert, and the shower counts as announced, so the working channel is not
told about it again on every tick until the broken one recovers:

```text
didi/imminent: sent to 1 of 2 channels: telegram: 401 Unauthorized
```

### Choosing a place

The place field suggests real places as you type, from `/places`, which proxies
the geocoder so the page never talks to anyone but this server. Picking a
suggestion stores its coordinates, so the place is unambiguous.

Before anyone is signed up, the server asks a weather provider for actual
numbers at those coordinates. Geocoding will happily resolve somewhere the
weather APIs have nothing for, and a signup that can only ever be silent is
worse than an error on the form.

## The website

The page lives at
[src/gleichnass/templates/index.html](src/gleichnass/templates/index.html) and is
served by the application, because it carries the sign-up form and so cannot be
a static file. It is a single
self-contained file: no external fonts, scripts, images or network requests, and
it works in light and dark. The two hooks the server fills in are HTML comments,
so opening the file directly still renders the finished page.

## Leaving

**ntfy cannot tell us that somebody unsubscribed in the app.** Publishing to a
topic with no subscribers returns 200 and says nothing about who is listening,
so an unsubscribe there is invisible from this side. Two mechanisms cover it:

- **Every notification carries an unsubscribe link**, as an ntfy action button
  and as a line of text on Telegram. Following it asks first and only removes
  the person on the confirmation, since chat apps and mail clients follow links
  to preview them and a link that deleted on sight would go off by itself.
  This needs `signup.base_url`, because a notification has no way of knowing
  what this deployment is called.
- **Telegram does report a block**, unlike ntfy. A `403 blocked`, a deactivated
  account or a missing chat drops that channel rather than failing on it every
  five minutes, and a user with no channels left is removed entirely.

`gleichnass remove-user <id-or-name>` does the same by hand. Either way the
user's file is the whole record, so deleting it is the whole job.

## Providers

All free, keyless, no account.

| Name | Source | Resolution | Horizon | Probability |
|---|---|---|---|---|
| `dwd-radar` | DWD radar nowcast (RV) via Bright Sky | 5 min, 1 km | ~2 h | no |
| `icon-d2` | DWD ICON-D2 via Open-Meteo | 15 min, 2 km | ~2 days | yes |
| `brightsky` | DWD MOSMIX via Bright Sky | 1 h, station | ~10 days | yes |
| `open-meteo` | Open-Meteo best-match blend | 15 min | ~10 days | yes |
| `met-no` | MET Norway (Yr) | 1 h | ~2.5 days | not in Germany |

They are asked in the order you configure them, and the first that can see the
whole window is the one the message quotes. `dwd-radar` extrapolates what the
radar is actually seeing rather than what a model predicts, so it wins inside
two hours and is ignored beyond them. Everything except `met-no` traces back to
the DWD, which makes MET Norway the only source that can meaningfully disagree.

Adding one is a class with a `fetch()` method plus a line in
[providers/\_\_init\_\_.py](src/gleichnass/providers/__init__.py).

Ad-hoc, without any config:

```sh
gleichnass forecast --place Konstanz --window 12h --provider all
```

```text
  Konstanz, Baden-Württemberg, DE (47.6603, 9.1758)
  12:33 → Tue 00:33  ·  rain = ≥0.2 mm/h at ≥50% (where reported)

  dwd-radar   ~  now – 12:45  0.4 mm  peak 2.8 mm/h  (only sees to 13:25)
  icon-d2     ~  now – 15:00  0.9 mm  peak 2.0 mm/h  100%  (+1 more)
  brightsky   ~  now – 17:00  4.2 mm  peak 1.1 mm/h  75%
  met-no      ~  now – 20:00 10.5 mm  peak 3.0 mm/h  (+1 more)

  4/4 providers expect rain, earliest 12:33 (in 0 min).
```

## Configuration

`gleichnass.yaml` holds what everyone shares; each person is one file in
`users.d/`. See the commented [example](src/gleichnass/gleichnass.example.yaml).

```yaml
defaults:
  timezone: Europe/Berlin
  language: de              # de or en
  min_mm_per_hour: 0.2      # what counts as rain, as intensity
  min_probability: 50
  min_total_mm: 0.1         # ...and how much of it is worth a notification
  min_agreement: 1          # how many providers must agree
  providers: [dwd-radar, icon-d2, brightsky]
  channels:                 # settings shared per channel type
    ntfy: {server: https://ntfy.sh}
    telegram:
      token: ${GLEICHNASS_TELEGRAM_TOKEN}

signup:
  enabled: true             # open registration, the default
  per_hour: 8               # signup attempts allowed per address per hour
  # invite_code: ${GLEICHNASS_INVITE_CODE}
```

```yaml
id: didi
location: {lat: 47.6603, lon: 9.1758, name: Konstanz}
channels:                   # or `channel:` for just one
  - {type: ntfy, topic: regen-…}
  - {type: telegram, chat_id: 12345678}
rules:
  - preset: night
  - preset: imminent
    window: 90m             # presets are defaults; override anything
  - name: bike-commute      # or write one from scratch
    at: "07:30"
    window: 2h
    notify_when_dry: true
```

Secrets are read from the environment via `${VAR}`; never put a bot token in a
config file.

## Things worth knowing

- **Providers label intervals differently**, and getting it wrong shifts rain by
  an hour. Open-Meteo, Bright Sky and the radar stamp a value with the *end* of
  its interval; MET Norway with the *start*. Verified against the live APIs and
  pinned by tests. Each provider normalises to explicit start/end.
- **A short horizon is not a dry forecast.** Every provider reports how far it
  can see. A two-hour radar view never produces a twelve-hour all-clear, and an
  API outage is reported as "no data", never as "no rain".
- **Intensity, not accumulation, sets the threshold.** 0.1 mm is drizzle over an
  hour and heavy rain over five minutes, so slots are compared as mm/h whatever
  their length. A separate `min_total_mm` stops a single noisy radar frame,
  0.04 mm over five minutes, from waking anyone.
- **The same shower is announced once.** The imminent rule remembers the onset it
  last sent; a new alert needs an onset genuinely far from that one, and a
  delivery that failed is retried rather than silently marked as sent.
- **The radar feed can lag.** Bright Sky serves the newest RV run it has
  ingested, observed up to ~70 minutes old, which shrinks the usable nowcast.
  The run age is in the provider's `source` string. *Not yet acted on: a stale
  run should be refused rather than merely reported.*
- **Users at the same place share one request**, so ten friends in Berlin cost
  one API call per provider per run.
- **A channel is validated when the config loads**, not at 3am: a typo in a
  channel type or a missing bot token is reported by `gleichnass users`
  rather than discovered the first time it rains.
- **Registration faces the whole internet**, so the sign-up rate limit counts
  every attempt from an address, not just the ones that produced a user. Behind
  a reverse proxy that limit needs `--trust-proxy`, or every visitor lands in
  one bucket.
- **A user's id is a UUID, not their name.** Two friends called Alex would
  otherwise become `alex` and `alex-2`, which tells neither of them apart and
  makes removing one a guess. Commands take an id, a unique part of one, or a
  name when it is unambiguous, and say so when it is not:

  ```text
  error: 'Alex' matches more than one person: Alex (49973a1d), Alex (78934846)
  ```
- **Place lookups are cached in the same SQLite file.** A town's coordinates do
  not change, so the geocoder answers a given query once and every later visitor
  is served locally: about 4 ms instead of 200 ms, and well inside Open-Meteo's
  limits. Empty answers are not cached, since an empty list usually means the
  geocoder was unreachable rather than that the place does not exist.
- **The QR code opens the app, not a web page.** It points at `/abo/<topic>` on
  this site, which forwards into ntfy's `ntfy://` deep link so the app opens and
  subscribes in one step. That scheme is Android only, so pointing the QR
  straight at `ntfy://` would give iPhones a dead scan.
- **A phone gets buttons, not a QR code.** The pages read the `User-Agent` and
  drop the codes on a phone: a code is a way to move something to a second
  screen, and there is no second screen. Android is offered the deep link,
  Telegram its `t.me` link, and each phone only the app shop it can install
  from. The guess is never load bearing - a computer's page keeps a plain link
  next to the code, for the iPad that calls itself a Mac.
- **On the iPhone, nothing can hand ntfy a topic.** The iOS app registers no
  URL scheme and claims no https address, so no link, QR code or shortcut can
  reach it - the only route in is the topic field. So iPhones get the topic in
  one tap on the clipboard, and, for anyone who would rather not paste at all,
  ntfy's web app: opening `https://<server>/<topic>` subscribes by itself, and
  added to the home screen it can push as well. That last route is offered
  outright as the `web` way, whose second step is the honest part: Safari
  refuses a page notification permission entirely until it has been added to
  the home screen.
- **The Telegram bot is identified from its token**, via `getMe`. Setting the
  token used not to be enough: the site also wanted the bot's name in the config
  file, which lives inside the deployment's volume, so a token in the
  environment quietly did nothing.
- **A test notification has to be proved for.** Ids used to be first names, so
  `/test` was something a stranger could aim at someone else's phone. An ntfy
  signup proves itself with its topic: it is the secret handed out at signup,
  and anyone holding it could publish to that topic directly anyway. A Telegram
  signup has no topic, so its leaving token stands in - equally theirs, and
  already on the page the button sits on.

## Tests

```sh
.venv/bin/pip install -e ".[dev]" && .venv/bin/python -m pytest tests -q
```

No network: provider tests replay trimmed real responses, and the delivery
tests fake both the weather and the phone.

## Roadmap

- Refuse to alert on a stale radar run.
- Web push (PWA), so there is a route with no app at all. Works on Android and
  iOS 16.4+ once added to the home screen.
- Self-hosted ntfy, and other alerts (snow, frost, wind) through the same rule
  engine.

## Licence

AGPL-3.0-or-later. Weather data from the [DWD](https://www.dwd.de) via
[Bright Sky](https://brightsky.dev) and [Open-Meteo](https://open-meteo.com),
plus [MET Norway](https://www.met.no). Push delivery by [ntfy](https://ntfy.sh).
