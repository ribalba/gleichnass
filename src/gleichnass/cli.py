"""Command line surface.

    gleichnass forecast --place Konstanz    ad-hoc: what do the providers say?
    gleichnass run                          one pass over every user and rule (cron calls this)
    gleichnass add-user --name … --place …  create a user and print their QR code
    gleichnass serve                        the self-signup site
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import config as config_module
from . import geocode, message, notify, providers, signup, telegram_link, web
from .analyze import Outlook, Threshold, analyze
from .config import ConfigError
from .models import Location
from .rules import PRESETS
from .runner import run_once
from .state import Store
from .util import expand_env, parse_duration

EXAMPLE_CONFIG = Path(__file__).with_name("gleichnass.example.yaml")


def default_config_path() -> Path:
    return Path(os.environ.get("GLEICHNASS_CONFIG", "gleichnass.yaml"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="gleichnass",
        description="Tells you, and your friends, when it is about to rain.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log what is happening")
    subcommands = parser.add_subparsers(dest="command", required=True)

    _add_forecast(subcommands)
    _add_run(subcommands)
    _add_users(subcommands)
    _add_serve(subcommands)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.handler(args)
    except ConfigError as error:
        print(f"config: {error}", file=sys.stderr)
        return 2
    except (ValueError, LookupError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------
# forecast, the v0 tool, unchanged behaviour
# --------------------------------------------------------------------------


def _add_forecast(subcommands) -> None:
    parser = subcommands.add_parser("forecast", help="ask every provider about one place")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--place", help="place name, e.g. 'Konstanz'")
    where.add_argument("--lat", type=float, help="latitude (use with --lon)")
    parser.add_argument("--lon", type=float)
    parser.add_argument("--window", type=parse_duration, default="12h",
                        help="how far ahead to look, from now (default: 12h)")
    parser.add_argument("--provider", action="append", metavar="NAME",
                        help=f"repeatable; 'all' for everything. {', '.join(providers.ALL)}")
    parser.add_argument("--min-mm", type=float, default=0.2, help="rain threshold in mm/h")
    parser.add_argument("--min-prob", type=float, default=50.0, help="probability threshold in %%")
    parser.add_argument("--tz", default="Europe/Berlin")
    parser.add_argument("--json", action="store_true")
    parser.set_defaults(handler=_forecast)


def _forecast(args) -> int:
    if args.lat is not None and args.lon is None:
        raise ValueError("--lat requires --lon")

    selected = args.provider or providers.DEFAULT
    if "all" in selected:
        selected = providers.ALL
    chosen = [providers.build(name) for name in selected]

    zone = ZoneInfo(args.tz)
    threshold = Threshold(min_mm_per_hour=args.min_mm, min_probability=args.min_prob)
    start = datetime.now(UTC)
    end = start + args.window

    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        location = (
            geocode.lookup(client, args.place)
            if args.place
            else Location(lat=args.lat, lon=args.lon)
        )

        def run(provider):
            try:
                forecast = provider.fetch(client, location, end)
                return provider.name, analyze(forecast, start, end, threshold), None
            except Exception as error:  # noqa: BLE001, one flaky API must not sink the rest
                return provider.name, None, f"{type(error).__name__}: {error}"

        with ThreadPoolExecutor(max_workers=len(chosen)) as pool:
            results = list(pool.map(run, chosen))

    if args.json:
        print(json.dumps(_forecast_json(location, start, end, threshold, results), indent=2))
    else:
        _render_forecast(location, start, end, threshold, results, zone)
    return 0


def _render_forecast(location, start, end, threshold, results, zone) -> None:
    print(f"\n  {location}")
    print(
        f"  {_time(start, zone, start)} → {_time(end, zone, start)}"
        f"  ·  rain = ≥{threshold.min_mm_per_hour:g} mm/h"
        f" at ≥{threshold.min_probability:g}% (where reported)\n"
    )

    width = max(len(name) for name, _, _ in results)
    for name, outlook, error in results:
        print(f"  {name:<{width}}  {_forecast_line(outlook, error, zone, start)}")

    answered = [o for _, o, _ in results if o is not None and o.has_data]
    wet = [o for o in answered if o.will_rain]
    print()
    if not answered:
        print("  No provider returned data.\n")
    elif wet:
        onset = min(o.onset for o in wet)
        print(
            f"  {len(wet)}/{len(answered)} providers expect rain,"
            f" earliest {_time(onset, zone, start)} ({_delta(onset - start)}).\n"
        )
    else:
        print(f"  All {len(answered)} providers expect no rain in this window.\n")


def _forecast_line(outlook: Outlook | None, error: str | None, zone: tzinfo, now: datetime) -> str:
    if error:
        return f"!  {error}"
    if not outlook.has_data:
        return "?  no data for this window"

    if outlook.will_rain:
        onset = (
            "now"
            if outlook.onset <= now + timedelta(minutes=1)
            else _time(outlook.onset, zone, now)
        )
        text = (
            f"~  {onset} – {_time(outlook.until, zone, now)}"
            f"  {outlook.total_mm:.1f} mm  peak {outlook.peak_mm_per_hour:.1f} mm/h"
        )
        if outlook.max_probability is not None:
            text += f"  {outlook.max_probability:.0f}%"
        if outlook.spells > 1:
            text += f"  (+{outlook.spells - 1} more)"
    else:
        text = ".  dry"

    if outlook.truncated:
        text += f"  (only sees to {_time(outlook.covered_until, zone, now)})"
    return text


def _forecast_json(location, start, end, threshold, results) -> dict:
    return {
        "location": {"lat": location.lat, "lon": location.lon, "name": location.name},
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "threshold": {
            "min_mm_per_hour": threshold.min_mm_per_hour,
            "min_probability": threshold.min_probability,
        },
        "providers": [
            {
                "name": name,
                "error": error,
                **(
                    {}
                    if outlook is None
                    else {
                        "source": outlook.source,
                        "has_data": outlook.has_data,
                        "will_rain": outlook.will_rain,
                        "onset": outlook.onset.isoformat() if outlook.onset else None,
                        "until": outlook.until.isoformat() if outlook.until else None,
                        "spells": outlook.spells,
                        "total_mm": round(outlook.total_mm, 2),
                        "peak_mm_per_hour": round(outlook.peak_mm_per_hour, 2),
                        "max_probability": outlook.max_probability,
                        "covered_until": outlook.covered_until.isoformat(),
                    }
                ),
            }
            for name, outlook, error in results
        ],
    }


# --------------------------------------------------------------------------
# run, what cron calls
# --------------------------------------------------------------------------


def _config_argument(parser) -> None:
    parser.add_argument("-c", "--config", type=Path, default=default_config_path(),
                        help="path to gleichnass.yaml (env: GLEICHNASS_CONFIG)")


def _add_run(subcommands) -> None:
    parser = subcommands.add_parser("run", help="check every due rule and notify")
    _config_argument(parser)
    parser.add_argument("--dry-run", action="store_true",
                        help="print notifications instead of sending, and touch no state")
    parser.add_argument("--force", action="store_true",
                        help="evaluate every rule regardless of its schedule")
    parser.add_argument("--user", help="limit to one user id")
    parser.set_defaults(handler=_run)


def _run(args) -> int:
    config = config_module.load(args.config)
    with Store(config.state_path) as store, httpx.Client(timeout=20.0) as client:
        # Anyone who has sent the bot their code since the last tick gets their
        # Telegram hooked up before the notifications go out.
        if not args.dry_run:
            for user_id, chat_id in _claim_telegram(config, client):
                print(f"  linked Telegram chat {chat_id} to {user_id}")
                config = config_module.load(args.config)

        decisions = run_once(
            config, store, client,
            dry_run=args.dry_run, only_user=args.user, force=args.force,
        )

    if not decisions:
        print("nothing due")
        return 0
    for decision in decisions:
        print(f"  {decision}")
    return 1 if any(d.error for d in decisions) else 0


# --------------------------------------------------------------------------
# user management
# --------------------------------------------------------------------------


def _telegram_token(config) -> str:
    return expand_env(
        config.defaults["channels"].get("telegram", {}).get("token", "")
    ) or os.environ.get("GLEICHNASS_TELEGRAM_TOKEN", "")


def _claim_telegram(config, client) -> list[tuple[str, int]]:
    token = _telegram_token(config)
    if not token or not telegram_link.pending(config):
        return []
    return telegram_link.link_waiting(config, client, token)


def _add_users(subcommands) -> None:
    listing = subcommands.add_parser("users", help="list configured users")
    _config_argument(listing)
    listing.set_defaults(handler=_users)

    adding = subcommands.add_parser("add-user", help="create a user file and print a QR code")
    _config_argument(adding)
    adding.add_argument("--name", required=True)
    adding.add_argument("--place", help="place name to geocode")
    adding.add_argument("--lat", type=float)
    adding.add_argument("--lon", type=float)
    adding.add_argument("--mode", action="append", choices=sorted(PRESETS),
                        help=f"repeatable; default {', '.join(signup.DEFAULT_PRESETS)}")
    adding.add_argument("--language", choices=sorted(message.TEXTS))
    adding.add_argument("--id", help="readable id instead of a generated one")
    adding.add_argument("--telegram-chat", metavar="ID",
                        help="also notify this Telegram chat (see telegram-ids)")
    adding.add_argument("--no-ntfy", action="store_true",
                        help="skip the ntfy channel, e.g. for Telegram only")
    adding.add_argument("--night-at", metavar="HH:MM", help="evening digest time (default 20:00)")
    adding.add_argument("--morning-at", metavar="HH:MM", help="morning digest time (default 08:00)")
    adding.add_argument("--imminent-window", metavar="DURATION",
                        help="how far ahead the watch looks (default 1h)")
    adding.set_defaults(handler=_add_user)

    removing = subcommands.add_parser("remove-user", help="delete a user and their state")
    _config_argument(removing)
    removing.add_argument("user", help="id, a unique part of it, or the person's name")
    removing.set_defaults(handler=_remove_user)

    testing = subcommands.add_parser("test", help="send a test notification")
    _config_argument(testing)
    testing.add_argument("user", help="id, a unique part of it, or the person's name")
    testing.set_defaults(handler=_test)

    deliveries = subcommands.add_parser("log", help="recently delivered notifications")
    _config_argument(deliveries)
    deliveries.add_argument("-n", type=int, default=20)
    deliveries.set_defaults(handler=_log)

    chats = subcommands.add_parser("telegram-ids", help="chat ids of people who messaged the bot")
    _config_argument(chats)
    chats.set_defaults(handler=_telegram_ids)

    linking = subcommands.add_parser(
        "telegram-link", help="connect waiting sign-ups to the chats that sent their code"
    )
    _config_argument(linking)
    linking.set_defaults(handler=_telegram_link)

    starter = subcommands.add_parser("init", help="write a starter config")
    _config_argument(starter)
    starter.set_defaults(handler=_init)


def resolve_user(config, token: str):
    """Find a user by id, by a unique prefix of it, or by name.

    Ids are UUIDs now, so nobody should have to type one in full; and since two
    people may share a name, an ambiguous one is reported rather than guessed.
    """
    token = (token or "").strip()
    exact = [u for u in config.users if u.id == token]
    if exact:
        return exact[0]

    lowered = token.lower()
    matches = [u for u in config.users if u.id.startswith(token)] if token else []
    matches += [
        u for u in config.users
        if u.name.lower() == lowered and u not in matches
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise KeyError(token)
    listed = ", ".join(f"{u.name} ({u.id[:8]})" for u in matches)
    raise ValueError(f"{token!r} matches more than one person: {listed}")


def _users(args) -> int:
    config = config_module.load(args.config)
    if not config.users:
        print(f"no users yet, try: gleichnass add-user --name … --place … -c {args.config}")
        return 0
    for user in config.users:
        described = []
        for spec in user.channels:
            try:
                described.append(notify.build(spec.type, spec.settings).describe())
            except ValueError as error:
                # Listing must still work when one person's channel is broken, # that is exactly when you need to look at the list.
                described.append(f"! {error}")
        channel = " + ".join(described)
        rules = ", ".join(
            f"{rule.name}@{rule.at:%H:%M}"
            if rule.at
            else f"{rule.name}/{int(rule.every.total_seconds() // 60)}m"
            for rule in user.rules
        )
        print(f"  {user.name[:16]:<16} {user.id[:8]}  {str(user.location):<42}"
              f"  {channel:<26} {rules}")
    return 0


def _add_user(args) -> int:
    config = config_module.load(args.config)
    location = None
    if args.lat is not None or args.lon is not None:
        if args.lat is None or args.lon is None:
            raise ValueError("--lat and --lon go together")
        location = Location(args.lat, args.lon, args.place or args.name)

    channels = [] if args.no_ntfy else [{"type": "ntfy"}]
    if args.telegram_chat:
        channels.append({"type": "telegram", "chat_id": args.telegram_chat})
    if not channels:
        raise ValueError("--no-ntfy leaves no channel; add --telegram-chat")

    with httpx.Client(timeout=15.0) as client:
        created = signup.create(
            config, name=args.name, place=args.place, location=location,
            presets=args.mode, language=args.language, channels=channels, client=client,
            user_id=args.id,
            options={"night": args.night_at, "morning": args.morning_at,
                     "imminent": args.imminent_window},
        )

    modes = ", ".join(
        f"{rule['preset']}@{rule['at']}" if "at" in rule
        else f"{rule['preset']}/{rule.get('window', '')}"
        for rule in created.entry["rules"]
    )
    print(f"\n  wrote {created.path}")
    print(f"  id: {created.entry['id']}")
    print(f"  {created.entry['location']['name']} · {modes}")
    print(f"  channels: {', '.join(spec['type'] for spec in created.entry['channels'])}")
    if created.topic:
        server = _ntfy_server(config, created.entry)
        url = f"{server}/{created.topic}"
        print(f"\n  Subscribe in the ntfy app to:  {created.topic}\n  or open:  {url}\n")
        _print_qr(url)
    return 0


def _ntfy_server(config, entry: dict) -> str:
    shared = config.defaults["channels"].get("ntfy", {})
    for spec in entry.get("channels", []):
        if spec.get("type", "ntfy") == "ntfy":
            return str(spec.get("server") or shared.get("server") or "https://ntfy.sh").rstrip("/")
    return "https://ntfy.sh"


def _print_qr(url: str) -> None:
    import segno

    if shutil.get_terminal_size().columns >= 45:
        segno.make(url, error="m").terminal(compact=True)
        print()


def _remove_user(args) -> int:
    config = config_module.load(args.config)
    user = resolve_user(config, args.user)
    if user.path is None or user.path == config.path:
        raise ValueError(f"{user.id} is defined in {config.path}; remove them by hand")
    user.path.unlink()
    with Store(config.state_path) as store:
        store.forget(user.id)
    print(f"removed {user.id} ({user.path})")
    return 0


def _test(args) -> int:
    config = config_module.load(args.config)
    user = resolve_user(config, args.user)
    note = message.test_notification(user)

    failures = 0
    with httpx.Client(timeout=15.0) as client:
        for spec in user.channels:
            try:
                channel = notify.build(spec.type, spec.settings)
                channel.send(client, note)
            except Exception as error:  # noqa: BLE001, report every channel
                failures += 1
                print(f"  {spec.type:<10} FAILED  {type(error).__name__}: {error}")
            else:
                print(f"  {spec.type:<10} sent    {channel.describe()}")
    return 1 if failures else 0


def _log(args) -> int:
    config = config_module.load(args.config)
    with Store(config.state_path) as store:
        rows = store.recent_deliveries(args.n)
    for row in rows:
        mark = "ok " if row["ok"] else "ERR"
        stamp = datetime.fromisoformat(row["sent_at"]).astimezone().strftime("%d.%m %H:%M")
        print(f"  {stamp}  {mark}  {row['user_id']}/{row['rule']}  {row['title']}")
        if row["error"]:
            print(f"           {row['error']}")
    return 0


def _telegram_link(args) -> int:
    config = config_module.load(args.config)
    token = _telegram_token(config)
    if not token:
        raise ValueError("no bot token; set GLEICHNASS_TELEGRAM_TOKEN")

    waiting = telegram_link.pending(config)
    if not waiting:
        print("nobody is waiting to be linked")
        return 0
    print(f"waiting: {', '.join(f'{u} ({c})' for u, c in waiting.items())}")

    with httpx.Client(timeout=15.0) as client:
        linked = telegram_link.link_waiting(config, client, token)
    for user_id, chat_id in linked:
        print(f"  linked chat {chat_id} to {user_id}")
    if not linked:
        print("  no matching message yet; ask them to send the bot their code")
    return 0


def _telegram_ids(args) -> int:
    from .notify.telegram import recent_chats

    config = config_module.load(args.config)
    token = _telegram_token(config)
    if not token:
        raise ValueError("no bot token; set GLEICHNASS_TELEGRAM_TOKEN")
    with httpx.Client(timeout=15.0) as client:
        chats = recent_chats(client, token)
    if not chats:
        print("nobody has messaged the bot yet, ask them to send it /start")
    for chat in chats:
        label = chat.get("username") or chat.get("first_name") or chat.get("title") or "?"
        print(f"  {chat['id']:<14} {label}")
    return 0


def _init(args) -> int:
    target = Path(args.config)
    if target.exists():
        raise ValueError(f"{target} already exists")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    (target.parent / "users.d").mkdir(exist_ok=True)
    print(f"wrote {target}\nnext: gleichnass add-user --name 'Didi' --place Konstanz -c {target}")
    return 0


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------


def _add_serve(subcommands) -> None:
    parser = subcommands.add_parser("serve", help="run the website, including registration")
    _config_argument(parser)
    parser.add_argument("--host", default="127.0.0.1",
                        help="use 0.0.0.0 to accept from outside (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--trust-proxy", action="store_true",
                        default=os.environ.get("GLEICHNASS_TRUST_PROXY", "") not in ("", "0"),
                        help="take the client address from X-Forwarded-For; only with a "
                             "reverse proxy in front (env: GLEICHNASS_TRUST_PROXY)")
    parser.set_defaults(handler=_serve)


def _serve(args) -> int:
    config = config_module.load(args.config)
    logging.getLogger("gleichnass.web").setLevel(logging.INFO)
    if config.signup.enabled:
        gate = (
            f"invite code required, {config.signup.per_hour}/h per address"
            if config.signup.invite_code
            else f"open, {config.signup.per_hour} signups/h per address"
        )
        print(f"registration: {gate}")
    else:
        print("registration: closed (signup.enabled is false)")
    if args.trust_proxy:
        print("client address: from X-Forwarded-For (reverse proxy assumed)")
    web.serve(args.config, host=args.host, port=args.port,
              per_hour=config.signup.per_hour, trust_proxy=args.trust_proxy)
    return 0


# --------------------------------------------------------------------------


def _time(moment: datetime, zone: tzinfo, reference: datetime) -> str:
    local = moment.astimezone(zone)
    same_day = local.date() == reference.astimezone(zone).date()
    return local.strftime("%H:%M" if same_day else "%a %H:%M")


def _delta(span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    if minutes < 60:
        return f"in {minutes} min"
    return f"in {minutes // 60}h {minutes % 60:02d}m"


if __name__ == "__main__":
    sys.exit(main())
