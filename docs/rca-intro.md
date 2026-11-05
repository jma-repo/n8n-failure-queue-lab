# SQL root-cause analysis

Eight queries (CTEs, window functions, gaps-and-islands, ROLLUP) that turn the failure ledger into a post-mortem: which errors
dominate, when they happen, how long incidents last, whether retrying helps, and whether the retry policy is followed.

**The history is simulated.** `uv run lab history` replays two weeks of operations (seed 7) through the same store the live
stack uses, so queueing, backoff, dead-lettering, audits and alert cool-downs are the real code. Only the world is scripted:
which request meets which fault, and when a person acts. {{facts}}

The queries never read the timeline below. `tests/test_rca.py` checks that they find it blind.

**Scripted timeline (ground truth, UTC)**

{{timeline}}

Reproduce this page with `uv run lab history && uv run lab rca --markdown docs/rca-report.md`. All times are UTC.
