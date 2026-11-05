"""Retry delays: exponential growth with a cap and optional jitter.

Without jitter every failure that started together retries together, so a recovering upstream gets hit by the whole
backlog at once. Equal jitter keeps half of the exponential delay and randomizes the other half.
"""

from __future__ import annotations

import random


def next_delay(
    attempt: int,
    base_s: float,
    cap_s: float,
    *,
    jitter: str = "equal",
    rng: random.Random | None = None,
    floor_s: float = 0.0,
) -> float:
    """Seconds to wait before retry number attempt (0 is the first retry). floor_s honours a Retry-After header."""
    ceiling = min(cap_s, base_s * (2**attempt))
    rng = rng or random
    if jitter == "none":
        delay = ceiling
    elif jitter == "full":
        delay = rng.uniform(0.0, ceiling)
    elif jitter == "equal":
        delay = ceiling / 2 + rng.uniform(0.0, ceiling / 2)
    else:
        raise ValueError(f"unknown jitter mode {jitter}")
    return max(delay, floor_s)
