"""Train search: the timetable (search-travel), then fares for those trains (journey-search). Before the
cut-off, Renfe's legacy shop gives both in one search instead (legacy.py); if it fails, the new system's
partial timetable is shown without fares.

Request shapes copied from one anonymous web search (venta.rsv.renfe.com, 2026-11-15). Fares are for
one adult with no discount card: with discounts or promos the web app adds a reCAPTCHA token, which
this client does not do.

Any unexpected response shape becomes ApiShapeError (exit 7), never a crash.
"""

import math
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import config, legacy, shop
from .client import RsvClient, search_slot
from .errors import ApiShapeError, BlockedError, RenfeError
from .stations import Station

MADRID = ZoneInfo(config.TIMEZONE)
# The legacy shop stops listing a train about 5 minutes before it leaves. Today's searches every 15 minutes on two
# routes, 18-20 Nov 2026: of 74 trains due within 15 minutes of a search, the 6 due in under 5 minutes were missing
# and the 68 due in 5.8 minutes or more were listed. The extra minute covers the clocks.
LEGACY_LISTING_LEAD = timedelta(minutes=6)
_SHAPE_ERRORS = (KeyError, IndexError, TypeError, AttributeError, ValueError, OverflowError)


def utc_window(day: date) -> tuple[str, str]:
    """Madrid midnight to the next midnight, in UTC, as the web app sends it."""
    start = datetime.combine(day, time.min, MADRID)
    end = datetime.combine(day + timedelta(days=1), time.min, MADRID)
    fmt = lambda t: t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return fmt(start), fmt(end)


def _utc(text: Any, where: str) -> datetime:
    """Parse an ISO timestamp (Z, ±HHMM or ±HH:MM) to an aware UTC datetime."""
    if not isinstance(text, str):
        raise ApiShapeError(f"{where}: missing timestamp")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            raise ApiShapeError(f"{where}: unreadable timestamp") from None
    if parsed.tzinfo is None:
        raise ApiShapeError(f"{where}: timestamp without a timezone")
    return parsed.astimezone(timezone.utc)


def _local(moment: datetime, tz_name: Any) -> str:
    """The station's local time as Renfe labels it (Europe/Madrid unless it says otherwise)."""
    try:
        tz = ZoneInfo(tz_name) if isinstance(tz_name, str) and tz_name else MADRID
    except Exception:
        tz = MADRID
    return moment.astimezone(tz).isoformat()


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ApiShapeError(f"{where}: missing or non-text field")
    return value


def _leg(path: Any, names: dict[str, str]) -> dict:
    where = "search-travel leg"
    if not isinstance(path, dict):
        raise ApiShapeError(f"{where}: not an object")
    origin = _text(path.get("origin_stop_code"), where)
    destination = _text(path.get("destination_stop_code"), where)
    dep = _utc(path.get("departure_datetime"), where)
    arr = _utc(path.get("arrival_datetime"), where)
    stops = path.get("intermediate_stop_count")
    return {
        "service": _text(path.get("service_name"), where),
        "type_code": _text(path.get("service_type_code"), where),
        "origin": {"code": origin, "name": names.get(origin)},
        "destination": {"code": destination, "name": names.get(destination)},
        "departure": _local(dep, path.get("departure_datetime_timezone")),
        "arrival": _local(arr, path.get("arrival_datetime_timezone")),
        "intermediate_stops": stops if isinstance(stops, int) and not isinstance(stops, bool) else None,
        # kept for the fares request; dropped from the output
        "_dep_utc": dep,
        "_arr_utc": arr,
        "_schedule_date": _text(path.get("schedule_date"), where),
    }


def _route_key(legs: list[tuple[Any, datetime]]) -> tuple:
    return tuple((service, moment.strftime("%Y-%m-%dT%H:%M")) for service, moment in legs)


def _fares_request(origin: Station, destination: Station, day: date, trains: list[dict]) -> dict:
    routes = [{"legs": [{
        "origin": leg["origin"]["code"],
        "destination": leg["destination"]["code"],
        "departure": leg["_dep_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arrival": leg["_arr_utc"].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "service_name": leg["service"],
        "service_type": leg["type_code"],
        "service_schedule_date": leg["_schedule_date"],
    } for leg in train["legs"]]} for train in trains]
    return {
        "is_aftersales": False,
        "passengers": [{"disability_type": "SD", "type": "A", "id": "passenger_1",
                        "travel_passes": [], "discount_cards": []}],
        "travels": [{"direction": "outbound", "origin": origin.code, "destination": destination.code,
                     "excluded_product_types": list(config.EXCLUDED_PRODUCT_TYPES), "id": "travel_outbound",
                     "departure": day.isoformat(), "routes": routes}],
        "promos": [],
    }


def _parse_fares(doc: Any) -> dict[tuple, dict]:
    """Route key -> {"available", "type", "fares"} from a journey-search response."""
    try:
        return _parse_fares_unchecked(doc)
    except ApiShapeError:
        raise
    except _SHAPE_ERRORS:
        raise ApiShapeError("journey-search: unexpected response shape") from None


def _parse_fares_unchecked(doc: Any) -> dict[tuple, dict]:
    offer = doc["data"]["offer"]
    routes = offer["travels"][0]["routes"]
    if not isinstance(routes, list):
        raise ApiShapeError("journey-search: routes is not a list")
    families = {f.get("id"): f.get("name") for f in offer.get("product_families") or [] if isinstance(f, dict)}
    out: dict[tuple, dict] = {}
    for route in routes:
        legs = route["legs"]
        if not isinstance(legs, list) or not legs:
            raise ApiShapeError("journey-search: a route has no legs")
        key = _route_key([(leg["service_name"],
                           _utc(leg["departure_station"]["departure_timestamp"], "journey-search leg"))
                          for leg in legs])
        fares = []
        for bundle in route.get("bundles") or []:
            price = bundle.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                continue
            if not math.isfinite(price) or price < 0:
                raise ApiShapeError("journey-search: a fare has no usable price")
            name = families.get(bundle.get("product_family_id")) or bundle.get("product_family_id")
            fares.append({"name": name if isinstance(name, str) else None, "price": round(float(price), 2),
                          "currency": bundle.get("currency") if isinstance(bundle.get("currency"), str) else "EUR"})
        fares.sort(key=lambda f: f["price"])
        availability = route.get("bundle_availability")
        available = availability.get("availability") if isinstance(availability, dict) else None
        types = [leg.get("service_type") for leg in legs]
        type_names = [t.get("name") if isinstance(t, dict) else None for t in types]
        out[key] = {
            "available": available if isinstance(available, bool) else None,
            # one name per leg, as the web app shows a train with a change ("INTERCITY + INTERCITY")
            "type": " + ".join(type_names) if all(isinstance(n, str) for n in type_names) else None,
            "fares": fares,
        }
    return out


def _timetable(doc: Any, names: dict[str, str]) -> tuple[list[dict], int | None]:
    if not isinstance(doc, dict) or not isinstance(doc.get("travel_options"), list):
        raise ApiShapeError("search-travel: no travel_options list")
    trains = []
    for option in doc["travel_options"]:
        path = option.get("travel_path") if isinstance(option, dict) else None
        if not isinstance(path, list) or not path:
            raise ApiShapeError("search-travel: an option has no travel_path")
        legs = [_leg(p, names) for p in path]
        duration = option.get("travel_duration")
        trains.append({
            "service": "+".join(leg["service"] for leg in legs),
            "type": None,
            "type_code": legs[0]["type_code"],
            "departure": legs[0]["departure"],
            "arrival": legs[-1]["arrival"],
            "duration_minutes": duration if isinstance(duration, int) and not isinstance(duration, bool) else None,
            "changes": len(legs) - 1,
            "legs": legs,
            "available": None,
            "fares": None,
        })
    total = doc.get("total_count")
    return trains, total if isinstance(total, int) and not isinstance(total, bool) else None


_SKIPPED = object()  # a fare batch never sent because another one failed


def _fetch_fares(client: RsvClient, origin: Station, destination: Station, day: date,
                 batches: list[list[dict]]) -> list[Any]:
    """One journey-search call per batch, up to FARE_WORKERS at once, started in batch order. Returns,
    in batch order, each response, the BlockedError it raised, or _SKIPPED when it never ran (or failed
    some other way) after another call had failed.

    Among the calls that ran, a fixed rule picks the outcome: a bug (not a RenfeError) is raised;
    otherwise, if any call was blocked, the caller gets a partial result (exit 5, timetable kept);
    otherwise the first error in batch order is raised. Which later batches get sent at all after a
    failure still depends on timing. Workers are daemon threads, so Ctrl-C never waits for a call."""
    answers: list[Any] = [_SKIPPED] * len(batches)
    pending = list(range(len(batches)))
    lock = threading.Lock()
    failed = threading.Event()

    def work() -> None:
        while not failed.is_set():
            with lock:
                if not pending:
                    return
                index = pending.pop(0)
            try:
                answers[index] = client.post(config.JOURNEY_SEARCH_PATH,
                                             _fares_request(origin, destination, day, batches[index]),
                                             label="journey-search", may_resend=lambda: not failed.is_set())
            except BaseException as err:  # kept for the main thread; start no more calls
                answers[index] = err
                failed.set()

    workers = [threading.Thread(target=work, name=f"renfe-fares-{n}", daemon=True)
               for n in range(max(1, min(config.FARE_WORKERS, len(batches))))]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join()
    except BaseException:  # Ctrl-C: queued batches start nothing; calls in flight die with the process
        failed.set()
        raise

    errors = [a for a in answers if isinstance(a, BaseException)]
    bugs = [e for e in errors if not isinstance(e, RenfeError)]
    if bugs:
        raise bugs[0]
    if any(isinstance(e, BlockedError) for e in errors):
        return [_SKIPPED if isinstance(a, BaseException) and not isinstance(a, BlockedError) else a
                for a in answers]
    if errors:
        raise errors[0]
    return answers


def search(
    client: RsvClient,
    origin: Station,
    destination: Station,
    day: date,
    *,
    names: dict[str, str] | None = None,
    after: time | None = None,
    arrive_by: time | None = None,
    fares: bool = True,
    limit: int | None = None,
    now: datetime | None = None,
    from_now: bool = False,
) -> dict:
    """Trains from `origin` to `destination` on `day` (Madrid date), with fares for one adult. Before the
    cut-off they come from the legacy shop; if it fails, from the new system's partial list without fares,
    with a notice. `after` keeps trains leaving at or after that local time. `now` (aware), when given, lets an
    empty legacy list for today count as the day being over (see below). The CLI sets `from_now` for today
    without an explicit departure time. `arrive_by` is an inclusive deadline on the search date in Madrid."""
    search_slot(config.SEARCH_SPACING, config.cache_dir() / config.SEARCH_PACE_FILE)
    names = names or {}
    notices: list[str] = []
    sold_here = day >= config.NSV_FIRST_SALE_DATE
    trains, legacy_failure, legacy_empty = None, None, False
    if not sold_here:  # the legacy shop sells these dates, and lists every train with its fares
        try:
            trains = legacy.trains(origin, destination, day, names, transport=client.transport,
                                   retries=client.retries)
        except RenfeError as err:
            legacy_failure = err.message
        # Not trusted: an unknown station or a changed answer may look like this too (checked below).
        if trains == []:
            trains, legacy_failure, legacy_empty = None, "it listed no trains", True
    from_legacy = trains is not None
    if trains is None:
        after_utc, before_utc = utc_window(day)
        doc = client.post(config.SEARCH_TRAVEL_PATH, {
            "depart_after_datetime": after_utc,
            "depart_before_datetime": before_utc,
            "travel_path": [{"code": origin.code}, {"code": destination.code}],
            "direction": "OUTBOUND",
        }, label="search-travel")
        trains, total = _timetable(doc, names)
        if total is not None and total > len(trains):
            notices.append(f"Renfe reported {total} trains but returned {len(trains)}; some may be missing.")
    minute = now.replace(second=0, microsecond=0) if now is not None else None  # as the CLI's default `after`
    leaves = lambda t: datetime.fromisoformat(t["departure"])
    shop_would_list = now is not None and any(leaves(t) > now + LEGACY_LISTING_LEAD for t in trains)
    if from_now and minute is not None:
        # Moments, not clock faces: the night the clocks go back, 02:20 CET comes after 02:40 CEST, and a station's
        # local time (Renfe labels each one) needn't be Madrid's. `after` stays for the notice below.
        after = minute.time()
        trains = [t for t in trains if leaves(t) >= minute]
    elif after:
        trains = [t for t in trains if leaves(t).time() >= after]
    leaving_now = False
    if (legacy_empty and minute is not None and day == minute.date() and not shop_would_list
            and all(leaves(t) >= minute for t in trains)):
        # The shop lists only trains leaving in more than about 5 minutes. Today, with none of those in the new
        # system's list either, nothing failed: the day is over (nothing in the list shown), or the last trains are
        # leaving now (those it shows). Not on other days, and not when `after` keeps other trains: before the
        # cut-off the new system lists only part of the timetable, so a list from it needs the notice, and its being
        # empty proves little.
        legacy_failure, leaving_now = None, bool(trains)
    # Both end-of-day checks describe departures, before the arrival deadline can hide any of them.
    no_more_departures = from_now and not trains
    if arrive_by is not None:
        deadline = datetime.combine(day, arrive_by, MADRID)
        trains = [t for t in trains if datetime.fromisoformat(t["arrival"]) <= deadline]
    trains.sort(key=lambda t: t["legs"][0]["_dep_utc"])
    if limit is not None:
        trains = trains[:limit]

    if from_legacy and not fares:
        for train in trains:
            train.update(available=None, fares=None)
    fares_error = None
    if fares and trains and sold_here:
        batches = [trains[i:i + config.FARE_BATCH] for i in range(0, len(trains), config.FARE_BATCH)]
        answers = _fetch_fares(client, origin, destination, day, batches)
        unpriced = 0
        for batch, answer in zip(batches, answers):
            if answer is _SKIPPED or isinstance(answer, BlockedError):
                if isinstance(answer, BlockedError) and fares_error is None:
                    fares_error = answer.to_dict()  # the first block in batch order
                unpriced += len(batch)
                continue
            priced = _parse_fares(answer)
            matched = 0
            for train in batch:
                hit = priced.get(_route_key([(leg["service"], leg["_dep_utc"]) for leg in train["legs"]]))
                if hit:
                    matched += 1
                    train.update(available=hit["available"], fares=hit["fares"], type=hit["type"])
            if priced and not matched:
                raise ApiShapeError("journey-search: no returned fare matches the requested trains")
            unpriced += len(batch) - matched
        if fares_error is not None:
            notices.append(f"Renfe stopped answering fare requests; {unpriced} train(s) have no fares. "
                           "Wait before trying again.")
        elif unpriced:
            notices.append(f"{unpriced} train(s) came back without fares.")
    if legacy_failure is not None:
        # Observed 2026-11-15: before the cut-off this API lists only part of the timetable. Madrid-Barcelona,
        # Fri 20 Nov 2026: 8 trains here vs 24 on the legacy shop (all 8 among them); the web never shows
        # this list (the new shop's calendar won't pick dates before the cut-off).
        notices.append(f"Renfe's legacy shop ({config.LEGACY_SHOP_URL}), which sells trips before "
                       f"{config.NSV_FIRST_SALE_DATE.isoformat()}, failed ({legacy_failure}), so this list is from "
                       f"Renfe's new system: {'no fares, and ' if fares else ''}it may be missing trains. "
                       "`shop.url` opens the full list on Renfe's website.")

    if leaving_now:
        notices.append(f"Renfe's legacy shop ({config.LEGACY_SHOP_URL}) stops listing a train about 5 minutes before "
                       f"it leaves, so the trains leaving now are from Renfe's new system"
                       f"{', without fares' if fares else ''}.")

    if no_more_departures and after is not None:
        notices.append(f"No more trains today after {after:%H:%M}; try --date tomorrow.")

    for train in trains:
        for leg in train["legs"]:
            for private in ("_dep_utc", "_arr_utc", "_schedule_date"):
                leg.pop(private, None)
        if train["changes"] == 0:
            train["intermediate_stops"] = train["legs"][0]["intermediate_stops"]
            del train["legs"]

    out = {
        "date": day.isoformat(),
        "origin": origin.to_dict(),
        "destination": destination.to_dict(),
        "passengers": {"adults": 1},
        "fares_included": bool(fares and (sold_here or from_legacy) and fares_error is None),
        "notice": " ".join(notices) or None,
        "count": len(trains),
        "trains": trains,
        "shop": shop.link(origin, destination, day),  # where to pick the train and pay
    }
    if fares_error:
        out["fares_error"] = fares_error
    return out
