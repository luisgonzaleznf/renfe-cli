"""Retries for one-off failures (retry.py), wherever the CLI calls Renfe, and what the output says about them.
`retry_pauses` (conftest) records the pauses instead of waiting."""

import json
from datetime import datetime as real_datetime

import httpx
import pytest
from typer.testing import CliRunner

from renfe import cli, config, live, retry
from renfe.delays import TIMETABLE_URL
from renfe.errors import UpstreamError

from fakes import FakeRenfe, flaky, live_map, path

runner = CliRunner()
DROPPED = httpx.RemoteProtocolError("Server disconnected without sending a response.")
DOWN = httpx.Response(503, json={})


def use(monkeypatch, fake: FakeRenfe) -> FakeRenfe:
    monkeypatch.setattr(cli, "make_client", lambda: cli.RsvClient(transport=httpx.MockTransport(fake), spacing=0))
    return fake


def frozen_at(monkeypatch, moment: str) -> None:
    pinned = real_datetime.fromisoformat(moment).replace(tzinfo=live.MADRID)

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return pinned

    monkeypatch.setattr(cli, "datetime", Frozen)


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def sent(fake: FakeRenfe, part: str) -> int:
    return sum(part in str(r.url) for r in fake.requests)


# --- the new system ----------------------------------------------------------------------------------------------


def test_a_server_error_is_retried_and_the_output_says_so(monkeypatch, retry_pauses):
    fake = FakeRenfe()
    fake.overrides[config.SEARCH_TRAVEL_PATH] = flaky(fake, httpx.Response(500, json={}))
    use(monkeypatch, fake)
    out = json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29", "--no-fares").stdout)
    assert out["count"] == 4 and out["notice"] is None and retry_pauses == [1.0]
    assert out["retries"] == [{"request": "search-travel", "tries": 2, "errors": ["server error (500)"],
                               "recovered": True}]
    assert sent(fake, config.SEARCH_TRAVEL_PATH) == 2


def test_nothing_retried_is_an_empty_list(monkeypatch):
    use(monkeypatch, FakeRenfe())
    assert json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29").stdout)["retries"] == []


def test_the_web_config_and_the_token_are_retried_too(monkeypatch):
    fake = FakeRenfe()
    fake.overrides["arch-config"] = flaky(fake, DROPPED)
    fake.overrides[config.PUBLIC_TOKEN_PATH] = flaky(fake, httpx.Response(502, json={}))
    use(monkeypatch, fake)
    out = json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29", "--no-fares").stdout)
    assert [(r["request"], r["errors"], r["recovered"]) for r in out["retries"]] == [
        ("web config", ["connection dropped (RemoteProtocolError)"], True),
        ("anonymous token", ["server error (502)"], True)]


def test_three_failed_tries_give_the_same_error_as_one_did(monkeypatch, retry_pauses):
    fake = use(monkeypatch, FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: DOWN}))
    result = invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29")
    assert result.exit_code == 5 and result.stdout == ""
    assert json.loads(result.stderr)["error"] == {  # the error as before, and what was retried
        "type": "upstream_unavailable", "exit_code": 5, "http_status": 503,
        "message": "search-travel: Renfe returned a server error (503)",
        "retries": [{"request": "search-travel", "tries": 3, "errors": ["server error (503)"] * 3, "recovered": False}]}
    assert sent(fake, config.SEARCH_TRAVEL_PATH) == 3 and retry_pauses == [1.0, 2.0]


@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(429, json={}),
    lambda r: httpx.Response(403, json={}),
    lambda r: httpx.Response(503, headers={"content-type": "text/html"}, text="<html>busy</html>"),  # a page: a block
    lambda r: httpx.Response(501, json={}),
    lambda r: httpx.Response(400, json={}),
    lambda r: httpx.Response(200, text="not json"),
    lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("slow")),  # it already cost the whole timeout
])
def test_blocks_timeouts_and_the_rest_are_tried_once(monkeypatch, retry_pauses, answer):
    fake = use(monkeypatch, FakeRenfe(**{config.SEARCH_TRAVEL_PATH: answer}))
    result = invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29")
    assert result.exit_code != 0 and sent(fake, config.SEARCH_TRAVEL_PATH) == 1 and retry_pauses == []


def test_a_retry_is_not_a_notice_so_open_still_trusts_the_list(monkeypatch):
    """`open --train` reads a notice as "the list may be incomplete"; a retry that worked isn't that."""
    monkeypatch.setattr("webbrowser.open", lambda url, new=0: True)
    fake = FakeRenfe()
    fake.overrides[config.SEARCH_TRAVEL_PATH] = flaky(fake, DOWN)
    use(monkeypatch, fake)
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", "99999", "--no-browser")
    assert result.exit_code == 2  # a train that isn't there is still a usage error


def test_no_retry_is_sent_once_the_caller_says_stop():
    """Fare batches: once another batch was blocked during the pause, the retry isn't sent."""
    calls, stop = [], [False]

    def send():
        calls.append(1)
        raise UpstreamError("journey-search: Renfe returned a server error (503)", transient="server error (503)")

    log: list[dict] = []
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(retry, "_sleep", lambda pause: stop.__setitem__(0, True))  # another batch blocked meanwhile
        with pytest.raises(UpstreamError):
            retry.call("journey-search", send, log, may_retry=lambda: not stop[0])
    assert calls == [1] and log == []  # asked again after the pause: nothing re-sent, nothing retried


# --- the legacy shop ---------------------------------------------------------------------------------------------


def legacy_search(monkeypatch, fake: FakeRenfe) -> dict:
    frozen_at(monkeypatch, "2026-11-15T10:00")
    use(monkeypatch, fake)
    return json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2026-11-20").stdout)


def test_the_legacy_shop_starts_over_with_fresh_cookies(monkeypatch):
    """As on 18 Nov at 15:34: the results page answered 500 once."""
    fake = FakeRenfe()
    page = httpx.Response(500, headers={"content-type": "text/html"}, text="<html>Error 500</html>")  # HTML: its own
    fake.overrides["buscarTrenEnlaces.do"] = flaky(fake, page)
    out = legacy_search(monkeypatch, fake)
    assert out["notice"] is None and out["fares_included"] is True and out["count"] == 5
    assert out["retries"] == [{"request": "legacy shop search (all four requests, started over)", "tries": 2,
                               "errors": ["legacy shop results page: server error (500)"], "recovered": True}]
    steps = [r.url.path.rsplit("/", 1)[-1] for r in fake.legacy_requests()]
    assert steps == ["buscarTren.do", "buscarTrenEnlaces.do",  # the first try, failed at step 2
                     "buscarTren.do", "buscarTrenEnlaces.do", "__System.generateId.dwr",
                     "trainEnlacesManager.getTrainsList.dwr"]  # search only: nothing selected or confirmed
    first_page, restart = fake.legacy_requests()[1], fake.legacy_requests()[2]
    assert "JSESSIONID=" in first_page.headers.get("cookie", "") and "cookie" not in restart.headers  # fresh cookies
    assert not fake.new_system_requests()  # no fallback


def test_a_dropped_connection_at_the_last_step_starts_over_too(monkeypatch):
    fake = FakeRenfe()
    fake.overrides["getTrainsList"] = flaky(fake, httpx.ReadError("Connection reset by peer"))
    out = legacy_search(monkeypatch, fake)
    assert out["notice"] is None and len(fake.legacy_requests()) == 4 + 4
    assert out["retries"][0]["errors"] == ["legacy shop train list: connection dropped (ReadError)"]


def test_the_legacy_shop_falls_back_as_before_after_three_tries(monkeypatch, retry_pauses):
    fake = use(monkeypatch, FakeRenfe(**{"buscarTrenEnlaces.do": lambda r: httpx.Response(502)}))
    out = legacy_search(monkeypatch, fake)
    assert "failed (legacy shop results page: Renfe returned a server error (502))" in out["notice"]
    assert out["count"] == 4 and out["fares_included"] is False and retry_pauses == [1.0, 2.0]
    assert out["retries"] == [{"request": "legacy shop search (all four requests, started over)", "tries": 3,
                               "errors": ["legacy shop results page: server error (502)"] * 3, "recovered": False}]


def test_a_legacy_read_timeout_starts_over_once(monkeypatch, retry_pauses):
    """As on 19 Nov at 01:19, when one train list hung for the full timeout; now LEGACY_TIMEOUT, then a fresh try."""
    fake = FakeRenfe()
    fake.overrides["getTrainsList"] = flaky(fake, httpx.ReadTimeout("slow"))
    out = legacy_search(monkeypatch, fake)
    assert out["notice"] is None and out["fares_included"] is True and len(fake.legacy_requests()) == 4 + 4
    assert out["retries"] == [{"request": "legacy shop search (all four requests, started over)", "tries": 2,
                               "errors": ["legacy shop train list: no answer in time"], "recovered": True}]
    assert retry_pauses == [1.0]
    assert {r.extensions["timeout"]["read"] for r in fake.legacy_requests()} == {config.LEGACY_TIMEOUT} == {12.0}


def test_a_second_legacy_read_timeout_falls_back(monkeypatch, retry_pauses):
    """A read timeout after a 500 still gets its one more try; two read timeouts end the search, whatever's left."""
    fake = FakeRenfe()
    fake.overrides["getTrainsList"] = flaky(fake, httpx.ReadTimeout("slow"), httpx.ReadTimeout("slow"))
    out = legacy_search(monkeypatch, fake)
    assert "failed (legacy shop train list: Renfe did not answer in time)" in out["notice"]
    assert out["retries"] == [{"request": "legacy shop search (all four requests, started over)", "tries": 2,
                               "errors": ["legacy shop train list: no answer in time",
                                          "legacy shop train list: Renfe did not answer in time"],
                               "recovered": False}]
    assert retry_pauses == [1.0] and len(fake.legacy_requests()) == 4 + 4
    fake = FakeRenfe()
    page = httpx.Response(500, headers={"content-type": "text/html"}, text="<html>Error 500</html>")
    fake.overrides["buscarTrenEnlaces.do"] = flaky(fake, page)
    fake.overrides["getTrainsList"] = flaky(fake, httpx.ReadTimeout("slow"))
    out = legacy_search(monkeypatch, fake)
    assert out["notice"] is None and out["retries"][0]["tries"] == 3


def test_a_legacy_block_falls_back_at_once(monkeypatch, retry_pauses):
    fake = use(monkeypatch, FakeRenfe(**{"buscarTrenEnlaces.do": lambda r: httpx.Response(429)}))
    out = legacy_search(monkeypatch, fake)
    assert "(429)" in out["notice"] and len(fake.legacy_requests()) == 2 and retry_pauses == []
    assert out["retries"] == []


# --- the live map ------------------------------------------------------------------------------------------------


def test_status_retries_the_map_and_says_so(monkeypatch):
    fake = FakeRenfe()
    fake.overrides["flotaLD"] = flaky(fake, DROPPED, DROPPED)
    use(monkeypatch, fake)
    out = json.loads(invoke("status", "03100").stdout)
    assert out["delay_minutes"] == 12 and len(fake.live_requests()) == 3
    assert [str(r.url) for r in fake.requests].count(TIMETABLE_URL) == 1
    assert out["retries"] == [{"request": "live map", "tries": 3, "recovered": True,
                               "errors": ["connection dropped (RemoteProtocolError)"] * 2}]


def test_status_gives_up_after_three_tries_with_the_same_error(monkeypatch, retry_pauses):
    fake = use(monkeypatch, FakeRenfe(**{"flotaLD": lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused"))}))
    result = invoke("status", "03100")
    assert result.exit_code == 5 and len(fake.live_requests()) == 3 and retry_pauses == [1.0, 2.0]
    err = json.loads(result.stderr)["error"]
    assert err["message"] == "live map: request failed (ConnectError)" and err["retries"] == [
        {"request": "live map", "tries": 3, "errors": ["no connection (ConnectError)"] * 3, "recovered": False}]


@pytest.mark.parametrize("answer", [
    lambda r: httpx.Response(429, json={}),
    lambda r: (_ for _ in ()).throw(httpx.ConnectTimeout("slow")),
])
def test_status_tries_a_block_or_a_timeout_once(monkeypatch, answer):
    fake = use(monkeypatch, FakeRenfe(**{"flotaLD": answer}))
    assert invoke("status", "03100").exit_code == 5 and len(fake.live_requests()) == 1


def test_a_search_retries_the_map_inside_its_budget(monkeypatch):
    frozen_at(monkeypatch, "2027-01-29T09:30")
    fake = FakeRenfe()
    fake.overrides["flotaLD"] = flaky(fake, DROPPED)
    use(monkeypatch, fake)
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00").stdout)
    assert out["delays_included"] is True and out["notice"] is None
    assert {"request": "live map", "tries": 2, "errors": ["connection dropped (RemoteProtocolError)"],
            "recovered": True} in out["retries"]


def test_a_search_doesnt_retry_the_map_past_its_budget(monkeypatch, retry_pauses):
    """With 0.5 s, a 1 s pause can't fit: no retry, and the search doesn't wait for one."""
    frozen_at(monkeypatch, "2027-01-29T09:30")
    monkeypatch.setattr(config, "LIVE_SEARCH_BUDGET", 0.5)
    fake = FakeRenfe()
    fake.overrides["flotaLD"] = flaky(fake, DROPPED)
    use(monkeypatch, fake)
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00").stdout)
    assert out["delays_included"] is False and len(fake.live_requests()) == 1 and retry_pauses == []
    assert "Delays are unknown (live map: request failed (RemoteProtocolError))" in out["notice"]
    assert out["retries"] == []


def test_watch_lines_carry_their_checks_retries(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(cli, "_clock", lambda: clock[0])
    monkeypatch.setattr(cli, "_sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(config, "WATCH_LIMIT", 0.0)  # one check
    fake = FakeRenfe()
    fake.overrides["flotaLD"] = flaky(fake, httpx.Response(504, json={}))
    use(monkeypatch, fake)
    out = [json.loads(line) for line in invoke("watch", "03100").stdout.splitlines()]
    assert out[0]["delay_minutes"] == 12 and out[0]["retries"][0]["errors"] == ["server error (504)"]
    assert out[1]["event"] == "ended"


# --- the empty day -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("arrival", [[], ["--arrive-by", "20:00"]])
def test_after_the_last_train_an_empty_day_isnt_a_failure(monkeypatch, arrival):
    """As on 18 Nov at 22:04: the legacy shop lists only trains that haven't left, and the new system has none
    leaving from now on either."""
    frozen_at(monkeypatch, "2026-11-15T23:30")
    that_day = {"total_count": 2, "travel_options": [  # the new system's list for that day: all gone by 23:30
        {"travel_duration": 160, "travel_path": [path("03100", "2026-11-15T08:00:00Z", "2026-11-15T10:40:00Z")]},
        {"travel_duration": 165, "travel_path": [path("03400", "2026-11-15T21:30:00Z", "2026-11-16T00:15:00Z")]}]}
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(
        200, json=that_day)}))
    out = json.loads(invoke("search", "Madrid", "Barcelona", *arrival).stdout)
    assert out["count"] == 0 and out["notice"] == "No more trains today after 23:30; try --date tomorrow."
    assert out["fares_included"] is False  # nothing was priced


def test_interactive_checkout_explains_when_the_last_train_has_left(monkeypatch):
    frozen_at(monkeypatch, "2026-11-15T23:30")
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{
        config.SEARCH_TRAVEL_PATH: lambda _: httpx.Response(200, json={"total_count": 0, "travel_options": []}),
    }))
    result = invoke("checkout", "Madrid", "Barcelona", "--interactive")
    assert result.exit_code == 2 and not result.stdout
    assert json.loads(result.stderr)["error"]["message"] == "No more trains today after 23:30; try --date tomorrow."


def new_system_on(day: str, *departures: str) -> dict:
    """The new system's list for `day`: one train per Madrid departure time ("09:00"), in winter (UTC+1)."""
    from datetime import datetime as dt, timedelta

    options = []
    for n, hhmm in enumerate(departures):
        dep = dt.fromisoformat(f"{day}T{hhmm}") - timedelta(hours=1)
        arr = dep + timedelta(hours=2, minutes=40)
        options.append({"travel_duration": 160, "travel_path": [path(
            f"03{n + 1}00", dep.strftime("%Y-%m-%dT%H:%M:%SZ"), arr.strftime("%Y-%m-%dT%H:%M:%SZ"))]})
    return {"total_count": len(options), "travel_options": options}


@pytest.mark.parametrize("clock,after,listed", [
    ("23:30", "06:00", 2),   # `--after` keeps the day's trains: the new system's partial list, said so
    ("21:57", None, 1),      # the 22:04 train is 7 minutes away: the shop should still list it
    ("21:30", "21:00", 1),   # likewise with `--after`
])
def test_an_empty_legacy_list_still_falls_back_when_the_new_system_list_is_shown(monkeypatch, clock, after, listed):
    frozen_at(monkeypatch, f"2026-11-15T{clock}")
    that_day = new_system_on("2026-11-15", "09:00", "22:04")
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(
        200, json=that_day)}))
    out = json.loads(invoke("search", "Madrid", "Barcelona", *(["--after", after] if after else [])).stdout)
    assert out["count"] == listed and "failed (it listed no trains)" in out["notice"]
    assert "may be missing trains" in out["notice"] and out["fares_included"] is False


@pytest.mark.parametrize("clock,after", [
    ("22:04:30", None),     # as on 19 Nov at 21:49: the last train leaves this minute, the new system still lists it
    ("21:58:01", None),     # just inside the shop's cut-off (and the minute of margin)
    ("21:59", "21:00"),     # `--after` changes nothing when what it keeps is leaving now
])
def test_the_last_train_leaving_now_isnt_a_legacy_failure(monkeypatch, clock, after):
    """The legacy shop stops listing a train about 5 minutes before it leaves; the new system lists it until its
    minute is over. The list shows it, from the new system, and the notice says why, not that the shop failed."""
    frozen_at(monkeypatch, f"2026-11-15T{clock}")
    that_day = new_system_on("2026-11-15", "09:00", "22:04")
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(
        200, json=that_day)}))
    out = json.loads(invoke("search", "Madrid", "Barcelona", *(["--after", after] if after else [])).stdout)
    assert [t["departure"][11:16] for t in out["trains"]] == ["22:04"]
    assert out["notice"] == ("Renfe's legacy shop (https://venta.renfe.com/vol/) stops listing a train about 5 minutes "
                             "before it leaves, so the trains leaving now are from Renfe's new system, without fares.")
    assert out["fares_included"] is False and out["trains"][0]["fares"] is None
    no_fares = json.loads(invoke("search", "Madrid", "Barcelona", "--no-fares").stdout)
    assert no_fares["notice"].endswith("are from Renfe's new system.")


@pytest.mark.parametrize("clock,departure", [("08:00", []), ("23:30", ["--after", "06:00"])])
def test_arrival_deadline_keeps_the_legacy_fallback_notice(monkeypatch, clock, departure):
    frozen_at(monkeypatch, f"2026-11-15T{clock}")
    that_day = new_system_on("2026-11-15", "09:00")
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=that_day)}))
    result = invoke("search", "Madrid", "Barcelona", "--arrive-by", "10:00", *departure)
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["trains"] == [] and "may be missing trains" in out["notice"] and "No more trains" not in out["notice"]


@pytest.mark.parametrize("date", ["today", "2026-11-20"])
def test_after_judges_nothing_the_real_now_does(monkeypatch, date):
    """At 10:00, `--after 23:50` empties the new system's list, but trains still leave today (and any day but today
    proves nothing): the shop's empty list stays unexplained."""
    frozen_at(monkeypatch, "2026-11-15T10:00")
    use(monkeypatch, FakeRenfe(legacy_trains=[]))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "-d", date, "--after", "23:50").stdout)
    assert out["count"] == 0 and "failed (it listed no trains)" in out["notice"] and out["fares_included"] is False


def test_another_day_with_no_trains_in_either_system_keeps_the_fallback_notice(monkeypatch):
    """A station key the shop doesn't know and a route the new system doesn't list look like this too."""
    frozen_at(monkeypatch, "2026-11-15T10:00")
    use(monkeypatch, FakeRenfe(legacy_trains=[], **{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(
        200, json={"total_count": 0, "travel_options": []})}))
    out = json.loads(invoke("search", "Madrid", "Barcelona", "-d", "2026-11-20").stdout)
    assert out["count"] == 0 and "failed (it listed no trains)" in out["notice"]
    result = invoke("open", "Madrid", "Barcelona", "-d", "2026-11-20", "--train", "03100", "--no-browser")
    assert result.exit_code == 0, result.output  # the list may be incomplete: look for it on Renfe's page
    assert "isn't in the list this CLI can see" in json.loads(result.stdout)["notice"]


def test_open_today_after_the_last_train_keeps_the_fallback_notice(monkeypatch):
    """`open` never judges the day over: a notice there means the list may be incomplete."""
    frozen_at(monkeypatch, "2026-11-15T23:30")
    use(monkeypatch, FakeRenfe(legacy_trains=[]))
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "today", "--train", "03100", "--no-browser").stdout)
    assert out["train"]["service"] == "03100" and "failed (it listed no trains)" in out["notice"]


# --- review round 5 ----------------------------------------------------------------------------------------------


def test_a_503_page_from_the_map_is_a_block_tried_once(monkeypatch):
    page = httpx.Response(503, headers={"content-type": "text/html"}, text="<html>Service Unavailable</html>")
    fake = use(monkeypatch, FakeRenfe(**{"flotaLD": lambda r: page}))
    result = invoke("status", "03100")
    assert result.exit_code == 5 and json.loads(result.stderr)["error"]["type"] == "blocked"
    assert len(fake.live_requests()) == 1 and "retries" not in json.loads(result.stderr)["error"]


@pytest.mark.parametrize("answer", [
    httpx.Response(503, headers={"content-type": "text/html"}, text="<html>Service Unavailable</html>"),
    httpx.Response(504, headers={"content-type": "text/html"}, text="<html>Gateway Timeout</html>"),
    httpx.Response(500, headers={"content-type": "text/html", "retry-after": "120"}, text="<html>Later</html>"),
])
def test_the_legacy_shop_retries_no_503_504_or_retry_after(monkeypatch, retry_pauses, answer):
    """There they may be an edge deny or a waiting room: fall back at once, as before round 4."""
    fake = FakeRenfe(**{"buscarTrenEnlaces.do": lambda r: answer})
    out = legacy_search(monkeypatch, fake)
    assert len(fake.legacy_requests()) == 2 and retry_pauses == [] and out["retries"] == []
    assert f"server error ({answer.status_code})" in out["notice"]


def test_a_map_retry_still_running_when_the_search_stops_waiting_shows(monkeypatch):
    """Try 1 drops; try 2 hangs past the budget. The search stops waiting, and says what it saw."""
    import threading

    frozen_at(monkeypatch, "2027-01-29T09:30")
    monkeypatch.setattr(config, "LIVE_SEARCH_BUDGET", 1.2)
    release, tries = threading.Event(), []
    fake = FakeRenfe()

    def map_answer(request):
        tries.append(request)
        if len(tries) == 1:
            raise DROPPED
        release.wait(10)  # the second try hangs until the test is over
        return fake.default(request)

    fake.overrides["flotaLD"] = map_answer
    use(monkeypatch, fake)
    try:
        out = json.loads(invoke("search", "Madrid", "Barcelona", "--after", "06:00").stdout)
    finally:
        release.set()
    assert out["delays_included"] is False and "no answer within 1.2 s" in out["notice"]
    assert out["retries"] == [{"request": "live map", "tries": 2, "recovered": False, "errors": [
        "connection dropped (RemoteProtocolError)", "no answer within the search's 1.2 s budget"]}]


def batch_of(request: httpx.Request) -> str:
    routes = json.loads(request.content)["travels"][0]["routes"]
    return ",".join("+".join(leg["service_name"] for leg in route["legs"]) for route in routes)


def test_a_fare_batch_is_retried_through_the_thread_pool(monkeypatch):
    """Two batches of fares in flight; the second answers 503 once, then prices its train."""
    fake = FakeRenfe()
    failed = []

    def journey(request):
        if batch_of(request) == "03200" and not failed:
            failed.append(1)
            return DOWN
        return fake.default(request)

    fake.overrides[config.JOURNEY_SEARCH_PATH] = journey
    use(monkeypatch, fake)
    result = invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["fares_included"] is True and all(t["fares"] for t in out["trains"]) and "fares_error" not in out
    assert out["retries"] == [{"request": "journey-search", "tries": 2, "errors": ["server error (503)"],
                               "recovered": True}]
    assert sent(fake, config.JOURNEY_SEARCH_PATH) == 3


def test_a_fare_batch_isnt_retried_once_another_was_blocked(monkeypatch):
    """The first batch's 503 waits out its pause until the second batch's 429 has stopped the fares (search.py's
    `failed` event), whichever answers first: it is never sent again."""
    import threading
    from types import SimpleNamespace

    from renfe import search as search_mod

    events = []

    class Recorded(threading.Event):
        def __init__(self):
            super().__init__()
            events.append(self)

    monkeypatch.setattr(search_mod, "threading", SimpleNamespace(Event=Recorded, Lock=threading.Lock,
                                                                 Thread=threading.Thread))
    monkeypatch.setattr(retry, "_sleep", lambda pause: events[0].wait(5))
    fake = use(monkeypatch, FakeRenfe(**{config.JOURNEY_SEARCH_PATH: lambda r: DOWN if batch_of(r) != "03200"
                                         else httpx.Response(429, json={})}))
    result = invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29")
    assert result.exit_code == 5 and json.loads(result.stdout)["fares_error"]["http_status"] == 503  # batch order
    first = [r for r in fake.requests if config.JOURNEY_SEARCH_PATH in str(r.url) and batch_of(r) != "03200"]
    assert len(first) == 1 and json.loads(result.stdout)["retries"] == []
