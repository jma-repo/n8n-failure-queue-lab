from __future__ import annotations

import random
from datetime import datetime, timedelta

import pytest

from lab.queue_api.store import Policy, Store

T0 = datetime(2026, 11, 5, 12, 0, 0)


class Clock:
    """A clock the test moves by hand, so retry schedules can be checked to the second."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(clock: Clock) -> Store:
    return Store(":memory:", policy=Policy(jitter="none"), clock=clock, rng=random.Random(7))
