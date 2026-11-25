"""search(): request shapes, timetable parsing, fare batching and matching, the coexistence cut-off."""

import json
import threading
from datetime import date, datetime, time, timedelta

import httpx
import pytest

from renfe import config, search
from renfe.client import RsvClient
from renfe.errors import ApiShapeError
from renfe.stations import parse

from fakes import STATIONS, TRAVEL_OPTIONS, FakeRenfe, path

EVERY = {s.code: s for s in parse(STATIONS)}
NAMES = {code: s.name for code, s in EVERY.items()}
MADRI, BARCE = EVERY["MADRI"], EVERY["BARCE"]


def run(fake, day=date(2027, 1, 29), **kw):
    with RsvClient(transport=httpx.MockTransport(fake), spacing=0) as c:
        return search.search(c, MADRI, BARCE, day, names=NAMES, **kw)


def bodies(fake, path):
    return [json.loads(r.content) for r in fake.requests if r.url.path == path]


def test_utc_window_follows_madrid_time():
    assert search.utc_window(date(2027, 1, 25)) == ("2027-01-24T23:00:00.000Z", "2027-01-25T23:00:00.000Z")
    assert search.utc_window(date(2027, 7, 1)) == ("2027-06-30T22:00:00.000Z", "2027-07-01T22:00:00.000Z")
    # the day the clocks go forward is 23 hours long
    assert search.utc_window(date(2027, 3, 28)) == ("2027-03-27T23:00:00.000Z", "2027-03-28T22:00:00.000Z")


def test_search_travel_request_shape():
    fake = FakeRenfe()
    run(fake, fares=False)
    (body,) = bodies(fake, config.SEARCH_TRAVEL_PATH)
    assert body == {"depart_after_datetime": "2027-01-28T23:00:00.000Z",
                    "depart_before_datetime": "2027-01-29T23:00:00.000Z",
                    "travel_path": [{"code": "MADRI"}, {"code": "BARCE"}], "direction": "OUTBOUND"}
    assert not bodies(fake, config.JOURNEY_SEARCH_PATH)


def test_trains_sorted_local_times_and_shape():
    out = run(FakeRenfe(), fares=False)
    assert [t["service"] for t in out["trains"]] == ["03000", "00600+00500", "03100", "03200"]
    first = out["trains"][0]
    assert first["departure"] == "2027-01-29T06:30:00+01:00" and first["arrival"] == "2027-01-29T09:15:00+01:00"
    assert first["duration_minutes"] == 165 and first["changes"] == 0 and "legs" not in first
    assert first["intermediate_stops"] == 1
    change = out["trains"][1]
    assert change["changes"] == 1
    assert [leg["destination"]["name"] for leg in change["legs"]] == ["Zaragoza-Delicias", "Barcelona-Sants"]
    assert all(not k.startswith("_") for leg in change["legs"] for k in leg)
    assert out["fares_included"] is False and out["notice"] is None and out["count"] == 4


def test_fares_batched_by_three_and_matched():
    fake = FakeRenfe()
    out = run(fake)
    # the two calls run in parallel, so they may arrive in either order
    calls = sorted(bodies(fake, config.JOURNEY_SEARCH_PATH), key=lambda c: -len(c["travels"][0]["routes"]))
    assert [len(c["travels"][0]["routes"]) for c in calls] == [3, 1]
    travel = calls[0]["travels"][0]
    assert travel["direction"] == "outbound" and travel["departure"] == "2027-01-29"
    assert travel["excluded_product_types"] == ["DG", "BK"] and travel["id"] == "travel_outbound"
    assert calls[0]["passengers"] == [{"disability_type": "SD", "type": "A", "id": "passenger_1",
                                       "travel_passes": [], "discount_cards": []}]
    leg = travel["routes"][0]["legs"][0]
    assert leg == {"origin": "60000", "destination": "71801", "departure": "2027-01-29T05:30:00Z",
                   "arrival": "2027-01-29T08:15:00Z", "service_name": "03000", "service_type": "AV1",
                   "service_schedule_date": "2027-01-29"}
    assert out["fares_included"] is True
    for train in out["trains"]:
        assert train["available"] is True
        assert train["type"] == ("AVE + AVE" if train["changes"] else "AVE")  # one name per leg, as the web
        assert [f["name"] for f in train["fares"]] == ["Básico", "Elige"]  # cheapest first
    assert out["trains"][0]["fares"][0]["price"] == 30.0
    assert out["trains"][3]["fares"][0]["price"] == 30.0  # second batch starts again at route 0


def test_before_cutoff_the_legacy_shop_gives_every_train_and_its_fares():
    fake = FakeRenfe()
    out = run(fake, day=date(2026, 11, 21))
    assert not fake.new_system_requests()  # nothing asked of the new system, which prices none of them
    assert out["fares_included"] is True and out["notice"] is None and out["count"] == 5
    assert out["trains"][0]["fares"] and out["trains"][0]["departure"].startswith("2026-11-21T07:00")


def test_after_and_limit():
    out = run(FakeRenfe(), fares=False, after=time(8, 0), limit=1)
    assert [t["service"] for t in out["trains"]] == ["03100"]


def test_today_hides_only_trains_that_have_left_even_the_night_the_clocks_go_back():
    """31 Oct 2027: 02:40 CEST is 00:40 UTC, and a train at 02:20 CET (01:20 UTC) leaves 40 minutes later."""
    night = {"total_count": 2, "travel_options": [
        {"travel_duration": 160, "travel_path": [path("03000", "2027-10-31T00:20:00Z", "2027-10-31T03:00:00Z")]},
        {"travel_duration": 160, "travel_path": [path("03100", "2027-10-31T01:20:00Z", "2027-10-31T04:00:00Z")]}]}
    now = datetime(2027, 10, 31, 2, 40, fold=0, tzinfo=search.MADRID)  # the first 02:40, summer time
    assert now.utcoffset() == timedelta(hours=2)
    out = run(FakeRenfe(**{"search-travel": lambda r: httpx.Response(200, json=night)}), day=date(2027, 10, 31),
              fares=False, now=now, from_now=True)
    assert [t["service"] for t in out["trains"]] == ["03100"]  # 03000 left at 02:20 CEST; 03100 is still to leave
    assert out["trains"][0]["departure"] == "2027-10-31T02:20:00+01:00" and out["notice"] is None


def test_today_compares_a_station_in_another_time_zone_by_the_moment():
    """If Renfe labels a station Europe/Lisbon (a synthetic answer here, not seen from Renfe): 09:00 in Madrid is
    08:00 in Porto, so a train leaving Porto at 08:30 local time is still to leave, though 08:30 reads earlier."""
    leg = path("00421", "2027-01-29T08:30:00Z", "2027-01-29T10:30:00Z", origin="94346", destination="22308")
    leg["departure_datetime_timezone"] = "Europe/Lisbon"
    options = {"total_count": 1, "travel_options": [{"travel_duration": 120, "travel_path": [leg]}]}
    now = datetime(2027, 1, 29, 9, 0, tzinfo=search.MADRID)
    out = run(FakeRenfe(**{"search-travel": lambda r: httpx.Response(200, json=options)}), fares=False, now=now,
              from_now=True)
    assert [t["departure"] for t in out["trains"]] == ["2027-01-29T08:30:00+00:00"] and out["notice"] is None


def test_today_hides_a_train_that_left_in_the_first_of_the_repeated_hours():
    """31 Oct 2027: at 02:10 CET (01:10 UTC), a train at 02:30 CEST (00:30 UTC) left 40 minutes before."""
    night = {"total_count": 1, "travel_options": [
        {"travel_duration": 160, "travel_path": [path("03000", "2027-10-31T00:30:00Z", "2027-10-31T03:10:00Z")]}]}
    now = datetime(2027, 10, 31, 2, 10, fold=1, tzinfo=search.MADRID)  # the second 02:10, winter time
    assert now.utcoffset() == timedelta(hours=1)
    out = run(FakeRenfe(**{"search-travel": lambda r: httpx.Response(200, json=night)}), day=date(2027, 10, 31),
              fares=False, now=now, from_now=True)
    assert out["trains"] == [] and out["notice"] == "No more trains today after 02:10; try --date tomorrow."




@pytest.mark.parametrize("day", [date(2026, 11, 20), date(2027, 1, 29)])
def test_arrive_by_excludes_next_day_arrivals_and_skips_empty_fare_batches(day):
    overnight = {"travel_duration": 165, "travel_path": [path("03400", "2027-01-29T21:30:00Z", "2027-01-30T00:15:00Z")]}
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json={"travel_options": [overnight]})})
    out = run(fake, day=day, after=time(22), arrive_by=time(23, 59))
    assert out["count"] == 0 and out["notice"] is None
    assert not bodies(fake, config.JOURNEY_SEARCH_PATH)


@pytest.mark.parametrize("doc", [{}, {"travel_options": None}, {"travel_options": [{"travel_path": []}]},
                                 {"travel_options": [{"travel_path": [{"service_name": "1"}]}]}])
def test_search_travel_shape_errors(doc):
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=doc)})
    with pytest.raises(ApiShapeError):
        run(fake, fares=False)


def test_journey_search_shape_error():
    fake = FakeRenfe(**{config.JOURNEY_SEARCH_PATH: lambda r: httpx.Response(200, json={"data": {}})})
    with pytest.raises(ApiShapeError):
        run(fake)


def test_no_trains():
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json={"travel_options": []})})
    out = run(fake)
    assert out["count"] == 0 and out["trains"] == [] and not bodies(fake, config.JOURNEY_SEARCH_PATH)


def test_fixture_is_not_mutated():
    before = json.dumps(TRAVEL_OPTIONS, sort_keys=True)
    run(FakeRenfe())
    assert json.dumps(TRAVEL_OPTIONS, sort_keys=True) == before


def _route(service, local_departure, price=30.0):
    return {"legs": [{"service_name": service, "departure_station": {"departure_timestamp": local_departure}}],
            "bundles": [{"product_family_id": "F1", "price": price, "currency": "EUR"}]}


def _answer(*routes):
    return lambda request: httpx.Response(200, json={"data": {"offer": {"product_families": [{"id": "F1", "name": "Básico"}],
                                                                        "travels": [{"routes": list(routes)}]}}})


def test_no_fare_matching_any_train_is_an_api_change():
    fake = FakeRenfe(**{config.JOURNEY_SEARCH_PATH: _answer(_route("99999", "2027-01-29T00:00:00+0100"))})
    with pytest.raises(ApiShapeError, match="no returned fare matches"):
        run(fake)


def test_partial_fare_miss_sets_a_notice():
    # first batch: only the 06:30 train (03000) is priced; second batch (03200) normally. The batches
    # run in parallel, so tell them apart by content, not by arrival order.
    def answer(request):
        routes = json.loads(request.content)["travels"][0]["routes"]
        if routes[0]["legs"][0]["service_name"] == "03000":
            return _answer(_route("03000", "2027-01-29T06:30:00+0100"))(request)
        return _answer(_route("03200", "2027-01-29T13:00:00+0100"))(request)

    out = run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: answer}))
    priced = [t["service"] for t in out["trains"] if t["fares"]]
    assert priced == ["03000", "03200"]
    assert "2 train(s) came back without fares" in out["notice"]


def test_summer_time_fares_match():
    summer = {"total_count": 1, "travel_options": [
        {"travel_duration": 160, "travel_path": [path("03100", "2027-07-01T06:00:00Z", "2027-07-01T08:40:00Z")]}]}
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=summer)})
    out = run(fake, day=date(2027, 7, 1))
    (train,) = out["trains"]
    assert train["departure"] == "2027-07-01T08:00:00+02:00" and train["fares"]


def test_october_window_is_25_hours():
    assert search.utc_window(date(2027, 10, 31)) == ("2027-10-30T22:00:00.000Z", "2027-10-31T23:00:00.000Z")


def test_block_during_fares_keeps_the_timetable_and_stops(monkeypatch):
    monkeypatch.setattr(config, "FARE_WORKERS", 1)  # one at a time, so the second batch is still queued
    fake = FakeRenfe(**{config.JOURNEY_SEARCH_PATH: lambda r: httpx.Response(429, json={})})
    out = run(fake)
    assert len(bodies(fake, config.JOURNEY_SEARCH_PATH)) == 1  # no further request after the 429
    assert out["count"] == 4 and all(t["fares"] is None for t in out["trains"])
    assert out["fares_included"] is False and out["fares_error"]["http_status"] == 429
    assert "Wait before trying again" in out["notice"]


def test_total_count_mismatch_sets_a_notice():
    short = dict(TRAVEL_OPTIONS, total_count=9)
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=short)})
    assert "reported 9 trains but returned 4" in run(fake, fares=False)["notice"]


@pytest.mark.parametrize("answer", [
    {"data": {"offer": {"travels": [{"routes": None}]}}},
    {"data": {"offer": {"travels": [{"routes": ["not a route"]}]}}},
    {"data": {"offer": {"travels": [{"routes": [{"legs": ["not a leg"]}]}]}}},
    {"data": {"offer": {"travels": [{"routes": [{"legs": [{"service_name": "03000", "departure_station": "x"}]}]}]}}},
    {"data": {"offer": {"travels": [{"routes": [{"legs": [{"service_name": "03000", "departure_station":
        {"departure_timestamp": "2027-01-29T06:30:00+0100"}}], "bundles": [{"price": float("nan")}]}]}]}}},
    {"data": {"offer": {"travels": [{"routes": [{"legs": [{"service_name": "03000", "departure_station":
        {"departure_timestamp": "2027-01-29T06:30:00+0100"}}], "bundles": [{"price": 10 ** 400}]}]}]}}},
])
def test_journey_search_shape_drift_is_an_api_change(answer):
    fake = FakeRenfe(**{config.JOURNEY_SEARCH_PATH: lambda r: httpx.Response(200, content=json.dumps(answer),
                                                                             headers={"content-type": "application/json"})})
    with pytest.raises(ApiShapeError):
        run(fake)


@pytest.mark.parametrize("bad_leg", [
    {**path("03000", "2027-01-29T05:30:00Z", "2027-01-29T08:15:00Z"), "service_name": 3000},
    {**path("03000", "2027-01-29T05:30:00Z", "2027-01-29T08:15:00Z"), "departure_datetime": "yesterday"},
    "not a leg",
])
def test_search_travel_leg_drift_is_an_api_change(bad_leg):
    doc = {"travel_options": [{"travel_duration": 1, "travel_path": [bad_leg]}]}
    fake = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=doc)})
    with pytest.raises(ApiShapeError):
        run(fake, fares=False)


def services(request) -> str:
    """The trains a journey-search call asks about, e.g. "03000,00600+00500,03100"."""
    routes = json.loads(request.content)["travels"][0]["routes"]
    return ",".join("+".join(leg["service_name"] for leg in route["legs"]) for route in routes)


def test_fare_batches_run_in_parallel_up_to_the_cap(monkeypatch):
    monkeypatch.setattr(config, "FARE_BATCH", 1)  # 4 trains -> 4 calls, one more than the cap
    lock, in_flight, peak, arrived = threading.Lock(), [0], [0], [0]
    gate = threading.Barrier(3, timeout=5)  # the first three calls only pass together

    def journey(request):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            arrived[0] += 1
            first_three = arrived[0] <= 3
        if first_three:
            gate.wait()
        with lock:
            in_flight[0] -= 1
        return fake_priced(request)

    out = run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))
    assert out["fares_included"] is True and all(t["fares"] for t in out["trains"])
    assert peak[0] == config.FARE_WORKERS == 3


def test_fare_workers_are_daemon_threads():
    seen = []

    def journey(request):
        seen.append(threading.current_thread().daemon)
        return fake_priced(request)

    run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))
    assert seen and all(seen)  # Ctrl-C never waits for a call in flight


def test_block_in_a_later_batch_keeps_earlier_fares():
    first_sent = threading.Event()

    def journey(request):
        if services(request) == "03200":  # the second batch: fail only once the first is on its way
            first_sent.wait(5)
            return httpx.Response(429, json={})
        first_sent.set()
        return fake_priced(request)

    out = run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))
    priced = [t["service"] for t in out["trains"] if t["fares"]]
    assert priced == ["03000", "00600+00500", "03100"] and out["fares_error"]["http_status"] == 429
    assert out["fares_included"] is False
    assert "1 train(s) have no fares" in out["notice"]


@pytest.mark.parametrize("later_first", [False, True])
def test_first_block_in_batch_order_is_reported(monkeypatch, later_first):
    monkeypatch.setattr(config, "FARE_BATCH", 1)
    both_sent = threading.Barrier(2, timeout=5)
    answered = threading.Event()
    seen = set()

    def journey(request):
        batch = services(request)
        if batch not in ("03000", "00600+00500"):
            return fake_priced(request)
        if batch not in seen:  # a retry of the 503 (until the 429 stops it) answers at once
            seen.add(batch)
            both_sent.wait()  # first and second batch are both in flight
            # answer in either order: the error reported must not depend on it
            if (batch == "00600+00500") != later_first:
                answered.wait(5)
            answered.set()
        return httpx.Response(503 if batch == "03000" else 429, json={})

    out = run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))
    assert out["fares_error"]["http_status"] == 503


@pytest.mark.parametrize("later_first", [False, True])
def test_a_block_beats_another_error_whatever_the_timing(monkeypatch, later_first):
    """Batch 0 is rate-limited while batch 1 gets 401: always a partial result (exit 5), never exit 3."""
    monkeypatch.setattr(config, "FARE_BATCH", 1)
    both_sent = threading.Barrier(2, timeout=5)
    answered = threading.Event()

    def journey(request):
        batch = services(request)
        if batch not in ("03000", "00600+00500"):
            return fake_priced(request)
        both_sent.wait()
        if (batch == "00600+00500") != later_first:
            answered.wait(5)
        answered.set()
        return httpx.Response(429 if batch == "03000" else 401, json={})

    out = run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))
    assert out["count"] == 4 and out["fares_error"]["http_status"] == 429


def test_other_errors_without_a_block_raise_the_first_in_batch_order(monkeypatch):
    monkeypatch.setattr(config, "FARE_BATCH", 1)
    both_sent = threading.Barrier(2, timeout=5)

    def journey(request):
        batch = services(request)
        if batch == "03000":
            both_sent.wait()
            return httpx.Response(200, text="not json", headers={"content-type": "application/json"})
        if batch == "00600+00500":
            both_sent.wait()
            return httpx.Response(401, json={})
        return fake_priced(request)

    with pytest.raises(ApiShapeError):  # batch 0's error, though batch 1 failed too
        run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))


def test_a_bug_in_a_worker_is_never_hidden_by_a_block(monkeypatch):
    monkeypatch.setattr(config, "FARE_BATCH", 1)
    both_sent = threading.Barrier(2, timeout=5)

    def journey(request):
        batch = services(request)
        if batch in ("03000", "00600+00500"):
            both_sent.wait()
            if batch == "03000":
                return httpx.Response(429, json={})
            raise RuntimeError("a bug")
        return fake_priced(request)

    with pytest.raises(RuntimeError):
        run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: journey}))


@pytest.mark.parametrize("response", [
    httpx.Response(200, text="not json", headers={"content-type": "application/json"}),
    httpx.Response(200, content=b"null", headers={"content-type": "application/json"}),
])
def test_unreadable_fare_answer_from_a_worker_is_an_api_change(response):
    with pytest.raises(ApiShapeError):
        run(FakeRenfe(**{config.JOURNEY_SEARCH_PATH: lambda r: response}))


def fake_priced(request):
    from fakes import priced
    return httpx.Response(200, json=priced(json.loads(request.content)["travels"][0]["routes"]))
