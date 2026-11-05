import json
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from lab.source_sim import data
from lab.source_sim.app import create_app

WED_NOON = datetime(2026, 11, 4, 12, 0)  # a Wednesday, before the 16:30 Eastern publish time
URL = "/valet/observations/FXUSDCAD,FXEURCAD,FXGBPCAD,FXJPYCAD,FXCNYCAD,FXAUDCAD,FXCHFCAD,FXMXNCAD/json"


class Sim:
    def __init__(self, now=WED_NOON):
        self.now, self.slept = now, []
        self.client = TestClient(create_app(clock=lambda: self.now, sleeper=self.sleep))

    async def sleep(self, seconds):
        self.slept.append(seconds)

    def scenario(self, name, **kw):
        r = self.client.post("/admin/scenario", json={"name": name, **kw})
        assert r.status_code == 200, r.text
        return r

    def get(self, recent=5):
        return self.client.get(URL, params={"recent": recent})


@pytest.fixture
def sim():
    return Sim()


def test_the_healthy_answer_has_the_valet_shape_and_stable_numbers(sim):
    body = sim.get().json()
    assert set(body) == {"terms", "seriesDetail", "observations"}
    assert [o["d"] for o in body["observations"]] == ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02", "2026-11-03"]
    assert set(body["observations"][0]) == {"d", *data.SERIES}
    assert body == sim.get().json()
    assert 1.2 < float(body["observations"][-1]["FXUSDCAD"]["v"]) < 1.6


def test_a_day_is_published_only_after_the_publish_time_and_never_on_weekends():
    assert data.latest_published(datetime(2026, 11, 4, 12, 0)) == date(2026, 11, 3)
    assert data.latest_published(datetime(2026, 11, 4, 22, 0)) == date(2026, 11, 4)
    assert data.latest_published(datetime(2026, 11, 7, 23, 0)) == date(2026, 11, 6)
    assert data.latest_published(datetime(2026, 11, 8, 3, 0)) == date(2026, 11, 6)
    assert data.latest_published(datetime.now(UTC).replace(tzinfo=None)).weekday() < 5


def test_daily_moves_stay_far_below_the_audit_threshold():
    for series in data.SERIES:
        days = data.business_days(date(2026, 11, 3), 60)
        rates = [data.rate(series, d) for d in days]
        assert max(abs(b / a - 1) for a, b in zip(rates, rates[1:], strict=False)) < 0.03


def test_unknown_series_is_a_404_with_the_message_valet_uses(sim):
    r = sim.client.get("/valet/observations/FXFOOCAD/json")
    assert r.status_code == 404 and r.json() == {"message": "Series FXFOOCAD not found."}


def test_start_date_limits_the_window(sim):
    r = sim.client.get(URL, params={"start_date": "2026-11-02"})
    assert [o["d"] for o in r.json()["observations"]] == ["2026-11-02", "2026-11-03"]


def test_http_faults_use_the_status_codes_the_taxonomy_expects(sim):
    sim.scenario("http_500")
    assert sim.get().status_code == 500
    sim.scenario("rate_limit", params={"retry_after_s": 45})
    r = sim.get()
    assert r.status_code == 429 and r.headers["Retry-After"] == "45"
    sim.scenario("http_404")
    assert sim.get().status_code == 404


def test_timeout_and_slow_answer_late_but_correctly(sim):
    sim.scenario("timeout", params={"hang_s": 20})
    assert sim.get().status_code == 200 and sim.slept == [20.0]


def test_damaged_bodies(sim):
    sim.scenario("truncated")
    with pytest.raises(json.JSONDecodeError):
        json.loads(sim.get().text)
    sim.scenario("html")
    assert sim.get().text.startswith("<html>")
    sim.scenario("empty")
    assert sim.get().json()["observations"] == []
