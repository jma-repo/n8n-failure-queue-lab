from datetime import date

import pytest

BASE = {"USD": 1.40, "EUR": 1.57, "GBP": 1.80, "JPY": 0.0092, "CNY": 0.209, "AUD": 0.91, "CHF": 1.58, "MXN": 0.075}
D1, D2, D3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)


def day(d, factor=1.0, moved=None):
    """One day of rates for every series; `moved` maps a series to its own factor."""
    moved = moved or {}
    return [{"obs_date": d, "series": s, "rate": round(r * moved.get(s, factor), 6)} for s, r in BASE.items()]


def status_of(store, batch_id):
    return next(b["status"] for b in store.list_batches() if b["batch_id"] == batch_id)


def visible(store):
    return {(r["obs_date"], r["series"]): r["rate"] for r in store.current_rates(limit=1000)}


def test_a_first_load_is_published_and_visible(store):
    out = store.stage_and_publish("run-1", "ingest", day(D1) + day(D2))
    assert (out["outcome"], out["rows_new"], out["rows_changed"]) == ("published", 16, 0)
    assert len(visible(store)) == 16


def test_loading_the_same_rows_again_is_a_noop_and_leaves_no_trace(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", day(D1))
    assert (out["outcome"], out["batch_id"], out["rows_unchanged"]) == ("noop", None, 8)
    assert len(store.list_batches()) == 1
    assert store._one("SELECT count(*) AS n FROM fx_rates_log")["n"] == 8


def test_a_revised_value_becomes_the_visible_one_and_the_old_one_is_kept(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", [{"obs_date": D1, "series": "USD", "rate": 1.401}])
    assert (out["outcome"], out["rows_changed"]) == ("published", 1)
    assert visible(store)[(D1, "USD")] == 1.401
    assert store._one("SELECT count(*) AS n FROM fx_rates_log WHERE series = 'USD'")["n"] == 2


def test_a_batch_where_every_series_jumps_is_held_and_never_becomes_visible(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", day(D2, factor=1.10))
    assert out["outcome"] == "held"
    assert (out["audit"]["checked"], out["audit"]["suspicious"]) == (8, 8)
    assert set(visible(store)) == {(D1, s) for s in BASE}
    assert status_of(store, out["batch_id"]) == "held"


def test_one_odd_series_in_eight_is_tolerated(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", day(D2, moved={"USD": 1.2}))
    assert (out["outcome"], out["audit"]["suspicious"]) == ("published", 1)


def test_three_odd_series_in_eight_hold_the_batch(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", day(D2, moved={"USD": 1.2, "EUR": 1.2, "GBP": 1.2}))
    assert (out["outcome"], out["audit"]["suspicious"]) == ("held", 3)


def test_the_audit_follows_days_inside_the_same_batch(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    out = store.stage_and_publish("run-2", "ingest", day(D2) + day(D3, factor=1.10))
    assert out["outcome"] == "held"
    assert out["audit"]["checked"] == 16 and out["audit"]["suspicious"] == 8


def test_the_first_ever_single_day_has_nothing_to_compare_with_and_passes(store):
    out = store.stage_and_publish("run-1", "ingest", day(D1))
    assert (out["outcome"], out["audit"]["checked"]) == ("published", 0)


def test_a_held_batch_can_be_approved_once_and_then_it_is_visible(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    held = store.stage_and_publish("run-2", "ingest", day(D2, factor=1.10))["batch_id"]
    store.approve(held)
    assert len(visible(store)) == 16
    with pytest.raises(ValueError):
        store.approve(held)


def test_rolling_back_a_published_batch_brings_back_the_previous_values(store):
    store.stage_and_publish("run-1", "ingest", day(D1))
    bad = store.stage_and_publish("run-2", "ingest", [{"obs_date": D1, "series": "USD", "rate": 1.41}])["batch_id"]
    assert visible(store)[(D1, "USD")] == 1.41
    store.rollback(bad, "upstream restated by mistake")
    assert visible(store)[(D1, "USD")] == 1.40
    assert next(b for b in store.list_batches() if b["batch_id"] == bad)["reason"] == "upstream restated by mistake"
    with pytest.raises(ValueError):
        store.rollback(bad)
    with pytest.raises(LookupError):
        store.rollback("b-nope")
