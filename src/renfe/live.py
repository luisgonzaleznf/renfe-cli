"""Live delays from Renfe's live train map (config.LIVE_MAP_URL): public JSON, no key or session, long- and
medium-distance trains only, and only while they run. A different host from the search's.

Each entry (observed 2026-11-17 and 18: 95 trains at 17:48, one or none at night) has the five-digit train
number, as `renfe search` writes it; the last delay Renfe recorded, in minutes as a string (negative: early); the
previous and next station, as codes of the shipped list; the expected arrival at the next station, in Madrid time;
and a Unix time that Renfe refreshes every few minutes, even on entries hours old (all 95 at 17:48 were within 4
minutes of the map's update). What the samples showed, and the code keeps in mind:
- A train missing from the map may not have left yet, have arrived or been cancelled, or not be a long- or
  medium-distance train: never "on time". At night the map lists no trains.
- The delay is the last one recorded. A train held somewhere keeps its delay and next station, with a fresh
  position time, while its expected arrival slips into the past (9 h past, for one at 17:48).
- The same station as previous and next: the train is probably stopped there.
- The map has no run date: after midnight a train can still be on yesterday's run. Its dates are unreliable then
  too: at 02:14 on 18 Nov it gave 2026-11-19T02:09 as the expected arrival of a train due at 02:10 that night
  (timetable 23:20 on the 17th, 170 minutes late), while the position time, a Unix timestamp, was right.
"""

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from . import config, retry
from .client import _checked_json
from .errors import ApiShapeError, AuthError, RenfeError, UpstreamError, UsageError

MADRID = ZoneInfo(config.TIMEZONE)  # the map's times have no zone
LABEL = "live map"
_NUMBER = re.compile(r"[0-9]{1,6}")
_MINUTES = re.compile(r"-?[0-9]{1,4}")
# A train on the map is on the run of a search's leg only if it was seen at most this long before the leg leaves
# or after it arrives. After midnight the map can still show yesterday's run of a train that leaves again tonight:
# that run would have to be still going 12 h after it left the leg's station. The right run is dropped only when
# it left more than 12 h before reaching the leg: Renfe's open timetable (Nov 2026) has a handful of trips of 12
# to 14 h, and its median is under 2.5 h.
_SAME_RUN = timedelta(hours=12)
# A train is overdue once Renfe's expected arrival at its next station is this far behind the map's time: it is
# probably held, and its delay is the last one recorded. Between stations, 5 min keeps a train just reaching its
# station from flipping it at every check (`watch` prints a line when it changes). At a station (previous and next
# the same), the expected arrival is when it got there, so 10 min leaves room for a stop. A train running early is
# never overdue: it is waiting for its time. On the 17:48 frame of 17 Nov this flags 10 of 95 trains, against 17
# with a flat 5 min; among those dropped is 17012, 8 min early and waiting at Plasencia.
_OVERDUE_MOVING = timedelta(minutes=5)
_OVERDUE_AT_STATION = timedelta(minutes=10)
# An entry whose expected arrival is further behind than this is left over, hours old, not a train being held for a
# while: `search` gives it no delay. On that frame the past arrivals were up to 24 min (18777, at a station) and
# then 62, 121, 454 and 538 min (71891, 71876, 08109, 18054: 08109 listed at its origin 7.5 h after it left, delay
# 0). 45 min sits in that gap with 20 min either side.
_STALE = timedelta(minutes=45)
# `search` estimates a leg's departure only this far ahead of the map's time. Renfe's map, recorded every 2 minutes from
# Fri 20 Nov 2026 15:40 to Sat 21 Nov 23:30: over 30 minutes a running train's delay changed by a median of 2 minutes,
# by 8 or more one time in ten, and by 5 or more in 29% of 68,026 pairs; over 60 minutes by a median of 4 (one time in
# ten, 14 or more), too much to call an estimate. It grew more often than it shrank (52% and 30% of the pairs).
ESTIMATE_AHEAD = timedelta(minutes=30)


@dataclass(frozen=True)
class Board:
    updated: datetime | None  # when Renfe last refreshed the map
    trains: dict[str, dict]  # by train number without leading zeros


def key(number: str) -> str:
    return number.lstrip("0") or "0"


class MapChanged(ApiShapeError):
    """The map's URL or answer isn't what this module reads (exit 7)."""

    def to_dict(self) -> dict:
        return {**super().to_dict(), "hint": "Renfe's live map may have moved or changed; check config.LIVE_MAP_URL "
                                             "and update renfe/live.py."}


def fetch(*, transport: httpx.BaseTransport | None = None, timeout: float = config.LIVE_TIMEOUT,
          retries: list[dict] | None = None, deadline: float | None = None) -> Board:
    """The map as it is now. Raises UpstreamError when it doesn't answer, BlockedError on a block, ApiShapeError
    when the answer isn't the map. A one-off failure (a 5xx, a dropped connection) is tried again, up to 3 tries
    (retry.py), and logged in `retries`; with a `deadline` (a time.monotonic() value), only while another try can
    still end before it."""
    return retry.call(LABEL, lambda: _fetch_once(transport, timeout), retries, deadline=deadline)


def _fetch_once(transport: httpx.BaseTransport | None, timeout: float) -> Board:
    with httpx.Client(transport=transport, timeout=timeout, follow_redirects=False,
                      headers={"user-agent": config.USER_AGENT, "accept-language": config.ACCEPT_LANGUAGE}) as http:
        try:
            response = http.get(config.LIVE_MAP_URL, headers={"accept": "application/json"})
        except httpx.TimeoutException:  # never retried: it already cost the whole timeout
            raise UpstreamError(f"{LABEL}: Renfe did not answer in time") from None
        except httpx.RequestError as exc:
            raise UpstreamError(f"{LABEL}: request failed ({type(exc).__name__})",
                                transient=retry.connection_failure(exc)) from None
    try:
        doc = _checked_json(response, LABEL)  # a 5xx is transient; an HTML page, at any status, is a block
    except (UsageError, AuthError) as err:  # a 401 or 404 here means the map moved, not a bad argument
        raise MapChanged(f"{LABEL}: unexpected HTTP status {err.status}", status=err.status) from None
    except ApiShapeError as err:
        raise MapChanged(err.message, status=err.status) from None
    return parse(doc)


def parse(doc: Any) -> Board:
    """The map's trains by number. An entry without a usable number is skipped; a field that doesn't read is None."""
    if not isinstance(doc, dict) or not isinstance(doc.get("trenes"), list):
        raise MapChanged(f"{LABEL}: no list of trains")
    trains, updated = {}, _madrid(doc.get("fechaActualizacion"))
    for raw in doc["trenes"]:
        number = raw.get("codComercial") if isinstance(raw, dict) else None
        if isinstance(number, str) and _NUMBER.fullmatch(number):
            reported = _epoch(raw.get("time"))
            trains.setdefault(key(number), {
                "number": number,
                "delay": _minutes(raw.get("ultRetraso")),
                "origin": _code(raw.get("codOrigen")),
                "destination": _code(raw.get("codDestino")),
                "previous": _code(raw.get("codEstAnt")),
                "next": _code(raw.get("codEstSig")),
                "expected": _not_ahead(_madrid(raw.get("horaLlegadaSigEst")), reported or updated),
                "reported_at": reported,
            })
    return Board(updated=updated, trains=trains)


def status(board: Board, number: str, names: dict[str, str], retries: list[dict] | None = None) -> dict:
    """Train `number` on the map: its delay, previous and next station, the caveats that apply, and the requests
    that had to be retried to get the map."""
    entry = board.trains.get(key(number))
    station = lambda code: {"code": code, "name": names.get(code)} if code else None
    out: dict = {"train": entry["number"] if entry else number.zfill(5), "on_map": entry is not None,
                 "delay_minutes": None, "origin": None, "destination": None, "previous_station": None,
                 "next_station": None, "overdue": None, "reported_at": None, "map_updated": _iso(board.updated),
                 "notice": None, "retries": list(retries or [])}
    if entry is None:
        empty = "Renfe's live map lists no trains right now (it is empty at night). " if not board.trains else ""
        out["notice"] = (f"{empty}Train {out['train']} isn't on Renfe's live map, which lists long- and "
                         "medium-distance trains only while they run: it may not have left yet, may have arrived "
                         "or been cancelled, or isn't one of those trains. Its delay is unknown, not zero.")
        return out
    nxt = station(entry["next"])
    if nxt:
        nxt["expected_arrival"] = _iso(entry["expected"])
    out.update(delay_minutes=entry["delay"], origin=station(entry["origin"]), destination=station(entry["destination"]),
               previous_station=station(entry["previous"]), next_station=nxt, reported_at=_iso(entry["reported_at"]))
    at_station = entry["previous"] == entry["next"]
    out["overdue"] = _overdue(entry, board)
    where = nxt and (nxt["name"] or nxt["code"])
    since = f", since about {entry['expected']:%H:%M}" if entry["expected"] else ""
    if nxt and at_station:  # Renfe gives that station as both previous and next
        held = ", longer than a stop: its delay is the last one Renfe recorded" if out["overdue"] else ""
        out["notice"] = f"The train is probably stopped at {where}{since}{held}."
    elif out["overdue"]:
        out["notice"] = (f"Its expected arrival at {where} ({entry['expected']:%H:%M}) has already passed, so the "
                         "delay is the last one Renfe recorded and may be growing.")
    return out


def _late(entry: dict, board: Board) -> timedelta | None:
    """How far the expected arrival at the next station is behind the map's time (negative: still ahead)."""
    now = board.updated or entry["reported_at"]
    return now - entry["expected"] if now and entry["expected"] else None


def _overdue(entry: dict, board: Board) -> bool:
    """The expected arrival at the next station is further behind the map's time than a stop (or a moment, between
    stations) allows: the train is probably held, and its delay is the last one recorded."""
    late = _late(entry, board)
    grace = _OVERDUE_AT_STATION if entry["previous"] == entry["next"] else _OVERDUE_MOVING
    return bool(entry["next"] and late is not None and late > grace and (entry["delay"] or 0) >= 0)


def in_background(transport: httpx.BaseTransport | None) -> Callable[[], tuple[Board | None, str | None, list[dict]]]:
    """Start fetching the map in a daemon thread, so a search doesn't wait for it. The function returned waits
    until config.LIVE_SEARCH_BUDGET seconds after the start, at most, then gives the map, or None and why, and the
    map's retries. It never raises: a search never fails, or changes its exit code, because of the map. A one-off
    failure is tried again only if the pause and another try can end within that budget; a try still running when
    the search stops waiting shows in the retries as not recovered."""
    budget = config.LIVE_SEARCH_BUDGET
    deadline, got, log = time.monotonic() + budget, {}, []

    def run() -> None:
        try:
            got["board"] = fetch(transport=transport, timeout=budget, retries=log, deadline=deadline)
        except RenfeError as err:
            got["why"] = err.message
        except Exception as exc:  # a bug here must not reach the search either
            got["why"] = f"{LABEL}: unexpected error ({type(exc).__name__})"

    thread = threading.Thread(target=run, name="renfe-live-map", daemon=True)
    thread.start()

    def result() -> tuple[Board | None, str | None, list[dict]]:
        thread.join(max(0.0, deadline - time.monotonic()))
        done = dict(got)  # read once, before the log: a board or a failure in it means the thread is done
        board, running = done.get("board"), not done
        retries = [{**e, "errors": list(e["errors"])} for e in log]  # copies: a running thread still changes them
        for e in retries if running else []:  # a re-sent try the search won't wait for
            e.update(tries=e["tries"] + 1, recovered=False,
                     errors=e["errors"] + [f"no answer within the search's {budget:g} s budget"])
        return board, None if board else done.get("why", f"{LABEL}: no answer within {budget:g} s"), retries

    return result


def annotate(trains: list[dict], board: Board | None) -> None:
    """Set `delay_minutes` on every train of a search: its delay on the map, or None when there is no map, the train
    isn't on it, or the entry looks like another day's run. Each leg in `legs` gets its own, and the train the first
    of them the map has: the leg the trip is on, or the next one to run."""
    for train in trains:
        legs = train.get("legs") or [train]
        found = [leg_delay(leg, board) for leg in legs]
        if "legs" in train:
            for leg, delay in zip(legs, found):
                leg["delay_minutes"] = delay
        train["delay_minutes"] = next((delay for delay in found if delay is not None), None)


def leg_delay(part: dict, board: Board | None, number: str | None = None) -> int | None:
    """The delay of the map entry for `number` (default: the leg's own), if it is this leg's run and not left over."""
    entry = board.trains.get(key(number or part["service"])) if board else None
    seen = entry and (entry["reported_at"] or board.updated)
    if not seen or entry["delay"] is None:
        return None
    late = _late(entry, board)
    if late is not None and late > _STALE:  # an entry left over: its number would read as live (even 0, on time)
        return None
    start, end = datetime.fromisoformat(part["departure"]), datetime.fromisoformat(part["arrival"])
    return entry["delay"] if start - _SAME_RUN <= seen <= end + _SAME_RUN else None


def estimated_departure(part: dict, board: Board | None) -> str | None:
    """For `search`: a running train's scheduled departure from the leg's station plus the delay the leg got
    (`delay_minutes`, from the map entry `live_train` names), counting an early train as on time, since it waits for
    its departure. None unless that time falls within ESTIMATE_AHEAD after the map's time: never in the past (the
    train has left, or its delay has grown since), never for a train overdue at its next station (its delay is the
    last one recorded and may be growing), and never for one still at its first station: it isn't running yet, and
    how late a train leaves its first station hasn't been measured (269 of 686 numbers on the map on Sat 21 Nov
    first showed up there, one of them for 192 minutes)."""
    delay, number = part.get("delay_minutes"), part.get("live_train")
    entry = board.trains.get(key(number)) if board is not None and number else None
    if entry is None or delay is None or _overdue(entry, board):
        return None
    if entry["previous"] == entry["next"] == entry["origin"]:  # still at its first station
        return None
    now = board.updated or entry["reported_at"]
    moment = datetime.fromisoformat(part["departure"]) + timedelta(minutes=max(delay, 0))
    return moment.isoformat() if now is not None and now <= moment <= now + ESTIMATE_AHEAD else None


def _code(value: Any) -> str | None:
    return value if isinstance(value, str) and _NUMBER.fullmatch(value) else None


def _madrid(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return moment.replace(tzinfo=MADRID) if moment and moment.tzinfo is None else moment


def _not_ahead(moment: datetime | None, around: datetime | None) -> datetime | None:
    """`moment` brought back by whole days when it is over 12 h ahead of `around`: after midnight the map can date an
    arrival a day ahead. One in the past stays as it is: a held train's can be many hours past."""
    if moment is None or around is None or moment - around <= timedelta(hours=12):
        return moment
    return moment - timedelta(days=round((moment - around) / timedelta(days=1)))


def _minutes(value: Any) -> int | None:
    """The delay: a string of minutes, as seen ("19", "-8"), or a JSON number."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return int(value) if isinstance(value, str) and _MINUTES.fullmatch(value) else None


def _epoch(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, MADRID)
    except (OverflowError, OSError, ValueError):
        return None


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None
