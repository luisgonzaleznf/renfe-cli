# renfe-cli

[![CI](https://github.com/luisgonzaleznf/renfe-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/luisgonzaleznf/renfe-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](pyproject.toml)

Search Renfe trains and fares, and follow live delays, from the terminal. Every command prints JSON, so
scripts and AI agents can use it as easily as people. No account, no API key, and no browser to search.

> **Unofficial.** Not affiliated with, endorsed by or supported by Renfe. The CLI talks directly to
> Renfe's own web API, the same anonymous way the website does before anyone signs in. That API is
> undocumented and can change without notice.

## Features

- **Trains and fares** for a route and date, up to a year ahead: times, duration, changes and every fare
  for one adult, cheapest first. For trips before 25 Jan 2027 it asks Renfe's legacy shop as a guest, so
  no train is missing.
- **Live delays** from Renfe's public train map: one train (`status`), a JSON line whenever it changes
  (`watch`), or every delayed train on the network with its scheduled times (`delays`). Today's `search`
  gives each train's delay too and, for a train already running and due within 30 minutes, an estimated
  departure from your station.
- **The number on your ticket works.** A train can run under another number for part of its route;
  `search`, `status`, `watch` and `delays` find the live train either way.
- **About 1,400 stations ship with the CLI**, matched by name (accents and case ignored) or code, with
  spelling suggestions for a typo.
- **Buy on Renfe's website.** `renfe open` opens the right shop with the route filled in.
- **Gentle with Renfe.** It sends the requests the website sends, under its own name, starts at most one
  search a second on a machine, retries only one-off failures, and stops when Renfe blocks it.

## Install

You need Python 3.12 or later and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/luisgonzaleznf/renfe-cli
renfe --help
```

It isn't on PyPI yet. The `renfe-cli` package on PyPI is a different project, so `pip install renfe-cli`
won't install this one.

## Quick start

```sh
renfe stations sevilla                                   # find a station or its code
renfe search Madrid Barcelona --date 2027-01-29
renfe search Atocha Sants -d tomorrow --after 17:00 --arrive-by 22:00 --limit 5
renfe search MADRI BARCE -d 2027-02-05 --no-fares        # timetable only
renfe open Madrid Barcelona -d 2027-01-29 --train 03081  # buy it on Renfe's website
renfe delays                                             # delayed trains, with scheduled endpoint times
renfe delays 37432                                       # a delayed train using this number on any verified section
renfe status 03081                                       # that train's live delay and next station
renfe watch 03081                                        # a JSON line each time that changes
```

A search prints one JSON document. This one ran on 19 Nov 2026 with `--after 08:00 --limit 2`; the second
train is left out here:

```json
{
  "date": "2027-01-29",
  "origin": {"code": "MADRI", "name": "Madrid (Todas)", "province": "Madrid", "group": true, "main": true},
  "destination": {"code": "BARCE", "name": "Barcelona (Todas)", "province": "Barcelona", "group": true, "main": true},
  "passengers": {"adults": 1},
  "fares_included": true,
  "notice": null,
  "count": 2,
  "trains": [
    {
      "service": "03091",
      "type": "AVE",
      "type_code": "AV1",
      "departure": "2027-01-29T08:57:00+01:00",
      "arrival": "2027-01-29T12:06:00+01:00",
      "duration_minutes": 189,
      "changes": 0,
      "available": true,
      "fares": [
        {"name": "Básico", "price": 44.07, "currency": "EUR"},
        {"name": "Elige", "price": 48.48, "currency": "EUR"},
        {"name": "Elige Confort", "price": 52.77, "currency": "EUR"},
        {"name": "Premium", "price": 71.77, "currency": "EUR"}
      ],
      "intermediate_stops": 1,
      "delay_minutes": null,
      "live_train": null,
      "estimated_departure": null
    }
  ],
  "shop": {
    "url": "https://venta.rsv.renfe.com/coext/search?originUicStationCode=MADRI&destinationUicStationCode=BARCE&isOneWay=true",
    "prefilled": ["origin", "destination", "one_way"],
    "next": "Pick 2027-01-29 as the date (Renfe's new shop doesn't take one in a link), press 'Buscar billetes' and pick your train."
  },
  "delays_included": false,
  "retries": []
}
```

So it pipes straight into other tools:

```sh
renfe search Madrid Barcelona -d 2027-01-29 --after 08:00 --limit 2 \
  | jq -r '.trains[] | "\(.departure[11:16]) \(.service) \(.type) from \(.fares[0].price) €"'
# 08:57 03091 AVE from 44.07 €
# 09:27 03093 AVE from 49.8 €
```

## Commands

| Command | What it does |
|---|---|
| `renfe stations [QUERY]` | Stations and city groups matching a name or code, best match first |
| `renfe search FROM TO` | Trains on a date, with times, changes and fares for one adult; for today, live delays and estimated departures too |
| `renfe open FROM TO` | Opens Renfe's shop for that search in your browser, or prints the link |
| `renfe delays [TRAIN]` | Every delayed train on the live map, with its scheduled departure and arrival |
| `renfe status TRAIN` | One train's live delay, its next station and the expected arrival there |
| `renfe watch TRAIN` | A JSON line each time that train's live status changes |

Every command has `--help`. stdout is always JSON (JSON Lines for `watch`), and errors go to stderr with
the exit codes in [Output and errors](#output-and-errors).

## Searching

`search` returns, for each train:
- service number and `type_code`, plus `type` (AVE, Intercity…) when fares are fetched; a train
  with a change gets one name per leg, as on the website (`INTERCITY + INTERCITY`). Before 25 Jan 2027
  `type` is always there and `type_code` is `null`;
- departure and arrival in each station's local time, as Renfe reports it;
- duration and number of changes;
- fares for one adult, cheapest first;
- `delay_minutes`: for today, the train's live delay, else `null`. A train with a change also has one on
  each leg, and the train's is the first of those the map has. Beside it, `live_train` is the number on the
  live map that delay came from (`null` with no delay): the train's own, or a published partner's when the
  number you'd buy isn't on the map (see [Delays](#delays)).
- `estimated_departure`: for a train already running and due at your station within the next 30 minutes,
  its scheduled departure plus `delay_minutes`, else `null`. It's an estimate, not Renfe's; how far to trust
  it is under [Delays](#delays). A train with a change also has one on each leg, and the train's is its first
  leg's.

It also returns `retries`, empty when nothing was retried: one entry per request that failed once and was
tried again, with `request` (which one), `tries`, `errors` (what failed on each try, such as
`server error (500)` or `connection dropped (RemoteProtocolError)`) and `recovered` (whether a later try
worked). A retry that worked isn't a problem with the list, so it never goes into `notice`. A request the
search stopped waiting for (the live map, see [Delays](#delays)) ends with an error saying so. When a
command fails after retries, the stderr error object carries the same `retries` list.

It also returns `shop`: the page on Renfe's website where that search continues, to pick the train and
pay.

Dates are YYYY-MM-DD, `today` or `tomorrow`, up to a year ahead. `search` returns at most 10 trains by default
(`--limit`, up to 200). Fares cost one extra request per 3
trains. For today, trains that have already left are hidden unless you pass `--after`; before 25 Jan 2027
the legacy shop can leave them out anyway (see [Delays](#delays)).

`--arrive-by HH:MM` keeps journeys scheduled to reach the destination by that time on the search date
(Madrid time). An arrival exactly at the deadline qualifies. An arrival after midnight the next day falls
outside the window. The filter applies before `--limit` and fare requests.

### Stations

Stations are matched by name, ignoring accents and case, or by code. A city name such as `Madrid` means
all of that city's stations (`MADRI`). Renfe's list of about 1,400 stations ships with the CLI, so
finding one sends no request. Only a name that matches none of them makes the CLI ask Renfe for its
current list, in case the station is newer than the shipped one; that list is kept for a day.
If neither list matches, `stations` returns up to five spelling suggestions from the shipped list.
For example, `renfe stations Chamartn` suggests Madrid-Chamartín. Use its code or corrected name to search.
`renfe stations --refresh` asks Renfe directly. The cache folder is `~/.cache/renfe-cli`, or
`$XDG_CACHE_HOME/renfe-cli`, or `$RENFE_CACHE_DIR` if set.

### Before 25 Jan 2027: Renfe's legacy shop

Renfe is moving to a new sales system, which prices trips
only from 25 Jan 2027 and before that knows only some trains (for Madrid–Barcelona on Friday 20 Nov 2026,
8 of the 24). Renfe sells the earlier trips on its legacy shop (venta.renfe.com), so for those dates
`search` asks the legacy shop, the way its results page does, and gets every train with its fares in the
same JSON (Madrid–Barcelona on Friday 27 Nov 2026, checked 2026-11-17: 24 trains, cheapest €31.15).
There, `type_code` and `intermediate_stops` are `null`, and a train that isn't on sale (full, "not on
sale at the moment", not running, or only wheelchair places left) has `available: false` and no fares.
`renfe search` only reads the list. Buy on Renfe's site with `renfe open`.

A one-off failure at any of the legacy shop's four requests (a 500 or 502 without `Retry-After`, whatever
its body, or a dropped connection) starts its search over from the first request, with a new guest session
and fresh cookies, up to 3 tries in all. A request there that gets no answer for 12 s starts the search over
once more, not twice (on 19 Nov one train list hung for the CLI's general 25 s while the shop answered other
searches in about a second; over two days the slowest search that got its list took 11.9 s for the whole
command).
A 503 or 504 there isn't retried: it may be an edge deny or a
waiting room. If it still fails (or a block or rate limit, a waiting room or challenge page, or an answer
the CLI can't read stops it), or it lists no trains, you get the new system's partial list instead, without
fares, and a `notice` saying what failed; the exit code stays 0. On 19 Nov 2026 the legacy shop sent no train
list at all for any search from 03:49 until 04:04 (Madrid–Barcelona) and about 04:35 (Zaragoza–Madrid); the
notice then says it "isn't listing trains right now". The one exception is today at the end of the day:
the legacy shop lists only trains leaving in more than about 5 minutes (see [Delays](#delays)), so when it
lists none and the new system has nothing leaving later either, nothing failed. With no train left, the only
notice is "No more trains today". With the last trains leaving within those minutes, the list shows them from
the new system, without fares, and the notice says the legacy shop no longer lists them.
On any other date (or judged by `--after`), an empty list from the legacy shop keeps the fallback notice:
before 25 Jan the new system lists only part of the timetable, so its being empty too proves little. The
CLI never tries to get past a waiting room or a captcha. `shop.url` and `renfe open` take you to the full
list on Renfe's website either way.

From 25 Jan 2027 the CLI's list matches the website's exactly, trains and cheapest fares (checked
against the web for Friday 29 Jan: 22 trains each).

### Requests, speed and retries

Every request names the CLI in its User-Agent (`renfe-cli/` and the version) instead of posing as a
browser, so Renfe can tell what it is. Searches are spaced: on one machine at most one starts per second,
across every `renfe` process, so a script or an agent running them in a loop can't burst; a single search's
own requests aren't slowed. The CLI sends what the website sends, no more: one timetable call, then fares 3 trains per call with at most 3 calls in
flight; before 25 Jan 2027, the four requests the legacy shop's results page makes. If Renfe blocks
or rate-limits it, it stops and says so (exit 5); before 25 Jan 2027 it falls back instead (see
[above](#before-25-jan-2027-renfes-legacy-shop)). What it retries: a one-off failure (a 500, 502, 503 or 504 with a JSON or empty body, or a
connection that drops or is refused), up to 3 tries per request, 1 s and 2 s apart, listed in the
output's `retries`; one of those 3 tries can be the resend when Renfe rejects a saved session with 401
(see below). An HTML page at any status is taken for bot protection and tried once; so are a block, a
rate limit and a timeout. The legacy shop has its own rule (above).
Live delays come from Renfe's public live train map. The network-wide
`delays` command also downloads Renfe's public timetable when it needs to look up scheduled times.

Speed (measured 2026-11-15): a search takes about 0.3–0.5 s, and a full day of 22 trains with fares
about 1.4 s. The anonymous session (a 30-minute token and the site's public API key) is kept in the
cache folder as `session.json`, so a timetable search usually sends a single request; fares add one per
3 trains. If Renfe answers 401 to a saved session, the CLI opens a new one and resends that request
once. A 403 is reported as a block, and the saved session is dropped so the next run starts afresh. A
500, 502, 503 or 504, or a dropped or refused connection, is tried up to 3 times, 1 s and then 2 s apart
(a 24-hour soak test on 18 Nov saw single requests fail like that and work again a minute later). The
401 resend counts as one of those 3 tries and goes at once; a retry after it waits 1 s, as a first retry
does, and a 403 to a saved session drops the file even on the third try. An HTML page is bot protection
whatever its status (a 503 page too), and isn't retried. Neither is a timeout: one already costs the whole
25 s timeout (the legacy shop's is 12 s, and tried once more; see above). With no network at all, a search takes a few seconds longer to fail than before (3 s of
pauses; 6 s before 25 Jan 2027, when the legacy shop is tried first), since a DNS or TLS failure counts as a
refused connection.
Before 25 Jan 2027 a search is the legacy shop's four requests instead, about 0.8–1.8 s with fares
(measured 2026-11-17), and nothing is saved: its cookies live in memory for that one search. A search for
today also sends one request to Renfe's live map, alongside the others (see [Delays](#delays)).

## Buying

`renfe open FROM TO -d DATE` opens that page in your browser when run from a terminal
(`--browser` forces it, `--no-browser` only prints the link). Add `--train` to check the train exists
that day and show it, so you know which one to pick. With `open`, you pick the train and pay on Renfe’s page.
- **Dates before 25 Jan 2027:** www.renfe.com opens with the route and the date filled in. Mark
  "Solo ida" (or pick a return date) to enable "Buscar billetes"; it then searches Renfe's legacy shop.
- **From 25 Jan 2027:** Renfe's new shop opens with the route filled in and one way selected. Pick the
  date there: the new shop doesn't take a date in a link.
- `--train` is checked against the full list (before 25 Jan 2027, the legacy shop's). If the legacy shop
  fails, the CLI sees only the new system's partial list, so a train it can't see still opens the page,
  with a `notice`.

## Delays

`renfe delays` lists every train reporting a positive delay on Renfe's live long- and medium-distance
map, most delayed first. Each entry in `trains` includes `delay_minutes`, the `origin` and
`destination`, and the times the train was due to **leave its origin** (`scheduled_departure`)
and **reach its destination** (`scheduled_arrival`). Both times include the local date and Madrid
UTC offset. The live prediction for the next stop is a separate field, `next_station.expected_arrival`.
`map_updated` dates the snapshot. The counts `delayed`, `on_time`, `early`, and `unknown` add up
to `mapped_trains`, including when the map is empty.

The command fetches one map for the whole report and looks up all the schedules in a single public GTFS
timetable, cached for 24 hours. Any retried requests appear in the top-level `retries` list.
It checks the service calendar, including dated exceptions, and
uses the reported delay and live time to identify the run around midnight. [Renfe's timetable](https://data.renfe.com/es/dataset/horarios-de-alta-velocidad-larga-distancia-y-media-distancia)

**Looking up the number on a ticket.** `renfe delays TRAIN` returns every delayed train whose live
number or verified section matches. Leading zeros are optional. `train` is the number on the live map.
Each entry in `number_segments` gives a train number, its origin and destination stations, and its
scheduled departure and arrival for that stretch. A single train still counts once. On November 18,
for example, `00632` ran from Zaragoza to Salamanca. `37632` covered Miranda de Ebro to Valladolid,
and `37432` covered Valladolid to Salamanca. All three searches returned the same live report.
The network totals still cover the whole map. `matched_trains` counts the rows returned by the filter.
`schedule_matches` counts exact, linked, ambiguous, unmatched and unavailable schedules among the
returned rows. Add `exact` and `linked` to get the number with scheduled departure and arrival times.

Renfe's published groupings supply candidate numbers. The CLI checks active services on the run date,
including when a train arrives after midnight. A shared stretch must have matching stations, direction
and times. To join successive legs it also checks the published journey endpoints and how the legs
meet. A number missing from the published list or lacking timetable evidence can be absent from
`number_segments`. An unresolved schedule has an empty list. [Renfe's published train-number groups](https://grupo.renfe.com/content/dam/grupo-renfe/sala-de-prensa/sala-de-prensa/general/pdf-y-otros/Trenes%2017%20noviembre%20SC.pdf)

- `schedule_match: exact` means the train number and both stations match one set of scheduled times.
- `linked` means matching the journey required another published number. `schedule_trains` lists
  the verified numbers and `number_segments` shows where each applies.
- `unmatched` means no schedule fit. `ambiguous` means several different times fit. In both cases
  the train stays in the list with both scheduled times set to `null`.
- If the timetable fails, `schedule_match` is `unavailable` and the top-level `notice` explains why.
  The live report still exits successfully. If the live map itself fails, the command reports an error.

`renfe status TRAIN` shows a train's live delay from Renfe's live train map, the public JSON behind its map
page (`tiempo-real.largorecorrido.renfe.com`, no key or session). TRAIN is a number from `renfe search`
(`03081`, leading zeros optional); for a train with a change, one leg's number. It returns
`delay_minutes` (negative: early), the train's origin and destination, its previous and next station (with
the expected arrival there), `overdue`, `reported_at`, `map_updated` and `retries` (as in `search`, for the
map or timetable request). `reported_at` is the time on the
train's entry; Renfe refreshes it every few minutes even when the entry's delay and stations are hours old,
so it doesn't show how fresh they are.

Every status includes `requested_train`, `scheduled_departure`, `scheduled_arrival`, `schedule_match` and
`number_segments`. A direct map hit takes priority and uses the same timetable matching as `delays`. If
the timetable fails, the live result still prints with `schedule_match: "unavailable"` and the reason in
`notice`. Unknown times are null and unknown sections are an empty array.

When a ticket number is absent from the map but a published partner number is present, `renfe status TRAIN`
checks the timetable for a matching section. A unique match includes the ticket number in `requested_train`
and the live number in `train`, including for early and on-time trains. When several live trains match,
`matching_trains` lists them and the delay is unknown.

- **While it runs, and mainly long- and medium-distance trains.** The map lists trains around the time they
  run (some while still at their first station), and at night it is empty. A train that isn't on it has
  `on_map: false` and no delay: it may not have left yet, may have arrived or been cancelled, or isn't on
  the map at all. Its delay is unknown, not zero.
- **The delay is the last one Renfe recorded.** A train held somewhere keeps its delay and next station
  while it gets later. `overdue` is `true` once the expected arrival at its next station is more than 5
  minutes behind the map's update, or more than 10 when Renfe gives the same station as previous and next
  (the train is probably stopped there, and the notice says since when); never while the train runs early,
  waiting for its time. After midnight the map can date that arrival a day ahead; `status` brings it back.
- **Whichever run is on the map.** The map has no run date, so after midnight `status` and `watch` can show
  yesterday's run of a train that runs again today, still going (on 18 Nov at 02:14, 18135 of the 17th, 170
  minutes late). `delays` looks up the service day for that run. `search` checks whether the entry
  fits the requested journey (below).
- **Unreachable at times.** On 17 Nov 2026 it refused connections from Spain for over two hours while it
  answered from other countries. On 18 Nov, checked every 15 minutes from 12:24 to 22:04, it answered every
  time but once, when it dropped connections for under a minute (4 requests failed over about 10 seconds,
  and one a minute later worked). A refused or dropped connection, or a 500, 502, 503 or 504 that isn't an
  HTML page, is tried up to 3 times (listed in `retries`); an HTML page is taken for bot protection and
  tried once. `status` fails with exit 5 when it still fails, or when the
  map doesn't connect or send data for 10 s at a time (a timeout isn't retried).

**In `search`, for today only.** The search asks the map alongside its own requests and waits for it at most
2.5 s from its start, so a map that hangs costs at most that. Only when a train on the list isn't on the map
under its number but a published partner number is, it then waits at most 3 s more for Renfe's timetable
(below). It retries the map only when the pause and
another try can end within those 2.5 s; a retry still running when the search stops waiting is listed in
`retries` as not recovered. Each train on the map gets its
`delay_minutes`, the last one recorded, as in `status`; one that isn't gets `null`, and so do they all on
other days. `delays_included` says whether the map answered: when it doesn't, `notice` says the delays are
unknown, and the search and its exit code are unaffected. A train gets no delay from an entry left over on
the map: one whose expected arrival at its next station is more than 45 minutes past (on 17 Nov at 17:48,
4 of 95, one of them still at its first station 7.5 hours later with a delay of 0). Nor from yesterday's
run: the map's time for the train must be less than 12 hours before the leg searched leaves or after it
arrives. `renfe open` never asks the map.

A train sold under one number can run on the map under another, as `renfe status` explains for ticket numbers.
Then the search checks Renfe's timetable the way `status` does, loading it at most once (it is cached for a day)
and in one try. The leg gets the live train's delay, with that number in `live_train`, only when a verified
section under the leg's number spans the leg's scheduled times, and the same checks as above pass. So a
delay the search attaches is the live train and delay `renfe status NUMBER` gives (`status` also shows an
entry left over on the map, which the search drops). If the timetable fails or takes longer than 3 s, those
legs keep `null` and the notice says "Delays of trains sold under another number are unknown", with the
reason. With no cached timetable on a slow connection, a search that stops waiting leaves the cache empty, so
the next one waits again; `renfe delays` or `renfe status` fills it. On 18–20 Nov 2026, on the two routes the
soak test searched (Madrid–Barcelona, Zaragoza–Madrid), a sold number was off the map while a published
partner was on it once in 347 searches; routes with more partner numbers see it more often.

**Estimated departure.** A train with a delay also gets `estimated_departure`: its scheduled departure from
your station plus `delay_minutes`, with an early train counted as on time, since it waits for its departure.
It's given only when that time is within 30 minutes after the map's time, and never in the past: by then the
train has left, or its delay has grown. A train that is `overdue` at its next station (as `status` above
explains) gets none, since its delay may be growing. Nor does a train still at its first station: it isn't
running yet, and how late a train leaves its first station hasn't been measured. So the estimate is for
stations along a train's route, not where it starts. Each leg of a change has its own. The train's is its first
leg's, so a trip whose first train isn't running yet has `null` even when a later leg has one.

How far to trust it, from Renfe's live map recorded every 2 minutes from Friday 20 Nov 2026 15:40 to Saturday
21 Nov 23:30 (68,026 pairs of sightings of a running train, 30 minutes apart):
- Over 30 minutes the delay changed by a median of 2 minutes. One time in ten it changed by 8 minutes or
  more, and 29% of the time by 5 or more.
- Delays grew more often than they shrank (in 52% of the pairs, against 30%), so a train leaves after its
  estimate more often than before it.
- Over 60 minutes the median change was 4 minutes, and one time in ten 14 or more. That's why the estimate
  stops at 30 minutes.
- The sample is a Friday afternoon and a Saturday, with no weekday morning yet.

Today's list hides trains past their scheduled departure, even a late one that hasn't reached your station
yet:
- **Before 25 Jan 2027** the list comes from the legacy shop, which stops listing a train about 5 minutes
  before it leaves the station searched from, even with `--after`. Checked every 15 minutes on two routes on
  18–20 Nov 2026: of 74 trains due within 15 minutes, the 6 due in under 5 minutes were missing and the 68
  due in 5.8 minutes or more were there. On 18 Nov 2026 at about 06:30, `search Barcelona Madrid
  --after 05:00` started at 06:35, without 03052, which had left Barcelona-Sants (its first station) at
  05:40 and was on the map 7 minutes late. From a station further along its route, without `--after`, it
  was there: `search Zaragoza Madrid` listed it leaving at 07:09, with `delay_minutes: 7`.
- **From 25 Jan 2027** `--after HH:MM` shows the trains Renfe's new system lists for that time; whether it
  lists trains that have already left is unverified.

**`renfe watch TRAIN`** checks the map every 2 minutes and prints one line of JSON, shaped like `status`'s
output, on the first check and whenever the train's delay, its next station, the expected arrival there,
`overdue` or `on_map` changes, by any amount. Renfe moves the expected arrival between delay updates, so it
is the finer signal: on 18 Nov from 06:29 to 06:59 it moved 264 times with the delay and next station
unchanged, mostly by a minute either way, and a train on the map throughout gave about 10 lines in those
30 minutes (at most 15). It keeps checking a train that isn't on the map yet, so you can start it before
departure. A ticket number works too. When a published partner appears, the watch verifies its section
against the timetable. Several matches produce a line listing `matching_trains`. Once one partner matches,
the watch follows it even if another partner appears later. If the exact number you requested appears, the
watch prints its status and follows that number for the rest of the watch.

The watch loads the timetable at most once. If that load fails during ticket lookup, one line gives the
reason. The watch can still follow the exact number on the map, but it cannot match the ticket to a partner
for the rest of that watch. Status lines carry the same schedule fields as `renfe status`. Event lines
include `requested_train` only while the watch is following a different live number.

Each status line has that check's `retries`, which don't count as a change. Two other lines carry an
`event` instead: `map_unreachable` (with the `error` and `retries`) when the map stops answering, after its
retries, once per outage, after which the next answer prints a line whether or not anything changed; and
`ended` when the watch stops on its own. Pipe it into whatever pings you; the CLI sends no notifications
itself:

```sh
renfe watch 03081 | while read -r line; do
  case "$line" in
    *'"event": "map_unreachable"'*) msg="Renfe's live map isn't answering: delay unknown for now" ;;
    *'"event": "ended"'*) msg="renfe watch has stopped" ;;
    *) msg="$line" ;;
  esac
  curl -s -d "$msg" https://ntfy.sh/<your-topic> >/dev/null
done
```

- It ends on its own after 12 hours (the map doesn't show where a trip ends; for a longer one, run it
  again), or on Ctrl-C, both with exit 0. If what reads its output goes away, it ends with exit 1.
- A map that doesn't answer doesn't end it. A block or rate limit (403/429, a bot page) stops it with exit 5,
  and 3 answers in a row that aren't the map (it moved or changed) with exit 7.
- A train held somewhere keeps its delay, its next station and the expected arrival there, which slips into
  the past. `watch` prints one line when it becomes overdue there, and another when it moves on or Renfe
  updates it, not one per check.

## Output and errors

stdout is one JSON document, except for `watch`, which prints one per line (JSON Lines, including its outage
lines). Errors go to stderr as `{"error": {...}}`, with these exit codes:

| Code | Meaning |
|---|---|
| `0` | ok; check `notice` |
| `1` | internal error (only the exception type is shown) |
| `2` | usage: bad arguments, unknown station, bad date, or a request Renfe rejected |
| `3` | auth: Renfe refused the anonymous session |
| `5` | blocked or upstream: 403/429, a bot page, timeout or 5xx, including Renfe's live map for `delays` and `status` (for `watch`, only a block or rate limit). If this happens while fetching fares, stdout still has the timetable (plus `fares_error`). On the legacy shop it falls back instead (exit 0, `notice`) |
| `7` | API changed |

## Built on renfe-cli

- [renfe-tui](https://github.com/Sn4pe/renfe-tui): a terminal UI to search trains, see fares, buy, follow a
  train and check delays, with the mouse or the keyboard. It runs `renfe` as a subprocess and reads its JSON.

## Contributing

Issues and pull requests are welcome. To work on the code:

```sh
git clone https://github.com/luisgonzaleznf/renfe-cli
cd renfe-cli
uv sync
python3 .githooks/pii_check.py enable   # once per clone: the personal-data guard (below)
uv run renfe --help                     # your working copy
uv run pytest                           # offline: HTTP is mocked
RENFE_LIVE=1 uv run pytest -m live      # about 10 real requests, plus one to the live map
uv run python scripts/update_stations.py  # refresh the shipped station list; prints what changed
```

- Keep tests offline: HTTP is mocked. A test that calls Renfe gets the `live` marker, so it runs only with
  `RENFE_LIVE=1`.
- Scripts and agents read the output, so add JSON fields rather than renaming or removing them.
- Never commit real passenger or booking data, captures (HAR files), tickets or tokens. Use obviously fake
  values in tests.

CI runs the offline suite on Linux and macOS, with Python 3.12 and 3.13, for every pull request.

A scheduled workflow (`.github/workflows/stations-reminder.yml`) opens an issue to refresh the station
list on 1 February and 1 August. GitHub turns schedules off in a public repo after 60 days without
activity; re-enable it from the Actions tab, or run it from there by hand.

Personal-data guard: `.githooks/` checks staged changes and commit messages for passengers' and
buyers' data (names, DNI/NIE, emails, phones, booking locators, card numbers) and for capture files.
Enable it once per clone with `python3 .githooks/pii_check.py enable`.

## License

[MIT](LICENSE). The station list and train-number groups in `src/renfe/data/` come from Renfe's own
data and aren't covered by the MIT license. The station list is the one Renfe's web app loads (its station
master data), refreshed with `scripts/update_stations.py`. Renfe is a trademark of its owner; this project
uses the name only to say which service it works with.

If Renfe objects to anything this project does, open an issue, and that part will change or come out.
