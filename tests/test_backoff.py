import random

import pytest

from lab.queue_api.backoff import next_delay


def test_delay_doubles_until_the_cap():
    delays = [next_delay(n, 60, 3600, jitter="none") for n in range(8)]
    assert delays == [60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_equal_jitter_keeps_half_the_delay_and_randomizes_the_rest():
    rng = random.Random(1)
    draws = [next_delay(3, 60, 3600, jitter="equal", rng=rng) for _ in range(500)]
    assert all(240 <= d <= 480 for d in draws)
    assert max(draws) - min(draws) > 150


def test_full_jitter_ranges_from_zero_to_the_exponential_delay():
    rng = random.Random(2)
    draws = [next_delay(2, 60, 3600, jitter="full", rng=rng) for _ in range(500)]
    assert all(0 <= d <= 240 for d in draws)
    assert min(draws) < 30 and max(draws) > 210


def test_retry_after_is_a_floor_not_a_replacement():
    assert next_delay(0, 60, 3600, jitter="none", floor_s=500) == 500
    assert next_delay(5, 60, 3600, jitter="none", floor_s=500) == 1920


def test_failures_that_start_together_stop_retrying_together_only_with_jitter():
    rng = random.Random(3)
    plain = {next_delay(2, 60, 3600, jitter="none", rng=rng) for _ in range(100)}
    spread = {round(next_delay(2, 60, 3600, jitter="equal", rng=rng)) for _ in range(100)}
    assert len(plain) == 1
    assert len(spread) > 50


def test_unknown_jitter_mode_is_rejected():
    with pytest.raises(ValueError):
        next_delay(0, 60, 3600, jitter="sometimes")
