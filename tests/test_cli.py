"""The `renfe` command line: JSON on stdout, errors as JSON on stderr with the documented exit codes."""

import json

import httpx
import pytest
from typer.testing import CliRunner

from renfe import cli, config
from renfe.client import RsvClient

from fakes import STATIONS, FakeRenfe

runner = CliRunner()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe()
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    return handler


def invoke(*args):
    return runner.invoke(cli.app, list(args))


def test_search_outputs_json(fake):
    result = invoke("search", "Madrid", "Barcelona", "--date", "2027-01-29", "--limit", "2")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["origin"]["code"] == "MADRI" and out["destination"]["code"] == "BARCE"
    assert out["count"] == 2 and out["trains"][0]["fares"]
    assert config.STATIONS_PATH not in fake.paths()  # both stations came from the shipped list


def test_search_no_fares(fake):
    result = invoke("search", "60000", "71801", "-d", "2027-01-29", "--no-fares")
    assert result.exit_code == 0
    assert "/browsing/journey/v1/orientations/journey-search" not in fake.paths()


@pytest.mark.parametrize("day", ["2026-11-20", "2027-01-29"])
def test_arrive_by_is_inclusive_and_applies_before_limit_and_fares(fake, day):
    # The train at 07:00 arrives at noon. The train at 09:00 arrives first.
    result = invoke("search", "Madrid", "Barcelona", "-d", day, "--after", "07:00", "--arrive-by", "11:40",
                    "--limit", "1")
    assert result.exit_code == 0, result.output
    row, = json.loads(result.stdout)["trains"]
    assert row["service"] == "03100" and row["fares"]
    requests = [r for r in fake.requests if r.url.path == config.JOURNEY_SEARCH_PATH]
    if day == "2027-01-29":
        request, = requests
        routes = json.loads(request.content)["travels"][0]["routes"]
        assert [leg["service_name"] for route in routes for leg in route["legs"]] == ["03100"]
    else:
        assert not requests


def test_arrival_deadline_can_exclude_every_train_even_when_trains_still_depart_today(fake):
    result = invoke("search", "Madrid", "Barcelona", "--arrive-by", "10:00")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["trains"] == [] and out["notice"] is None


@pytest.mark.parametrize("args,needle", [
    (("search", "Madrid", "Barcelona", "--date", "29/01/2027"), "YYYY-MM-DD"),
    (("search", "Madrid", "Barcelona", "--after", "25:00"), "HH:MM"),
    (("search", "Madrid", "Barcelona", "--arrive-by", "24:00"), "--arrive-by must be HH:MM"),
    (("search", "Narnia", "Barcelona"), "no station matches"),
    (("search", "Madrid", "MADRI"), "same station"),
])
def test_usage_errors(fake, args, needle):
    result = invoke(*args)
    assert result.exit_code == 2
    err = json.loads(result.stderr)["error"]
    assert err["type"] == "usage" and needle in err["message"]
    assert result.stdout == ""


def test_blocked_exit_code(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe(**{"stations": lambda r: httpx.Response(429, json={}, headers={"retry-after": "30"})})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("stations", "Narnia")  # not in the shipped list, so Renfe is asked
    assert result.exit_code == 5
    err = json.loads(result.stderr)["error"]
    assert err["type"] == "blocked" and "retry after 30" in err["message"]


def test_unexpected_exception_reports_type_only(monkeypatch):
    def broken():
        raise RuntimeError("Bearer secret-token-value")

    monkeypatch.setattr(cli, "make_client", broken)
    result = invoke("search", "Madrid", "Barcelona", "-d", "2027-01-29")
    assert result.exit_code == 1
    assert "secret-token-value" not in result.stderr and "RuntimeError" in result.stderr


def test_stations(fake):
    result = invoke("stations", "madrid", "--limit", "2")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert [s["code"] for s in out["stations"]] == ["MADRI", "60000"]
    assert fake.paths() == []  # the shipped list answered: no request at all


def test_stations_asks_renfe_only_when_the_shipped_list_has_no_match(fake):
    out = json.loads(invoke("stations", "apeadero").stdout)
    assert [s["code"] for s in out["stations"]] == ["99001"]
    assert fake.paths().count(config.STATIONS_PATH) == 1
    assert json.loads(invoke("stations", "Narnia").stdout)["count"] == 0  # nowhere: an empty list, exit 0


def test_stations_refresh_uses_renfe_live_list(fake):
    out = json.loads(invoke("stations", "--refresh", "--limit", "50").stdout)
    assert out["count"] == len(STATIONS) and fake.paths().count(config.STATIONS_PATH) == 1


def test_search_takes_a_station_only_renfe_knows(fake):
    result = invoke("search", "Apeadero Nuevo", "Barcelona", "-d", "2027-01-29", "--no-fares")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["origin"]["code"] == "99001"
    assert fake.paths().count(config.STATIONS_PATH) == 1


def test_version():
    result = invoke("--version")
    assert result.exit_code == 0 and result.stdout.strip()


@pytest.mark.parametrize("argv,needle", [
    (["search", "Madrid"], "destination"),
    (["search", "Madrid", "Barcelona", "--limit", "0"], "--limit"),
    (["nonsense"], "nonsense"),
])
def test_main_renders_click_usage_errors_as_json(fake, capsys, argv, needle):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv)
    assert exc.value.code == 2
    err = json.loads(capsys.readouterr().err)["error"]
    assert err["type"] == "usage" and needle in err["message"]


def test_main_success_and_failure_exit_codes(fake, capsys):
    with pytest.raises(SystemExit) as ok:
        cli.main(["stations", "madrid", "--limit", "1"])
    assert ok.value.code == 0 and json.loads(capsys.readouterr().out)["count"] == 1
    with pytest.raises(SystemExit) as bad:
        cli.main(["search", "Narnia", "Madrid"])
    assert bad.value.code == 2 and json.loads(capsys.readouterr().err)["error"]["type"] == "usage"


def test_today_hides_trains_that_already_left(fake, monkeypatch):
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 1, 29, 9, 30, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Frozen)
    result = invoke("search", "Madrid", "Barcelona", "--no-fares")
    assert result.exit_code == 0, result.output
    departures = [t["departure"][11:16] for t in json.loads(result.stdout)["trains"]]
    assert departures and min(departures) >= "09:30"


def test_block_during_fares_prints_partial_result_and_exits_5(monkeypatch, tmp_path):
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path))
    handler = FakeRenfe(**{"journey-search": lambda r: httpx.Response(503, json={})})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("search", "Madrid", "Barcelona", "--date", "2027-01-29")
    assert result.exit_code == 5
    out = json.loads(result.stdout)
    assert out["count"] == 4 and out["fares_error"]["type"] == "upstream_unavailable"
    assert json.loads(result.stderr)["error"]["http_status"] == 503


def test_today_with_every_train_gone_says_so(fake, monkeypatch):
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class Late(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 1, 29, 23, 30, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Late)
    result = invoke("search", "Madrid", "Barcelona", "--no-fares")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["count"] == 0 and "No more trains today after 23:30" in out["notice"]


@pytest.fixture
def opened(monkeypatch):
    """Records the URLs `renfe open` asks the browser to open, instead of opening them."""
    import webbrowser

    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url, new=0: urls.append(url) or True)
    return urls


def test_search_includes_the_shop_link(fake):
    out = json.loads(invoke("search", "Madrid", "Barcelona", "--date", "2027-01-29", "--limit", "1").stdout)
    assert out["shop"]["url"].startswith("https://venta.rsv.renfe.com/coext/search?")


def test_open_without_a_train_sends_no_search(fake, opened):
    result = invoke("open", "Madrid", "Barcelona", "--date", "2026-11-20", "--browser")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["opened"] is True and out["train"] is None
    assert opened == [out["shop"]["url"]] and "departure-date=2026-11-20" in opened[0]
    assert fake.requests == []  # both stations came from the shipped list; no train was asked about


def test_open_with_a_train_checks_it_and_shows_it(fake, opened):
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", "3100", "--browser")  # leading zero optional
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["train"]["service"] == "03100" and out["train"]["departure"].startswith("2027-01-29T09:00")
    assert len(opened) == 1 and "originUicStationCode=MADRI" in opened[0]
    assert config.JOURNEY_SEARCH_PATH not in fake.paths()  # the check needs the timetable only


def test_open_with_a_train_that_has_a_change(fake, opened):
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "-t", "600 + 500", "--browser").stdout)
    assert out["train"]["service"] == "00600+00500"


def test_open_with_an_unknown_train_is_a_usage_error_and_opens_nothing(fake, opened):
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", "99999", "--browser")
    assert result.exit_code == 2
    assert "no train 99999" in json.loads(result.stderr)["error"]["message"]
    assert opened == []


def test_open_no_browser_only_prints_the_link(fake, opened):
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--no-browser").stdout)
    assert out["opened"] is False and opened == [] and out["shop"]["url"]


def test_open_before_the_cutoff_checks_the_legacy_shops_list(fake, opened):
    result = invoke("open", "Madrid", "Barcelona", "-d", "2026-11-20", "--train", "3100", "--browser")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["train"]["service"] == "03100" and out["train"]["departure"] == "2026-11-20T09:00:00+01:00"
    assert out["notice"] is None and len(opened) == 1 and "departure-date=2026-11-20" in opened[0]
    assert not fake.new_system_requests()


def test_open_before_the_cutoff_when_the_legacy_shop_fails_a_train_it_cant_see_still_opens(monkeypatch, opened):
    """Then the list is the new system's partial one, so a missing train isn't an error."""
    handler = FakeRenfe(**{"buscarTren.do": lambda r: httpx.Response(429)})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("open", "Madrid", "Barcelona", "-d", "2026-11-20", "--train", "03141", "--browser")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert out["train"] is None and "look for it on Renfe's page" in out["notice"]
    assert "legacy shop" in out["notice"] and "(429)" in out["notice"] and "no fares" not in out["notice"]
    assert len(opened) == 1 and "departure-date=2026-11-20" in opened[0]


def test_open_without_a_terminal_only_prints_the_link(fake, opened):
    """CliRunner's stdout isn't a TTY, like a script's or an agent's: no browser unless --browser."""
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29").stdout)
    assert out["opened"] is False and opened == []


@pytest.mark.parametrize("train", ["", "+", "abc", "03081x", "1++2", "1234567", "3 100", "\u0663\u0661\u0660\u0660"])
def test_open_rejects_what_isnt_a_train_number(fake, opened, train):
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", train, "--browser")
    assert result.exit_code == 2 and "--train must be a train number" in json.loads(result.stderr)["error"]["message"]
    assert opened == [] and config.SEARCH_TRAVEL_PATH not in fake.paths()


@pytest.mark.parametrize("day,legacy", [("2027-01-24", True), ("2027-01-25", False)])
def test_open_unknown_train_around_the_cutoff(fake, opened, day, legacy):
    """The legacy shop's list on its last day, the new system's from the cut-off: complete either way."""
    result = invoke("open", "Madrid", "Barcelona", "-d", day, "--train", "99999", "--browser")
    assert result.exit_code == 2 and opened == []
    assert bool(fake.legacy_requests()) is legacy and bool(fake.new_system_requests()) is not legacy


def test_open_unknown_train_when_renfe_says_the_list_is_short(monkeypatch, tmp_path, opened):
    """From the cut-off, if Renfe reports more trains than it returned, a train not seen still opens."""
    from fakes import TRAVEL_OPTIONS

    short = dict(TRAVEL_OPTIONS, total_count=9)
    handler = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(200, json=short)})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", "99999", "--browser")
    assert result.exit_code == 0, result.output
    out = json.loads(result.stdout)
    assert "reported 9 trains" in out["notice"] and "isn't in the list" in out["notice"] and len(opened) == 1


def test_open_today_reports_the_scheduled_departure(fake, opened, monkeypatch):
    from datetime import datetime as real_datetime

    from renfe import search as search_mod

    class LaterThatDay(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2027, 1, 29, 12, 0, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", LaterThatDay)
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "today", "--train", "03100").stdout)
    assert out["train"]["service"] == "03100" and "was scheduled to leave at 09:00" in out["notice"]
    assert "renfe status 03100" in out["notice"] and not fake.live_requests()


def test_open_explains_a_missing_train_on_todays_legacy_list(fake, monkeypatch):
    from datetime import datetime

    class LegacyDay(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 20, 12, 0, tzinfo=tz)

    monkeypatch.setattr(cli, "datetime", LegacyDay)
    result = invoke("open", "Madrid", "Barcelona", "--train", "00500", "--no-browser")
    assert result.exit_code == 2
    assert "about 5 minutes before it leaves the searched station" in json.loads(result.stderr)["error"]["message"]
    assert not fake.live_requests()


def test_open_same_station_is_a_usage_error(fake, opened):
    result = invoke("open", "Madrid", "MADRI", "-d", "2027-01-29", "--browser")
    assert result.exit_code == 2 and opened == []


def test_open_block_during_the_train_check_opens_nothing(monkeypatch, tmp_path, opened):
    handler = FakeRenfe(**{config.SEARCH_TRAVEL_PATH: lambda r: httpx.Response(429, json={})})
    monkeypatch.setattr(cli, "make_client", lambda: RsvClient(transport=httpx.MockTransport(handler), spacing=0))
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--train", "03100", "--browser")
    assert result.exit_code == 5 and result.stdout == "" and opened == []


def test_open_browser_failure_is_reported_not_fatal(fake, monkeypatch):
    import webbrowser

    def fails(url, new=0):
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setattr(webbrowser, "open", fails)
    result = invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29", "--browser")
    assert result.exit_code == 0 and json.loads(result.stdout)["opened"] is False


@pytest.mark.parametrize("day,needle", [
    ("2026-11-14", "in the past"),
    ("2028-01-01", "more than a year ahead"),
    ("9999-12-31", "more than a year ahead"),
    ("20261120", "YYYY-MM-DD"),
    ("2026-W47-5", "YYYY-MM-DD"),
])
def test_dates_out_of_range_or_odd_formats_are_usage_errors(fake, day, needle):
    result = invoke("search", "Madrid", "Barcelona", "--date", day)
    assert result.exit_code == 2 and needle in json.loads(result.stderr)["error"]["message"]


def test_open_from_a_terminal_opens_the_browser_by_default(fake, opened, monkeypatch):
    monkeypatch.setattr(cli, "_stdout_is_terminal", lambda: True)
    out = json.loads(invoke("open", "Madrid", "Barcelona", "-d", "2027-01-29").stdout)
    assert out["opened"] is True and opened == [out["shop"]["url"]]


def test_checkout_uses_one_way_guest_flow(fake, monkeypatch):
    calls = []

    def checkout(src, dst, day, method, train, review_only, progress):
        calls.append((src.code, dst.code, day.isoformat(), method.value, train, review_only))
        return {"status": "reviewed", "selected_method": method.value, "total": "20.00 €"}

    monkeypatch.setattr(cli.checkout_mod, "checkout", checkout)
    result = invoke("checkout", "Madrid", "Barcelona", "-d", "tomorrow", "--train", "00601 + 00533", "--review-only")
    assert result.exit_code == 0
    assert json.loads(result.stdout)["selected_method"] == "paypal"
    assert calls == [("MADRI", "BARCE", "2026-11-16", "paypal", ["601", "533"], True)]
    assert not fake.requests


@pytest.mark.parametrize("opts", [["-d", "2027-01-25"], ["--train", "ABC"], ["--method", "unknown"]])
def test_invalid_checkout_never_opens_browser(fake, opts):
    assert invoke("checkout", "Madrid", "Barcelona", *opts).exit_code == 2
    assert not fake.requests


def test_apple_pay_opens_safari_with_the_requested_route_and_date(fake, monkeypatch):
    opened = []
    monkeypatch.setattr(cli.shop_mod, "open_in_safari", lambda url: opened.append(url) or True)
    result = invoke("checkout", "Madrid", "Barcelona", "-d", "tomorrow", "--method", "apple-pay", "-t", "00601+00533")
    assert result.exit_code == 0
    out = json.loads(result.stdout)
    assert out["status"] == "handed_off" and out["requested_method"] == "apple-pay"
    assert opened == [out["url"]]
    assert "origin=MADRI&destination=BARCE&departure-date=2026-11-16" in out["url"]
    assert "Apple Pay" in out["next"] and "601+533" in out["next"]
    assert not fake.requests


def test_apple_pay_review_never_opens_a_browser(fake, monkeypatch):
    monkeypatch.setattr(cli.shop_mod, "open_in_safari", lambda url: pytest.fail("unexpected Safari launch"))
    result = invoke("checkout", "Madrid", "Barcelona", "--method", "apple-pay", "--review-only")
    assert result.exit_code == 2 and "Review-only" in result.stderr
    assert not fake.requests


def test_apple_pay_unavailable_reports_no_handoff(fake, monkeypatch):
    monkeypatch.setattr(cli.shop_mod, "open_in_safari", lambda url: False)
    result = invoke("checkout", "Madrid", "Barcelona", "--method", "apple-pay")
    assert result.exit_code == 2 and not result.stdout


def test_typo_suggestions_are_separate_from_real_stations(fake):
    out = json.loads(invoke("stations", "Mdrid").stdout)
    assert out["count"] == 0 and out["stations"] == []
    assert [s["code"] for s in out["suggestions"]][:2] == ["MADRI", "60000"]
    assert fake.paths().count(config.STATIONS_PATH) == 1
    assert "suggestions" not in json.loads(invoke("stations", "Madrid").stdout)
    result = invoke("search", "Mdrid", "Barcelona")
    assert result.exit_code == 2 and "Did you mean Madrid (Todas) (MADRI)" in result.stderr
    assert config.SEARCH_TRAVEL_PATH not in fake.paths()
