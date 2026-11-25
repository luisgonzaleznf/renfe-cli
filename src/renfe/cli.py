"""`renfe` CLI. Prints JSON to stdout; errors go to stderr as {"error": {...}} with the exit codes
1 (internal), 2 (usage), 3 (auth), 5 (blocked/upstream) and 7 (API changed). No account or login needed.
"""

import json
import re
import sys
import time as time_mod
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from typing import Annotated, Any, NoReturn

import typer

try:  # Typer >= 0.29 vendors Click; older Typer uses the click package
    from typer._click import exceptions as click_exceptions
except ImportError:  # pragma: no cover
    from click import exceptions as click_exceptions

from . import config, delays as delays_mod, live, package_version
from . import search as search_mod, shop as shop_mod, stations as stations_mod
from .client import RsvClient
from .errors import ApiShapeError, RenfeError, UpstreamError, UsageError
from .output import render, render_error

app = typer.Typer(
    name="renfe",
    help="Search Renfe trains and fares, follow live delays and buy tickets in a browser. "
         "No account needed. Output is JSON.",
    no_args_is_help=True,
    add_completion=False,
    # Rich tracebacks print frame locals, which include the bearer and the api key.
    pretty_exceptions_enable=False,
)


def make_client() -> RsvClient:
    """The client every command uses (tests replace this)."""
    return RsvClient()


@contextmanager
def _client() -> Iterator[RsvClient]:
    """make_client(), whose requests that had to be retried go with any RenfeError raised inside, so the stderr
    error object lists them too."""
    client = make_client()
    try:
        with client:
            yield client
    except RenfeError as err:
        if client.retries and not err.retries:
            err.retries = [{**e, "errors": list(e["errors"])} for e in client.retries]
        raise


def _fail(err: RenfeError) -> NoReturn:
    typer.echo(render_error(err), err=True)
    raise typer.Exit(err.exit_code)


def _emit(produce: Callable[[], Any]) -> None:
    """Print `produce()` as JSON. Any failure becomes the stderr error payload: a RenfeError as is,
    anything else by its type only (messages can embed URLs, headers or token values).

    A payload carrying `fares_error` (Renfe blocked or failed a fare request mid-search) is a partial
    result: it is printed on stdout, the error goes to stderr, and the exit code is the error's (5),
    so scripts checking `$?` stop too."""
    try:
        payload = produce()
        text = render(payload)
    except (typer.Exit, typer.Abort):
        raise
    except RenfeError as err:
        _fail(err)
    except Exception as exc:
        _fail(RenfeError(f"unexpected internal error: {type(exc).__name__}"))
    typer.echo(text)
    partial = payload.get("fares_error") if isinstance(payload, dict) else None
    if isinstance(partial, dict):
        retried = {"retries": payload["retries"]} if payload.get("retries") else {}
        typer.echo(json.dumps({"error": {**partial, **retried}}, ensure_ascii=False, allow_nan=False), err=True)
        raise typer.Exit(int(partial.get("exit_code") or 5))


# A safety bound, not Renfe's rule: www.renfe.com's search form loads at most 12 months of prices
# (DEFAULT_MAX_MONTHS_TO_CHARGE in its script, 2026-11-16). Beyond that a date is a typo.
_MAX_DAYS_AHEAD = 366


def _parse_date(text: str) -> date:
    today = datetime.now(search_mod.MADRID).date()
    word = text.strip().lower()
    if word in ("today", "hoy"):
        return today
    if word in ("tomorrow", "mañana", "manana"):
        return today + timedelta(days=1)
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", word):
            raise ValueError
        day = date.fromisoformat(word)
    except ValueError:
        raise UsageError(f"--date must be YYYY-MM-DD, today or tomorrow (got {text!r})") from None
    if day < today:
        raise UsageError(f"--date {day.isoformat()} is in the past")
    if day > today + timedelta(days=_MAX_DAYS_AHEAD):
        raise UsageError(f"--date {day.isoformat()} is more than a year ahead; Renfe doesn't sell that far")
    return day


def _parse_time(text: str | None, option: str) -> time | None:
    if text is None:
        return None
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", text.strip()):
        raise UsageError(f"{option} must be HH:MM (got {text!r})")
    hours, minutes = text.strip().split(":")
    return time(int(hours), int(minutes))


def _print_version(value: bool) -> None:
    if value:
        typer.echo(package_version() or "unknown")
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True, help="Print the version and exit.")
    ] = False,
) -> None:
    pass


@app.command("stations")
def stations_cmd(
    query: Annotated[str | None, typer.Argument(help="Part of a station name, or a code.")] = None,
    limit: Annotated[int, typer.Option(min=1, max=2000, help="Most results to show.")] = 20,
    refresh: Annotated[bool, typer.Option(
        help="Ask Renfe for its current list instead of using the one shipped with the CLI.")] = False,
) -> None:
    """List stations matching QUERY (all of them without one), best match first."""

    def pick(every: list[stations_mod.Station]) -> list[stations_mod.Station]:
        return stations_mod.find(query, every, limit) if query else every[:limit]

    def produce() -> dict:
        found = [] if refresh else pick(stations_mod.bundled())
        if not found:  # asked to, or nothing in the shipped list matches: Renfe's live list
            with _client() as client:
                found = pick(stations_mod.load(client, refresh=refresh))
        out = {"query": query, "count": len(found), "stations": [s.to_dict() for s in found]}
        if not found:
            out["suggestions"] = [s.to_dict() for s in stations_mod.suggestions(query or "")]
        return out

    _emit(produce)


def _stations(client: RsvClient, origin: str, destination: str) -> tuple[stations_mod.Station, stations_mod.Station]:
    src, dst = stations_mod.lookup(origin, client), stations_mod.lookup(destination, client)
    if src.code == dst.code:
        raise UsageError("origin and destination are the same station")
    return src, dst


def _names(*picked: stations_mod.Station) -> dict[str, str]:
    """Station names by code, for the legs of a search; a code the shipped list lacks keeps no name."""
    return {s.code: s.name for s in stations_mod.bundled()} | {s.code: s.name for s in picked}


def _live_trains(trains: list[dict]) -> None:
    """`live_train` beside each `delay_minutes`: the number on the map it came from (a published partner's when the
    ticket number isn't there), or None with no delay. A train's comes from the leg its delay comes from."""
    for train in trains:
        parts = train.get("legs") or [train]
        for part in parts:
            known = part["delay_minutes"] is not None
            part["live_train"] = (part.get("live_train") or part["service"].zfill(5)) if known else None
        if "legs" in train:
            first = next((part for part in parts if part["delay_minutes"] is not None), None)
            train["delay_minutes"] = first["delay_minutes"] if first else None
            train["live_train"] = first["live_train"] if first else None


def _estimates(trains: list[dict], board: live.Board | None) -> None:
    """`estimated_departure` beside each `delay_minutes` (live.estimated_departure). A train with a change takes its
    first leg's: when the journey starts, so `null` while that train isn't running, whatever a later leg has."""
    for train in trains:
        parts = train.get("legs") or [train]
        for part in parts:
            part["estimated_departure"] = live.estimated_departure(part, board)
        if "legs" in train:
            train["estimated_departure"] = parts[0]["estimated_departure"]


def _add_notice(out: dict, text: str) -> None:
    out["notice"] = f"{out['notice']} {text}" if out["notice"] else text


@app.command("search")
def search_cmd(
    origin: Annotated[str, typer.Argument(help="From: a station name (Madrid, Atocha) or code (MADRI, 60000).")],
    destination: Annotated[str, typer.Argument(help="To: a station name or code.")],
    date_: Annotated[str, typer.Option("--date", "-d", help="Travel date: YYYY-MM-DD, today or tomorrow.")] = "today",
    after: Annotated[str | None, typer.Option(
        help="Only trains leaving at or after HH:MM, local time (today: defaults to now).")] = None,
    arrive_by: Annotated[str | None, typer.Option(
        help="Only trains scheduled to arrive by HH:MM on the search date, Madrid time.")] = None,
    fares: Annotated[bool, typer.Option(help="Also fetch fares (one extra request per 3 trains).")] = True,
    limit: Annotated[int, typer.Option(min=1, max=200, help="Most trains to return, earliest first.")] = 10,
) -> None:
    """Trains from ORIGIN to DESTINATION on a date, with fares for one adult (before 25 Jan 2027, from Renfe's
    legacy shop). For today, also each train's live delay, from Renfe's live train map, and for a train already
    running and due within 30 minutes, an estimated departure."""

    def produce() -> dict:
        day, after_time = _parse_date(date_), _parse_time(after, "--after")
        arrival_time = _parse_time(arrive_by, "--arrive-by")
        now = datetime.now(search_mod.MADRID)
        today = day == now.date()
        with _client() as client:
            src, dst = _stations(client, origin, destination)
            names = _names(src, dst)
            delays = live.in_background(client.transport) if today else None  # runs alongside the search
            out = search_mod.search(client, src, dst, day, names=names, after=after_time, fares=fares,
                                    arrive_by=arrival_time, limit=limit, now=now, from_now=today and after_time is None)
        board, why, map_retries = delays() if delays else (None, None, [])
        try:
            live.annotate(out["trains"], board)
        except Exception as exc:  # the map never fails a search: a bug here leaves it without delays
            live.annotate(out["trains"], None)
            board, why = None, f"{live.LABEL}: unexpected error ({type(exc).__name__})"
        unchecked = None
        if board is not None:
            try:
                unchecked = delays_mod.ticket_delays(out["trains"], board, names, transport=client.transport)
            except Exception as exc:  # nor does a ticket-number check: those legs keep no delay
                unchecked = f"unexpected error ({type(exc).__name__})"
        _live_trains(out["trains"])
        try:
            _estimates(out["trains"], board)
        except Exception:  # nor does an estimate: a bug here leaves every train without one, and keeps the delays
            _estimates(out["trains"], None)
        out["delays_included"] = board is not None
        if why:
            _add_notice(out, f"Delays are unknown ({why}).")
        if unchecked:
            _add_notice(out, f"Delays of trains sold under another number are unknown ({unchecked}).")
        out["retries"] = client.retries + map_retries
        return out

    _emit(produce)


def _stdout_is_terminal() -> bool:
    return sys.stdout.isatty()


def _service_parts(text: str) -> list[str] | None:
    """`03081` -> ["3081"]; `00601 + 00533` -> ["601", "533"]; None when it isn't a train number."""
    compact = re.sub(r"\s*\+\s*", "+", text.strip())  # spaces allowed around "+", not inside a number
    if not re.fullmatch(r"[0-9]{1,6}(\+[0-9]{1,6})*", compact):
        return None
    return [part.lstrip("0") or "0" for part in compact.split("+")]


def _train_number(text: str) -> str:
    parts = _service_parts(text)
    if parts is None or len(parts) != 1:
        raise UsageError(f"TRAIN must be one train number such as 03081; for a train with a change, one leg's "
                         f"(got {text!r})")
    return parts[0]


@app.command("delays")
def delays_cmd(
    train: Annotated[str | None, typer.Argument(help="Filter by the live number or a verified ticket number.")] = None,
) -> None:
    """List delayed trains with scheduled times and the train numbers used along each route."""

    def produce() -> dict:
        wanted = _train_number(train) if train is not None else None
        with _client() as client:
            board = live.fetch(transport=client.transport, retries=client.retries)
            return delays_mod.report(board, _names(), transport=client.transport, retries=client.retries, train=wanted)

    _emit(produce)


@app.command("status")
def status_cmd(
    train: Annotated[str, typer.Argument(help="A live train number or ticket number, e.g. 03081.")],
) -> None:
    """A train's live delay and its next station, from Renfe's live train map (long- and medium-distance trains,
    while they run)."""

    def produce() -> dict:
        number = _train_number(train)
        with _client() as client:
            board = live.fetch(transport=client.transport, retries=client.retries)
            return delays_mod.status(board, number, _names(), transport=client.transport, retries=client.retries)

    _emit(produce)


_sleep, _clock = time_mod.sleep, time_mod.monotonic  # tests replace these
_OUTAGE = object()  # the last line said the map was unreachable


def _line(payload: dict) -> None:
    typer.echo(json.dumps(payload, ensure_ascii=False, allow_nan=False))  # echo flushes: fine in a pipe


@app.command("watch")
def watch_cmd(
    train: Annotated[str, typer.Argument(help="A live train number or a ticket number, e.g. 03081.")],
) -> None:
    """Check a live or ticket number every 2 minutes. Print a JSON line shaped like `renfe status` on the first
    check and whenever the train's delay, next station, expected arrival there, overdue flag or presence changes.
    A ticket lookup lists multiple verified partners in `matching_trains`. If the timetable fails, a line
    explains why partner lookup is unavailable for the rest of the watch. If the requested number appears
    after a partner, print its status and follow it. Events report map outages and the 12-hour time limit.
    Press Ctrl-C to stop early."""
    try:
        number = _train_number(train)
    except UsageError as err:
        _fail(err)
    last, unreadable, last_matches = None, 0, None
    reported_unavailable = False
    deadline = _clock() + config.WATCH_LIMIT
    try:
        with _client() as client:
            watch = delays_mod.Watch(number, _names(), client.transport)
            while True:
                retried: list[dict] = []  # this check's: not carried to later lines
                try:
                    board = live.fetch(transport=client.transport, retries=retried)
                    previous_pin = watch.pinned
                    now = watch.check(board, retried)
                except (UpstreamError, ApiShapeError) as err:  # a hiccup: one line per outage, and it keeps watching
                    unreadable = unreadable + 1 if isinstance(err, ApiShapeError) else 0
                    if unreadable >= config.WATCH_MAX_UNREADABLE:
                        err.retries = retried
                        _fail(err)  # answer after answer isn't the map: it has moved or changed
                    if last is not _OUTAGE:
                        _line({**watch.event_numbers(), "event": "map_unreachable", "error": err.to_dict(),
                               "retries": retried})
                        last = _OUTAGE  # so the first answer after it prints a line, changed or not
                except RenfeError as err:  # blocked or rate-limited: stop, as every command does
                    err.retries = retried
                    _fail(err)
                except Exception as exc:
                    _fail(RenfeError(f"unexpected internal error: {type(exc).__name__}"))
                else:
                    unreadable = 0
                    nxt = now["next_station"] or {}
                    seen = (now["on_map"], now["delay_minutes"], nxt.get("code"), nxt.get("expected_arrival"),
                            now["overdue"])
                    matches = now.get("matching_trains")
                    unavailable = now["schedule_match"] == "unavailable"
                    if (seen != last or watch.pinned != previous_pin or (matches and matches != last_matches)
                            or (unavailable and not reported_unavailable)):
                        _line(now)
                        last = seen
                        if matches:
                            last_matches = matches
                        if unavailable:
                            reported_unavailable = True
                left = deadline - _clock()
                if left <= 0:
                    _line({**watch.event_numbers(), "event": "ended",
                           "message": f"watched for {config.WATCH_LIMIT / 3600:g} h; run it again to keep watching"})
                    return
                _sleep(min(config.WATCH_INTERVAL, left))
    except KeyboardInterrupt:  # the way to stop it before the time limit: exit 0
        return


@app.command("open")
def open_cmd(
    origin: Annotated[str, typer.Argument(help="From: a station name (Madrid, Atocha) or code (MADRI, 60000).")],
    destination: Annotated[str, typer.Argument(help="To: a station name or code.")],
    date_: Annotated[str, typer.Option("--date", "-d", help="Travel date: YYYY-MM-DD, today or tomorrow.")] = "today",
    train: Annotated[str | None, typer.Option(
        "--train", "-t", help="A service number from `renfe search` (e.g. 03081): checked against that day's "
                              "trains and shown, so you know which one to pick on the page.")] = None,
    browser: Annotated[bool | None, typer.Option(
        "--browser/--no-browser",
        help="Open the link in your default browser. Default: only when run from a terminal, so scripts "
             "and agents get the link without a browser popping up.")] = None,
) -> None:
    """Open Renfe's website on this search, to pick the train and pay there."""

    def produce() -> dict:
        day = _parse_date(date_)
        wanted = _service_parts(train) if train is not None else None
        if train is not None and wanted is None:
            raise UsageError(f"--train must be a train number such as 03081 or 00601+00533 (got {train!r})")
        with _client() as client:
            src, dst = _stations(client, origin, destination)
            chosen, notices = None, []
            if wanted is not None:
                found = search_mod.search(client, src, dst, day, names=_names(src, dst), fares=False)
                chosen = next((t for t in found["trains"] if _service_parts(t["service"]) == wanted), None)
                # Without fares, search() has a notice only when its list may be missing trains: the legacy shop
                # failed before the cut-off, or Renfe's new system reported more trains than it returned.
                if found["notice"]:
                    notices.append(found["notice"])
                if chosen is None and not found["notice"]:
                    message = f"no train {train} from {src.name} to {dst.name} on {day.isoformat()}; `renfe search` lists them"
                    if day == datetime.now(search_mod.MADRID).date() and day < config.NSV_FIRST_SALE_DATE:
                        message += (". The legacy shop stops listing a train about 5 minutes before it leaves the "
                                    "searched station.")
                    raise UsageError(message)
                if chosen is None:
                    notices.append(f"Train {train} isn't in the list this CLI can see for {day.isoformat()}, "
                                   "which is incomplete; look for it on Renfe's page.")
                elif datetime.fromisoformat(chosen["departure"]) < datetime.now(search_mod.MADRID):
                    first_leg = chosen["service"].split("+")[0].strip()
                    notices.append(f"Train {chosen['service']} was scheduled to leave at {chosen['departure'][11:16]}, "
                                   f"which has passed. Check `renfe status {first_leg}` for its reported delay.")
        page = shop_mod.link(src, dst, day)
        launch = _stdout_is_terminal() if browser is None else browser  # scripts and agents: link only
        opened = shop_mod.open_in_browser(page["url"]) if launch else False
        return {"date": day.isoformat(), "origin": src.to_dict(), "destination": dst.to_dict(),
                "train": chosen, "notice": " ".join(notices) or None, "shop": page, "opened": opened}

    _emit(produce)


def main(argv: Sequence[str] | None = None) -> None:
    """Entry point. Runs Typer without its standalone error printing, so that usage errors (a missing
    argument, an out-of-range option) come out as the same JSON error payload as everything else."""
    try:
        rv = app(args=list(argv) if argv is not None else None, standalone_mode=False)
    except click_exceptions.NoArgsIsHelpError as exc:
        exc.show()
        sys.exit(0)
    except click_exceptions.ClickException as exc:
        typer.echo(render_error(UsageError(exc.format_message())), err=True)
        sys.exit(2)
    except typer.Abort:
        sys.exit(1)
    sys.exit(rv if isinstance(rv, int) else 0)
