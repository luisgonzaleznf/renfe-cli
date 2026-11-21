import sys
from pathlib import Path

import httpx
import pytest

from renfe import config

sys.path.insert(0, str(Path(__file__).parent))  # lets tests import the shared `fakes` module


@pytest.fixture(autouse=True)
def no_real_browser(monkeypatch):
    """No test may open a browser: `renfe open` tests replace this with a recorder."""
    import webbrowser

    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a real browser")

    monkeypatch.setattr(webbrowser, "open", refuse)


@pytest.fixture(autouse=True)
def no_real_network(request, monkeypatch):
    """Offline tests never reach Renfe: every request must go through a MockTransport. The live tests may."""
    if request.node.get_closest_marker("live"):
        return

    def refuse(self, req):
        raise AssertionError(f"a test tried to reach the real network ({req.url.host})")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)


@pytest.fixture(autouse=True)
def frozen_clock(request, monkeypatch):
    """Pin the CLI's "now" to 2026-11-15 10:00 Madrid, so a test's fixed dates never fall in the past or hit the
    today filter as the real calendar moves on. Tests that need another moment pin it again (`frozen_at`). The live
    tests keep the real clock."""
    if request.node.get_closest_marker("live"):
        return
    from datetime import datetime as real_datetime

    from renfe import cli, search as search_mod

    class Frozen(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return real_datetime(2026, 11, 15, 10, 0, tzinfo=search_mod.MADRID)

    monkeypatch.setattr(cli, "datetime", Frozen)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Every test gets its own cache dir: no test reads or writes the real session, stations or pace
    files (the live test included)."""
    monkeypatch.setenv(config.CACHE_ENV, str(tmp_path_factory.mktemp("cache")))


@pytest.fixture(autouse=True)
def shipped_stations(monkeypatch):
    """Lookups use a small fake shipped list, so refreshing the real one never changes a test. Tests of the
    real file use `stations.bundled` as imported before this runs."""
    from fakes import SHIPPED

    from renfe import stations

    every = stations.parse(SHIPPED)
    monkeypatch.setattr(stations, "bundled", lambda: every)


@pytest.fixture(autouse=True)
def retry_pauses(request, monkeypatch):
    """Offline tests never really wait between retries: the pauses are recorded here instead."""
    pauses: list[float] = []
    if not request.node.get_closest_marker("live"):
        from renfe import retry

        monkeypatch.setattr(retry, "_sleep", pauses.append)
    return pauses
