from datetime import timedelta

from test_source_sim import Sim


def latest(sim):
    return sim.get().json()["observations"][-1]


def test_schema_drift_renames_the_value_field_everywhere():
    sim = Sim()
    sim.scenario("schema_drift")
    cell = latest(sim)["FXUSDCAD"]
    assert "v" not in cell and "value" in cell


def test_missing_series_drops_one_series_from_every_row():
    sim = Sim()
    sim.scenario("missing_series", params={"series": "FXCNYCAD"})
    assert all("FXCNYCAD" not in o for o in sim.get().json()["observations"])


def test_null_bad_type_and_out_of_range_damage_only_the_newest_row_by_default():
    sim = Sim()
    sim.scenario("null_values")
    body = sim.get().json()["observations"]
    assert body[-1]["FXEURCAD"] == {"v": None} and body[0]["FXEURCAD"]["v"] is not None
    sim.scenario("bad_type", params={"series": "FXGBPCAD", "text": "1,80"})
    assert latest(sim)["FXGBPCAD"] == {"v": "1,80"}
    sim.scenario("out_of_range", params={"series": "FXJPYCAD", "rows": "all"})
    assert {o["FXJPYCAD"]["v"] for o in sim.get().json()["observations"]} == {"0.0000"}


def test_duplicates_repeat_the_newest_day_with_one_series_disagreeing():
    sim = Sim()
    sim.scenario("duplicates")
    a, b = sim.get().json()["observations"][-2:]
    assert a["d"] == b["d"] and a["FXUSDCAD"] != b["FXUSDCAD"] and a["FXEURCAD"] == b["FXEURCAD"]


def test_stale_holds_back_the_newest_days():
    sim = Sim()
    sim.scenario("stale", params={"days": 6})
    assert latest(sim)["d"] <= "2026-10-29"


def test_jump_moves_only_the_newest_day_and_all_series_together():
    sim = Sim()
    healthy = latest(sim)
    sim.scenario("jump", params={"pct": 0.10})
    damaged = sim.get().json()["observations"]
    for series in ("FXUSDCAD", "FXMXNCAD", "FXGBPCAD"):
        assert abs(float(damaged[-1][series]["v"]) / float(healthy[series]["v"]) - 1.10) < 0.005


def test_bad_date_corrupts_the_newest_date_string():
    sim = Sim()
    sim.scenario("bad_date")
    assert latest(sim)["d"] == "2026-13-45"


def test_p_is_the_share_of_requests_hit_and_a_seed_makes_it_repeatable():
    def faults(seed):
        sim = Sim()
        sim.scenario("http_500", p=0.5, seed=seed)
        return [sim.get().status_code for _ in range(200)]

    run = faults(1)
    assert 70 < run.count(500) < 130
    assert run == faults(1)


def test_first_n_makes_a_fault_that_goes_away():
    sim = Sim()
    sim.scenario("http_500", params={"first_n": 2})
    assert [sim.get().status_code for _ in range(4)] == [500, 500, 200, 200]


def test_ttl_ends_the_fault_by_itself():
    sim = Sim()
    sim.scenario("http_500", ttl_s=60)
    assert sim.get().status_code == 500
    sim.now += timedelta(seconds=61)
    assert sim.get().status_code == 200


def test_admin_switch_validates_and_resets():
    sim = Sim()
    assert sim.client.post("/admin/scenario", json={"name": "meteor"}).status_code == 422
    sim.scenario("http_500")
    assert sim.client.get("/admin/scenario").json()["name"] == "http_500"
    assert sim.client.delete("/admin/scenario").json()["name"] == "ok"
    assert sim.get().status_code == 200
