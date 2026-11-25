"""Live delays from Renfe's live train map: `renfe status`. Every map here is synthetic (fakes.live_map)."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from renfe import cli, config, live, train_numbers
from renfe import delays as delay_report
from renfe.delays import TIMETABLE_URL

from fakes import FakeRenfe, live_map, map_train, timetable

runner = CliRunner()


def use(monkeypatch, handler: FakeRenfe) -> FakeRenfe:
    monkeypatch.setattr(cli, "make_client", lambda: cli.RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    return handler


@pytest.fixture
def fake(monkeypatch):
    return use(monkeypatch, FakeRenfe())


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def raising(exc: Exception):
    def answer(request):
        raise exc
    return answer


def test_status_of_a_train_on_the_map(fake):
    result = invoke("status", "03100")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "train": "03100", "on_map": True, "delay_minutes": 12,
        "origin": {"code": "60000", "name": "Madrid-Puerta de Atocha-Almudena Grandes"},
        "destination": {"code": "71801", "name": "Barcelona-Sants"},
        "previous_station": {"code": "60000", "name": "Madrid-Puerta de Atocha-Almudena Grandes"},
        "next_station": {"code": "04040", "name": "Zaragoza-Delicias", "expected_arrival": "2027-01-29T10:32:00+01:00"},
        "overdue": False, "reported_at": "2027-01-29T09:29:40+01:00", "map_updated": "2027-01-29T09:30:00+01:00",
        "notice": None, "retries": [], "requested_train": "03100", "scheduled_departure": None,
        "scheduled_arrival": None, "schedule_match": "unmatched", "number_segments": [],
    }
    assert [str(r.url) for r in fake.requests] == [config.LIVE_MAP_URL, TIMETABLE_URL]


def test_status_takes_the_number_without_its_leading_zero(fake):
    assert json.loads(invoke("status", "3100").stdout)["delay_minutes"] == 12


def test_a_train_not_on_the_map_has_no_delay_and_says_why(fake):
    result = invoke("status", "03000")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["train"] == "03000" and out["on_map"] is False and out["delay_minutes"] is None
    assert out["next_station"] is None and out["overdue"] is None and out["map_updated"] == "2027-01-29T09:30:00+01:00"
    assert "may not have left yet" in out["notice"] and "unknown, not zero" in out["notice"]
    assert "empty at night" not in out["notice"]


def test_an_empty_map_says_so(monkeypatch):
    use(monkeypatch, FakeRenfe(live=live_map(updated="2027-01-29T02:00:00")))
    out = json.loads(invoke("status", "03100").stdout)
    assert out["on_map"] is False and "lists no trains right now" in out["notice"]


def test_a_train_standing_at_a_station_says_it_is_probably_stopped(fake):
    out = json.loads(invoke("status", "00500").stdout)
    assert out["previous_station"] == {"code": "04040", "name": "Zaragoza-Delicias"} and out["overdue"] is False
    assert out["notice"] == "The train is probably stopped at Zaragoza-Delicias, since about 09:33."


@pytest.mark.parametrize("previous,delay,expected,overdue,says", [
    ("60000", 12, "2027-01-29T09:26:00", False, None),                # between stations, 4 min past: not yet...
    ("60000", 12, "2027-01-29T09:24:00", True, "(09:24) has already passed"),  # ...6 min past: overdue
    ("04040", 12, "2027-01-29T09:21:00", False, "stopped at Zaragoza-Delicias, since about 09:21."),  # a stop
    ("04040", 12, "2027-01-29T09:19:00", True, "since about 09:19, longer than a stop"),  # 11 min: held
    ("04040", -8, "2027-01-29T08:50:00", False, "since about 08:50."),  # early: waiting for its time, never overdue
    ("60000", -3, "2027-01-29T09:00:00", False, None),
])
def test_overdue_leaves_room_for_a_stop_and_never_flags_an_early_train(monkeypatch, previous, delay, expected,
                                                                       overdue, says):
    """The map's time is 09:30. As at 17:48 on 17 Nov: 18014 (11 min at Burgos, really late) is overdue; 17012
    (8 min early, waiting at Plasencia) isn't."""
    use(monkeypatch, FakeRenfe(live=live_map(map_train("03100", delay, previous, "04040", expected))))
    out = json.loads(invoke("status", "03100").stdout)
    assert out["overdue"] is overdue
    assert out["notice"] is None if says is None else says in out["notice"]


def test_a_held_train_says_its_delay_may_be_growing(monkeypatch):
    """A next-station arrival already past: the delay is the last one recorded (seen at 17:48, 9 h past)."""
    held = map_train("03200", 7, "60000", "04040", "2027-01-29T08:51:00")
    use(monkeypatch, FakeRenfe(live=live_map(held)))
    out = json.loads(invoke("status", "03200").stdout)
    assert out["delay_minutes"] == 7 and out["overdue"] is True and "(08:51) has already passed" in out["notice"]
    assert "probably stopped" not in out["notice"]


def test_early_trains_and_unreadable_fields(monkeypatch):
    odd = map_train("03300", -3, "60000", "", "not a time")
    odd["time"] = "soon"
    broken = map_train("03400", 5, "60000", "04040", "2027-01-29T10:00:00")
    broken["ultRetraso"] = ""
    numeric = map_train("03500", 6, "60000", "04040", "2027-01-29T10:00:00")
    numeric["ultRetraso"] = 6
    use(monkeypatch, FakeRenfe(live=live_map(odd, broken, numeric, "junk", {"codComercial": 3600},
                                             {"codComercial": "3 1"})))
    out = json.loads(invoke("status", "03300").stdout)
    assert out["delay_minutes"] == -3 and out["next_station"] is None and out["reported_at"] is None
    assert json.loads(invoke("status", "03400").stdout)["delay_minutes"] is None  # listed, delay unreadable
    assert json.loads(invoke("status", "03500").stdout)["delay_minutes"] == 6  # a JSON number reads too


@pytest.mark.parametrize("answer,exit_code,kind", [
    (raising(httpx.ConnectError("refused")), 5, "upstream_unavailable"),
    (raising(httpx.ReadTimeout("slow")), 5, "upstream_unavailable"),
    (lambda r: httpx.Response(503, json={}), 5, "upstream_unavailable"),
    (lambda r: httpx.Response(429, json={}), 5, "blocked"),
    (lambda r: httpx.Response(200, headers={"content-type": "text/html"}, text="<html>"), 5, "blocked"),
    (lambda r: httpx.Response(404, json={}), 7, "api_changed"),
    (lambda r: httpx.Response(200, text="not json"), 7, "api_changed"),
    (lambda r: httpx.Response(200, json={"trains": []}), 7, "api_changed"),
])
def test_a_map_that_doesnt_answer_is_an_error_not_a_train_without_delay(monkeypatch, answer, exit_code, kind):
    use(monkeypatch, FakeRenfe(**{"flotaLD": answer}))
    result = invoke("status", "03100")
    assert result.exit_code == exit_code and result.stdout == ""
    err = json.loads(result.stderr)["error"]
    assert err["type"] == kind and err["message"].startswith("live map:")


def test_a_map_that_changed_points_at_live_py(monkeypatch):
    use(monkeypatch, FakeRenfe(**{"flotaLD": lambda r: httpx.Response(200, text="not json")}))
    err = json.loads(invoke("status", "03100").stderr)["error"]
    assert err["type"] == "api_changed" and "renfe/live.py" in err["hint"] and "search.py" not in err["hint"]


@pytest.mark.parametrize("train", ["00600+00500", "abc", "", "1234567"])
def test_status_takes_one_train_number(fake, train):
    result = invoke("status", train)
    assert result.exit_code == 2 and "TRAIN must be one train number" in json.loads(result.stderr)["error"]["message"]
    assert fake.requests == []


def test_parse_keeps_the_first_entry_of_a_number_listed_twice():
    board = live.parse(live_map(map_train("03100", 1, "60000", "04040", "2027-01-29T10:00:00"),
                                map_train("3100", 9, "60000", "04040", "2027-01-29T10:00:00")))
    assert list(board.trains) == ["3100"] and board.trains["3100"]["delay"] == 1


# --- `renfe search` for today ------------------------------------------------------------------------------------


def frozen_at(monkeypatch, moment: str) -> None:
    """Pin the CLI's "now" to a Madrid wall-clock time."""
    from datetime import datetime as real_datetime

    pinned = real_datetime.fromisoformat(moment).replace(tzinfo=live.MADRID)

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned

    monkeypatch.setattr(cli, "datetime", Frozen)


def delays(out: dict) -> dict:
    """Service -> the train's `delay_minutes`, which every train has."""
    return {t["service"]: t["delay_minutes"] for t in out["trains"]}


def leg_delays(out: dict, service: str) -> list:
    return [leg["delay_minutes"] for t in out["trains"] if t["service"] == service for leg in t["legs"]]


def no_delays(out: dict) -> bool:
    return all(part["delay_minutes"] is None and part["estimated_departure"] is None
               for t in out["trains"] for part in [t] + t.get("legs", []))


def estimates(out: dict) -> dict:
    """Service -> the train's `estimated_departure` and its legs'."""
    return {t["service"]: (t["estimated_departure"], [leg["estimated_departure"] for leg in t.get("legs", [])])
            for t in out["trains"]}


def test_today_each_train_gets_its_delay_and_each_leg_of_a_change_its_own(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    result = invoke("search", "Madrid", "Barcelona", "--after", "06:00")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert delays(out) == {"03000": None, "00600+00500": 5, "03100": 12, "03200": None}
    assert leg_delays(out, "00600+00500") == [None, 5]  # the first leg has arrived; the second is running
    assert "legs" not in out["trains"][2]  # a direct train: its delay is on the train only
    assert out["delays_included"] is True and out["notice"] is None and out["fares_included"] is True
    assert len(fake.live_requests()) == 1


def test_before_the_cutoff_the_legacy_shops_trains_get_delays_too(monkeypatch):
    frozen_at(monkeypatch, "2026-11-15T10:00")
    second_leg = map_train("00500", 8, "04040", "71801", "2026-11-15T12:08:00", reported="2026-11-15T10:00:00")
    handler = use(monkeypatch, FakeRenfe(live=live_map(second_leg, updated="2026-11-15T10:00:00")))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00", "--no-fares").stdout)
    assert delays(out)["00600+00500"] == 8 and leg_delays(out, "00600+00500") == [None, 8]
    assert handler.legacy_requests() and not handler.new_system_requests()


@pytest.mark.parametrize("answer,why", [
    (lambda r: httpx.Response(503, json={}), "server error (503)"),
    (raising(httpx.ConnectError("refused")), "request failed (ConnectError)"),
    (lambda r: httpx.Response(200, json={"trains": []}), "no list of trains"),
    (raising(RuntimeError("a bug")), "unexpected error (RuntimeError)"),
])
def test_a_map_that_fails_leaves_the_search_and_its_exit_code_alone(monkeypatch, answer, why):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    use(monkeypatch, FakeRenfe(**{"flotaLD": answer}))
    result = invoke("search", "Madrid", "Barcelona", "--after", "06:00")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["count"] == 4 and out["trains"][0]["fares"] and out["delays_included"] is False
    assert no_delays(out)
    assert out["notice"].startswith("Delays are unknown (live map:") and why in out["notice"]


def test_a_map_that_hangs_costs_the_search_its_budget_only(monkeypatch):
    import threading
    import time

    frozen_at(monkeypatch, "2027-01-29T09:30")
    monkeypatch.setattr(config, "LIVE_SEARCH_BUDGET", 0.2)
    release = threading.Event()
    use(monkeypatch, FakeRenfe(**{"flotaLD": lambda r: release.wait(10) and httpx.Response(200, json=live_map())}))
    started = time.monotonic()
    try:
        result = invoke("search", "Madrid", "Barcelona", "--after", "06:00")
    finally:
        release.set()
    assert time.monotonic() - started < 2
    out = json.loads(result.stdout)
    assert result.exit_code == 0 and out["count"] == 4 and "no answer within 0.2 s" in out["notice"]


def test_a_bug_attaching_delays_leaves_the_search_without_them(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")

    def broken(part, board, number=None):
        if board is not None:
            raise KeyError("departure")
        return None

    monkeypatch.setattr(live, "leg_delay", broken)
    result = invoke("search", "Madrid", "Barcelona", "--after", "06:00")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["count"] == 4 and out["delays_included"] is False and no_delays(out)
    assert "Delays are unknown (live map: unexpected error (KeyError))" in out["notice"]



# --- estimated departure in today's search ---------------------------------------------------------------------


@pytest.mark.parametrize("previous,origin,estimate", [
    ("70600", "60000", "2027-01-29T09:35:00+01:00"),  # running, from Madrid, toward Zaragoza
    ("04040", "04040", None),                          # standing at Zaragoza, its first station: not running yet
])
def test_a_train_running_toward_your_station_gets_an_estimate_and_a_change_its_first_legs(monkeypatch, previous,
                                                                                         origin, estimate):
    """At 09:30: 00500, due out of Zaragoza at 09:30 and 5 minutes late, gets 09:35 if it is on its way there. The
    journey starts with 00600, which isn't on the map, so the train gets none. 03100 left Madrid at about 09:12: past,
    so none, though it keeps its delay."""
    frozen_at(monkeypatch, "2027-01-29T09:30")
    second = map_train("00500", 5, previous, "04040", "2027-01-29T09:33:00", origin=origin)
    use(monkeypatch, FakeRenfe(live=live_map(map_train("03100", 12, "60000", "04040", "2027-01-29T10:32:00"), second)))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00").stdout)
    assert estimates(out) == {"03000": (None, []), "00600+00500": (None, [None, estimate]), "03100": (None, []),
                              "03200": (None, [])}
    assert delays(out)["03100"] == 12 and delays(out)["00600+00500"] == 5


@pytest.mark.parametrize("delay,estimate", [
    (10, "2026-11-15T17:10:00+01:00"),  # 30 minutes after the map's time: given
    (11, None),                          # 31: too far ahead to call an estimate
    (-4, "2026-11-15T17:00:00+01:00"),  # early: it waits for its departure
])
def test_an_estimate_is_given_up_to_30_minutes_ahead_and_never_before_the_timetable(monkeypatch, delay, estimate):
    """Before the cut-off, from the legacy shop: 03300 leaves Atocha at 17:00, and the map, at 16:40, has it on its way
    there from Chamartin."""
    frozen_at(monkeypatch, "2026-11-15T16:40")
    running = map_train("03300", delay, "17000", "60000", "2026-11-15T16:50:00", reported="2026-11-15T16:40:00",
                        origin="17000")
    handler = use(monkeypatch, FakeRenfe(live=live_map(running, updated="2026-11-15T16:40:00")))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--no-fares").stdout)
    assert delays(out)["03300"] == delay and estimates(out)["03300"] == (estimate, [])
    assert handler.legacy_requests() and not handler.new_system_requests()


@pytest.mark.parametrize("expected,estimate", [
    ("2027-01-29T09:26:00", "2027-01-29T10:00:00+01:00"),  # 4 minutes behind its expected arrival at the next station
    ("2027-01-29T09:24:00", None),                         # 6: overdue, its delay may be growing
])
def test_a_train_overdue_at_its_next_station_gets_no_estimate(expected, estimate):
    board = live.parse(live_map(map_train("05100", 20, "60000", "04040", expected, reported="2027-01-29T09:30:00")))
    leg = {"service": "05100", "departure": "2027-01-29T09:40:00+01:00", "arrival": "2027-01-29T12:00:00+01:00"}
    live.annotate([leg], board)
    leg["live_train"] = "05100"
    assert leg["delay_minutes"] == 20 and live.estimated_departure(leg, board) == estimate


def test_a_bug_estimating_keeps_the_delays_and_leaves_every_train_without_an_estimate(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")

    def broken(part, board):
        if board is not None:
            raise KeyError("departure")
        return None

    monkeypatch.setattr(live, "estimated_departure", broken)
    result = invoke("search", "Madrid", "Barcelona", "--after", "06:00")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert delays(out)["03100"] == 12 and out["delays_included"] is True and out["notice"] is None
    assert all(e == (None, [None] * len(legs)) for e, legs in
               ((estimates(out)[t["service"]], t.get("legs", [])) for t in out["trains"]))


# --- ticket numbers in today's search ----------------------------------------------------------------------------


def ticket_search(monkeypatch, departure="09:02:00Z", arrival="11:00:00Z", **overrides):
    """Today at 10:30: a search Valladolid - Salamanca lists 37432 (fakes.ticket_routes), and the map shows the train
    as 00632, 12 minutes late."""
    from fakes import path, ticket_routes
    from renfe import stations

    both = [{"_u_i_c_station_code": code, "name": name, "province": name, "agglomeration_type": "commercial",
             "main_station": True} for code, name in (("10600", "Valladolid-Campo Grande"), ("30100", "Salamanca"))]
    every = stations.bundled() + stations.parse(both)
    monkeypatch.setattr(stations, "bundled", lambda: every)
    frozen_at(monkeypatch, "2027-01-29T10:30")
    board = live_map(map_train("00632", 12, "10600", "30100", "2027-01-29T10:52:00", origin="04040",
                               destination="30100"), updated="2027-01-29T10:30:00")
    listed = {"total_count": 1, "travel_options": [{"travel_duration": 118, "travel_path": [path(
        "37432", f"2027-01-29T{departure}", f"2027-01-29T{arrival}", origin="10600", destination="30100")]}]}
    handlers = {config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=listed),
                TIMETABLE_URL: lambda r: httpx.Response(200, content=timetable(*ticket_routes())), **overrides}
    return use(monkeypatch, FakeRenfe(live=board, **handlers))


def timetable_requests(fake: FakeRenfe) -> int:
    return sum(str(r.url) == TIMETABLE_URL for r in fake.requests)


def test_a_ticket_number_gets_its_live_trains_delay_as_status_finds_it(monkeypatch):
    fake = ticket_search(monkeypatch)
    out = json.loads(invoke("search", "10600", "30100", "--after", "06:00", "--no-fares").stdout)
    (train,) = out["trains"]
    assert (train["service"], train["delay_minutes"], train["live_train"]) == ("37432", 12, "00632")
    assert out["notice"] is None and out["delays_included"] is True and timetable_requests(fake) == 1
    status = json.loads(invoke("status", "37432").stdout)  # the same live train, the same delay
    assert (status["train"], status["delay_minutes"], status["requested_train"]) == ("00632", 12, "37432")


def test_a_ticket_numbers_estimate_is_its_own_departure_plus_the_live_trains_delay(monkeypatch):
    ticket_search(monkeypatch, departure="09:40:00Z")  # 10:40 from Valladolid, 10 minutes after the map's time
    (train,) = json.loads(invoke("search", "10600", "30100", "--after", "06:00", "--no-fares").stdout)["trains"]
    assert (train["live_train"], train["delay_minutes"], train["estimated_departure"]) == (
        "00632", 12, "2027-01-29T10:52:00+01:00")


@pytest.mark.parametrize("departure,arrival", [
    ("09:01:00Z", "11:00:00Z"),  # leaves a minute before the verified section under 37432 starts (10:02 Madrid)
    ("09:02:00Z", "11:01:00Z"),  # arrives a minute after it ends: not that run, or not that stretch
])
def test_a_ticket_number_needs_a_verified_section_over_the_legs_times(monkeypatch, departure, arrival):
    fake = ticket_search(monkeypatch, departure, arrival)
    out = json.loads(invoke("search", "10600", "30100", "--after", "06:00", "--no-fares").stdout)
    assert out["trains"][0]["delay_minutes"] is None and out["trains"][0]["live_train"] is None
    assert out["notice"] is None and timetable_requests(fake) == 1


@pytest.mark.parametrize("response,why", [
    (httpx.Response(403), "Renfe timetable: blocked (HTTP 403)"),
    (httpx.Response(503), "Renfe timetable: HTTP 503"),  # one try: a search doesn't wait out retries
])
def test_a_timetable_failure_leaves_ticket_numbers_without_delay_and_says_so(monkeypatch, response, why):
    fake = ticket_search(monkeypatch, **{TIMETABLE_URL: lambda r: response})
    result = invoke("search", "10600", "30100", "--after", "06:00", "--no-fares")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["trains"][0]["delay_minutes"] is None and out["delays_included"] is True
    assert out["notice"] == f"Delays of trains sold under another number are unknown ({why})."
    assert timetable_requests(fake) == 1 and out["retries"] == []


def test_a_search_waits_for_the_timetable_only_within_its_budget(monkeypatch):
    import threading

    release = threading.Event()

    def hangs(request):
        release.wait(5)
        return httpx.Response(200, content=b"")

    monkeypatch.setattr(config, "SEARCH_TIMETABLE_BUDGET", 0.2)
    ticket_search(monkeypatch, **{TIMETABLE_URL: hangs})
    try:
        out = json.loads(invoke("search", "10600", "30100", "--after", "06:00", "--no-fares").stdout)
    finally:
        release.set()
    assert out["trains"][0]["delay_minutes"] is None
    assert out["notice"] == ("Delays of trains sold under another number are unknown "
                             "(Renfe timetable: no answer within 0.2 s).")


def test_live_train_says_where_each_delay_came_from_and_needs_no_timetable(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00").stdout)
    sources = {t["service"]: (t["live_train"], [leg["live_train"] for leg in t.get("legs", [])]) for t in out["trains"]}
    assert sources == {"03000": (None, []), "00600+00500": ("00500", [None, "00500"]), "03100": ("03100", []),
                       "03200": (None, [])}
    assert timetable_requests(fake) == 0  # no train sold under a partner of a number on the map
    other_day = json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2027-01-30").stdout)
    assert all(part["live_train"] is None for t in other_day["trains"] for part in [t] + t.get("legs", []))


def test_a_search_with_an_unknown_station_never_asks_the_map(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    assert invoke("search", "Ruritania Central", "Barcelona").exit_code == 2
    assert fake.live_requests() == []


def test_other_days_never_ask_the_map(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-28T09:30")
    out = json.loads(invoke("search", "Madrid", "Barcelona", "-d", "tomorrow").stdout)
    assert out["delays_included"] is False and out["notice"] is None and no_delays(out)
    assert fake.live_requests() == [] and fake.new_system_requests()


def test_open_never_asks_the_map(fake, monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    result = invoke("open", "Madrid", "Barcelona", "-d", "today", "--train", "03200", "--no-browser")
    assert result.exit_code == 0, result.output
    assert fake.live_requests() == [] and "delay_minutes" not in json.loads(result.stdout)["train"]


def test_after_midnight_yesterdays_run_lends_no_delay_to_tonights(monkeypatch):
    """As seen at 02:14 on 18 Nov: yesterday's run still going after midnight, its arrival dated a day ahead. Today's
    03200 leaves at 13:00, too far from a train seen at 00:30; 03100, seen 8.5 h before its 09:00 leg, keeps its."""
    frozen_at(monkeypatch, "2027-01-29T00:30")
    late = map_train("03200", 40, "60000", "04040", "2027-01-30T00:50:00", reported="2027-01-29T00:30:00")
    early = map_train("03100", 3, "60000", "04040", "2027-01-29T01:03:00", reported="2027-01-29T00:30:00")
    use(monkeypatch, FakeRenfe(live=live_map(late, early, updated="2027-01-29T00:30:00")))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--no-fares").stdout)
    assert delays(out)["03200"] is None and delays(out)["03100"] == 3 and out["delays_included"] is True


def test_an_entry_hours_out_of_date_gives_search_no_delay_and_a_live_leg_wins(monkeypatch):
    """As 08109 at 17:48 on 17 Nov: listed at its origin, delay 0, 7.5 h after its expected arrival. 0 would read
    as on time. 46 min past is stale; 44 min past is a train held a while, and keeps its number."""
    frozen_at(monkeypatch, "2027-01-29T09:30")
    stale = map_train("00600", 0, "60000", "60000", "2027-01-29T02:00:00", reported="2027-01-29T09:29:40")
    live_leg = map_train("00500", 35, "04040", "04040", "2027-01-29T09:28:00", origin="04040")
    held = map_train("03100", 20, "60000", "04040", "2027-01-29T08:46:00")
    gone = map_train("03200", 20, "60000", "04040", "2027-01-29T08:44:00")
    use(monkeypatch, FakeRenfe(live=live_map(stale, live_leg, held, gone)))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00", "--no-fares").stdout)
    assert leg_delays(out, "00600+00500") == [None, 35] and delays(out)["00600+00500"] == 35
    assert delays(out)["03100"] == 20 and delays(out)["03200"] is None
    assert json.loads(invoke("status", "00600").stdout)["delay_minutes"] == 0  # status still shows it, flagged
    assert json.loads(invoke("status", "00600").stdout)["overdue"] is True


@pytest.mark.parametrize("seen,kept", [
    ("2027-01-28T22:30:00", True),   # 12 h before the leg leaves: still that run
    ("2027-01-28T22:29:00", False),  # just over: yesterday's
    ("2027-01-30T00:00:00", True),   # 12 h after it arrives
    ("2027-01-30T00:01:00", False),
])
def test_a_train_seen_within_12_hours_of_the_leg_is_on_its_run(seen, kept):
    board = live.parse(live_map(map_train("05100", 10, "60000", "04040", seen, reported=seen), updated=seen))
    leg = {"service": "05100", "departure": "2027-01-29T10:30:00+01:00", "arrival": "2027-01-29T12:00:00+01:00"}
    live.annotate([leg], board)
    assert (leg["delay_minutes"] == 10) is kept


@pytest.mark.parametrize("legs,expected", [
    ([("03100", "09:00", "11:40")], (12, [12])),                                # one leg, kept: 12 on both
    ([("00600", "07:00", "09:00"), ("00500", "09:30", "12:00")], (5, [None, 5])),
    ([("03100", "07:00", "09:00"), ("00500", "09:30", "12:00")], (12, [12, 5])),  # both on the map: the first
])
def test_every_train_gets_its_delay_and_each_leg_its_own(legs, expected):
    """Whatever the train's shape: search() drops `legs` from a direct train, but annotate() doesn't rely on it."""
    from fakes import LIVE_MAP

    at = lambda hhmm: f"2027-01-29T{hhmm}:00+01:00"
    parts = [{"service": n, "departure": at(dep), "arrival": at(arr)} for n, dep, arr in legs]
    train = {"service": "+".join(n for n, _, _ in legs), "departure": parts[0]["departure"],
             "arrival": parts[-1]["arrival"], "legs": parts}
    live.annotate([train], live.parse(LIVE_MAP))
    assert (train["delay_minutes"], [leg["delay_minutes"] for leg in parts]) == expected


def test_an_arrival_the_map_dates_a_day_ahead_is_brought_back(monkeypatch):
    """At 02:14 the map dated a 02:10 arrival the next day. A train held 13 h keeps its date: past stays past."""
    ahead = map_train("03200", 170, "04040", "04040", "2027-01-30T02:09:00", reported="2027-01-29T02:14:00")
    held = map_train("03100", 27, "04040", "04040", "2027-01-28T13:00:00", reported="2027-01-29T02:14:00")
    use(monkeypatch, FakeRenfe(live=live_map(ahead, held, updated="2027-01-29T02:14:19")))
    out = json.loads(invoke("status", "03200").stdout)
    assert out["next_station"]["expected_arrival"] == "2027-01-29T02:09:00+01:00"
    assert out["overdue"] is False and "since about 02:09." in out["notice"]  # 5 min at a station: a stop
    out = json.loads(invoke("status", "03100").stdout)
    assert out["next_station"]["expected_arrival"] == "2027-01-28T13:00:00+01:00"
    assert out["overdue"] is True and "since about 13:00, longer than a stop" in out["notice"]


# --- `renfe watch` -----------------------------------------------------------------------------------------------


class Clock:
    """Fake time for `watch`: sleeping moves it on; `stop_after` sleeps, Ctrl-C."""

    def __init__(self, stop_after=None):
        self.now, self.sleeps, self.stop_after = 0.0, [], stop_after

    def sleep(self, seconds):
        if self.stop_after is not None and len(self.sleeps) >= self.stop_after:
            raise KeyboardInterrupt
        self.sleeps.append(seconds)
        self.now += seconds


def watching(monkeypatch, answers, stop_after=None) -> tuple[Clock, FakeRenfe]:
    """Each check gets the next answer (a map, an exception, or a response); the last one repeats."""
    clock, queue = Clock(stop_after), list(answers)

    def answer(request):
        got = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(got, Exception):
            raise got
        return got if isinstance(got, httpx.Response) else httpx.Response(200, json=got)

    monkeypatch.setattr(cli, "_clock", lambda: clock.now)
    monkeypatch.setattr(cli, "_sleep", clock.sleep)
    return clock, use(monkeypatch, FakeRenfe(**{"flotaLD": answer}))


def at(delay, next_="04040", number="03100"):
    return live_map(map_train(number, delay, "60000", next_, "2027-01-29T10:32:00"))


def lines(result) -> list[dict]:
    return [json.loads(line) for line in result.stdout.splitlines()]


def test_watch_prints_the_first_check_then_only_changes_and_ends_on_its_own(monkeypatch):
    clock, handler = watching(monkeypatch, [at(12), at(12), at(15), at(15, next_="71801"), at(15, next_="71801"),
                                            live_map(), at(15, next_="71801")])
    monkeypatch.setattr(config, "WATCH_LIMIT", 7 * 120.0)
    result = invoke("watch", "03100")
    assert result.exit_code == 0, result.output
    *checks, end = lines(result)
    got = [(o["on_map"], o["delay_minutes"], (o["next_station"] or {}).get("code")) for o in checks]
    assert got == [(True, 12, "04040"), (True, 15, "04040"), (True, 15, "71801"), (False, None, None),
                   (True, 15, "71801")]
    assert end == {"train": "03100", "event": "ended",
                   "message": "watched for 0.233333 h; run it again to keep watching"}  # 840 s here, 12 h for real
    assert checks[0].keys() == json.loads(invoke("status", "03100").stdout).keys()  # status's shape
    assert len(handler.live_requests()) == 8 + 1 and clock.sleeps == [120.0] * 7  # a check at 0, 120 … 840 s
    assert result.stderr == ""


def test_watch_keeps_polling_a_train_that_hasnt_left_yet(monkeypatch):
    watching(monkeypatch, [live_map(), live_map(), at(4)], stop_after=3)
    out = lines(invoke("watch", "3100"))
    assert [o["on_map"] for o in out] == [False, True] and out[1]["delay_minutes"] == 4


def test_a_hiccup_is_reported_once_and_the_watch_goes_on(monkeypatch):
    """Each answer below is one request; a check makes up to 3 (retry.py)."""
    refused, down = httpx.ConnectError("refused"), httpx.Response(503, json={})
    watching(monkeypatch, [at(12),                   # check 1
                           refused, refused, refused,  # check 2: 3 tries, then the outage line
                           down, down, down,           # check 3: still down, no new line
                           at(12),                   # check 4: back, same state, printed anyway
                           at(20)], stop_after=4)    # check 5
    result = invoke("watch", "03100")
    assert result.exit_code == 0, result.output
    out = lines(result)
    assert [o.get("event") or o["delay_minutes"] for o in out] == [12, "map_unreachable", 12, 20]  # one per outage
    assert out[1] == {"train": "03100", "event": "map_unreachable", "error": out[1]["error"], "retries": [
        {"request": "live map", "tries": 3, "errors": ["no connection (ConnectError)"] * 3, "recovered": False}]}
    assert out[1]["error"]["type"] == "upstream_unavailable" and result.stderr == ""
    assert out[2]["retries"] == [] and out[3]["retries"] == []  # each line has its own check's retries


def test_an_outage_from_the_first_check_says_so_at_once(monkeypatch):
    refused = httpx.ConnectError("refused")
    watching(monkeypatch, [refused, refused, refused, at(12)], stop_after=1)
    assert [o.get("event") or o["delay_minutes"] for o in lines(invoke("watch", "03100"))] == ["map_unreachable", 12]


def test_a_check_that_recovers_on_a_retry_prints_its_retries_only_with_a_change(monkeypatch, retry_pauses):
    """The retry isn't part of the change key: an unchanged check that needed one prints nothing."""
    dropped = httpx.RemoteProtocolError("peer closed connection")
    watching(monkeypatch, [at(12), dropped, at(12), dropped, at(15)], stop_after=2)
    out = lines(invoke("watch", "03100"))
    assert [o["delay_minutes"] for o in out] == [12, 15] and retry_pauses == [1.0, 1.0]
    assert out[1]["retries"] == [{"request": "live map", "tries": 2, "recovered": True,
                                  "errors": ["connection dropped (RemoteProtocolError)"]}]


def test_three_unreadable_answers_in_a_row_stop_the_watch(monkeypatch):
    """The map moved or changed: exit 7, instead of 12 h of requests. Two, then an answer, carry on."""
    not_the_map = httpx.Response(404, json={})
    watching(monkeypatch, [at(12), not_the_map, not_the_map, at(12), not_the_map, not_the_map, not_the_map])
    result = invoke("watch", "03100")
    assert result.exit_code == 7
    err = json.loads(result.stderr)["error"]
    assert err["type"] == "api_changed" and "renfe/live.py" in err["hint"]
    assert [o.get("event") or o["delay_minutes"] for o in lines(result)] == [12, "map_unreachable", 12,
                                                                             "map_unreachable"]


def test_a_block_stops_the_watch(monkeypatch):
    clock, _ = watching(monkeypatch, [at(12), httpx.Response(429, json={})])
    result = invoke("watch", "03100")
    assert result.exit_code == 5 and json.loads(result.stderr)["error"]["type"] == "blocked"
    assert [o["delay_minutes"] for o in lines(result)] == [12] and clock.sleeps == [120.0]


def test_ctrl_c_ends_the_watch_cleanly(monkeypatch):
    watching(monkeypatch, [at(12)], stop_after=0)
    result = invoke("watch", "03100")
    assert result.exit_code == 0 and len(lines(result)) == 1


def test_the_time_limit_cuts_the_last_sleep_short(monkeypatch):
    clock, _ = watching(monkeypatch, [at(12)])
    monkeypatch.setattr(config, "WATCH_LIMIT", 300.0)
    assert invoke("watch", "03100").exit_code == 0 and clock.sleeps == [120.0, 120.0, 60.0]


def test_watch_takes_one_train_number(fake):
    result = invoke("watch", "00600+00500")
    assert result.exit_code == 2 and fake.requests == []


def test_watch_prints_a_line_whenever_the_expected_arrival_moves(monkeypatch):
    """Same delay, same next station: Renfe moved the expected arrival alone, by a minute either way (as for 13041
    and 16000 on 18 Nov at 06:29-06:31). Every move is a line, however small; no move, no line."""
    def expecting(when):
        return live_map(map_train("03100", 12, "60000", "04040", f"2027-01-29T{when}"))

    watching(monkeypatch, [expecting("10:32:00"), expecting("10:32:00"), expecting("10:33:00"), expecting("10:32:30"),
                           expecting("10:32:30")], stop_after=4)
    out = lines(invoke("watch", "03100"))
    assert [o["next_station"]["expected_arrival"][11:19] for o in out] == ["10:32:00", "10:33:00", "10:32:30"]
    assert {o["delay_minutes"] for o in out} == {12}


def test_watch_says_once_when_a_held_train_is_overdue_and_again_when_it_moves_on(monkeypatch):
    """Held: same delay, same next station, its expected arrival (10:32) slipping into the past."""
    def seen_at(clock, next_="04040", expected="2027-01-29T10:32:00"):
        stamp = f"2027-01-29T{clock}:00"
        return live_map(map_train("03100", 12, "60000", next_, expected, reported=stamp), updated=stamp)

    watching(monkeypatch, [seen_at("10:30"), seen_at("10:36"), seen_at("10:38"), seen_at("10:40"), seen_at("10:50"),
                           seen_at("10:52", next_="71801", expected="2027-01-29T12:10:00")], stop_after=5)
    out = lines(invoke("watch", "03100"))
    assert [(o["overdue"], o["next_station"]["code"]) for o in out] == [(False, "04040"), (True, "04040"),
                                                                        (False, "71801")]
    assert "has already passed" in out[1]["notice"]  # 10:36 is within the 5-minute grace; 10:38 isn't

@pytest.fixture
def watch_timetable(monkeypatch):
    group = train_numbers.Group("60000", "71801", frozenset({"3100", "3000", "500"}))
    monkeypatch.setattr(train_numbers, "_groups", lambda: {n: [group] for n in group.numbers})
    trips = "\n".join(f"daily,{n},{n}" for n in group.numbers)
    stops = "\n".join(f"{n},1,60000,,09:00:00\n{n},2,71801,12:00:00," for n in group.numbers)
    return timetable(trips, stops, exceptions="daily,20270129,1\ndaily,20270130,1")


def test_ticket_watch_waits_reports_ambiguity_once_then_pins(monkeypatch, watch_timetable):
    def board(*numbers):
        return live_map(*(map_train(n, 12, "60000", "04040", "2027-01-29T10:32:00") for n in numbers))

    both = board("03100", "03000")
    maps = [board(), both, board(), both, board("03100"), board("03000"), httpx.ReadTimeout("timeout"),
            board("03100", "03000")]
    _, fake = watching(monkeypatch, maps)
    monkeypatch.setattr(config, "WATCH_LIMIT", (len(maps) - 1) * 120)
    fake.overrides[TIMETABLE_URL] = lambda r: httpx.Response(200, content=watch_timetable)
    parsed = []
    parse = delay_report._parse

    def counted(*args):
        parsed.append(True)
        return parse(*args)

    monkeypatch.setattr(delay_report, "_parse", counted)
    result = invoke("watch", "500")
    assert result.exit_code == 0, result.output
    waiting, ambiguous, pinned, absent, outage, back, end = lines(result)
    assert waiting["on_map"] is False and waiting["schedule_match"] is None
    assert ambiguous["matching_trains"] == ["03000", "03100"] and ambiguous["schedule_match"] == "ambiguous"
    assert pinned["on_map"] and pinned["train"] == "03100" and pinned["schedule_match"] == "exact"
    assert pinned["scheduled_departure"][11:16] == "09:00" and pinned["scheduled_arrival"][11:16] == "12:00"
    assert absent["train"] == "03100" and not absent["on_map"]
    assert back == pinned and outage["event"] == "map_unreachable" and end["event"] == "ended"
    assert outage["train"] == end["train"] == "03100"
    assert all(row["requested_train"] == "00500" for row in lines(result))
    assert [str(r.url) for r in fake.requests] == [config.LIVE_MAP_URL] * 2 + [TIMETABLE_URL] + [config.LIVE_MAP_URL] * 6
    assert parsed == [True]


def test_watch_switches_from_a_peer_to_the_exact_number_then_keeps_it(monkeypatch, watch_timetable):
    timeout = httpx.ReadTimeout("timeout")
    both = live_map(map_train("03000", 20, "60000", "04040", "2027-01-29T10:32:00"),
                    map_train("03100", 12, "60000", "04040", "2027-01-29T10:32:00"))
    maps = [timeout, at(12, number="03000"), both, at(25), timeout, at(30, number="03000")]
    _, fake = watching(monkeypatch, maps)
    fake.overrides[TIMETABLE_URL] = lambda r: httpx.Response(200, content=watch_timetable)
    monkeypatch.setattr(config, "WATCH_LIMIT", (len(maps) - 1) * 120)
    result = invoke("watch", "3100")
    assert result.exit_code == 0, result.output
    first_outage, peer, exact, later, second_outage, absent, end = lines(result)
    assert [(r["train"], r["delay_minutes"]) for r in (peer, exact, later, absent)] == [
        ("03000", 12), ("03100", 12), ("03100", 25), ("03100", None)]
    assert not absent["on_map"]
    assert all(r["requested_train"] == "03100" for r in (peer, exact, later, absent))
    assert [r["event"] for r in (first_outage, second_outage, end)] == ["map_unreachable", "map_unreachable", "ended"]
    assert all(r["train"] == "03100" and "requested_train" not in r for r in (first_outage, second_outage, end))


@pytest.mark.parametrize("code", [403, 503])
def test_ticket_watch_reports_one_failed_timetable_attempt_then_waits_for_the_exact_number(monkeypatch, watch_timetable, code):
    maps = [live_map(), at(12), at(20), live_map(), at(30), at(5, number="00500")]
    _, fake = watching(monkeypatch, maps, stop_after=len(maps) - 1)
    fake.overrides[TIMETABLE_URL] = lambda r: httpx.Response(code)
    result = invoke("watch", "500")
    assert result.exit_code == 0, result.output
    waiting, failed, direct = lines(result)
    assert not waiting["on_map"] and waiting["schedule_match"] is None
    assert not failed["on_map"] and failed["delay_minutes"] is None and failed["schedule_match"] == "unavailable"
    assert "rest of this watch" in failed["notice"] and str(code) in failed["notice"]
    assert direct["on_map"] and direct["delay_minutes"] == 5
    assert all(r["train"] == r["requested_train"] == "00500" for r in (waiting, failed, direct))
    assert [str(r.url) for r in fake.requests].count(TIMETABLE_URL) == 1


def test_direct_watch_uses_its_number_and_cached_service_days(monkeypatch, watch_timetable):
    def board(day):
        stamp = f"2027-01-{day}T10:32:00"
        return live_map(*(map_train(n, day, "60000", "04040", stamp, reported=stamp) for n in ("03100", "00500")),
                        updated=stamp)

    _, fake = watching(monkeypatch, [board(29), board(30)])
    fake.overrides[TIMETABLE_URL] = lambda r: httpx.Response(200, content=watch_timetable)
    monkeypatch.setattr(config, "WATCH_LIMIT", 120)
    first, next_day, end = lines(invoke("watch", "3100"))
    assert first["train"] == next_day["train"] == "03100"
    assert [r["scheduled_arrival"][:10] for r in (first, next_day)] == ["2027-01-29", "2027-01-30"]
    assert set(end) == {"train", "event", "message"}
    assert [str(r.url) for r in fake.requests].count(TIMETABLE_URL) == 1


@pytest.mark.parametrize("response", [httpx.Response(429), httpx.Response(503)])
def test_watch_keeps_live_changes_after_one_failed_timetable_attempt(monkeypatch, response):
    _, fake = watching(monkeypatch, [at(12), at(15), at(15)], stop_after=2)
    fake.overrides[TIMETABLE_URL] = lambda r: response
    result = invoke("watch", "3100")
    assert result.exit_code == 0
    rows = lines(result)
    assert [r["delay_minutes"] for r in rows] == [12, 15]
    assert all(r["schedule_match"] == "unavailable" and str(response.status_code) in r["notice"] for r in rows)
    assert [str(r.url) for r in fake.requests].count(TIMETABLE_URL) == 1


def test_the_map_request_names_the_cli():
    agents = []

    def handler(request):
        agents.append(request.headers["user-agent"])
        return httpx.Response(200, json={"fechaActualizacion": "2026-11-20T10:00:00", "trenes": []})

    live.fetch(transport=httpx.MockTransport(handler))
    assert agents and agents[0].startswith("renfe-cli/")
