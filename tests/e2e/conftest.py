"""End-to-end tests need the compose stack: `uv run lab up`, then `uv run lab e2e`. Without LAB_E2E=1 they are skipped."""

from __future__ import annotations

import os

import pytest

from lab import stack

SCALE = 0.005  # retry delays and alert cool-downs shrink 200x, so a 30-minute backoff takes seconds


def pytest_collection_modifyitems(config, items):
    if os.environ.get("LAB_E2E") == "1":
        return
    skip = pytest.mark.skip(reason="needs the compose stack: run `uv run lab up`, then `uv run lab e2e`")
    for item in items:
        if "e2e" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def lab() -> stack.Lab:
    return stack.Lab()


@pytest.fixture(autouse=True)
def clean(request, lab):
    if "e2e" not in request.keywords or os.environ.get("LAB_E2E") != "1":
        yield
        return
    lab.api.delete("/admin/outage")
    lab.reset(time_scale=SCALE, jitter="none")
    yield
    lab.sim.delete("/admin/scenario")
    lab.api.delete("/admin/outage")
