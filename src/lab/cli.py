"""`uv run lab <command>`: set up, drive and check the lab. Run `uv run lab --help` for the list."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys

from lab import stack
from lab.paths import ROOT


def cmd_init(args: argparse.Namespace) -> int:
    """Write .env with fresh random secrets (never printed). Re-running keeps the file unless --force."""
    env = ROOT / ".env"
    if env.exists() and not args.force:
        print(f"{env.name} already exists; use --force to replace it")
        return 0
    values = {
        "N8N_ENCRYPTION_KEY": secrets.token_hex(16),
        "LAB_API_TOKEN": secrets.token_urlsafe(24),
        "N8N_OWNER_EMAIL": "owner@lab.test",
        "N8N_OWNER_PASSWORD": "Lab-" + secrets.token_urlsafe(12) + "-1a",  # n8n wants 8+ characters with a capital and a digit
        "LAB_SOURCE_BASE": "http://source-sim:8080",
        "LAB_TIME_SCALE": "1",
    }
    env.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8", newline="\n")
    print(f"wrote {env.name} with new secrets")
    return 0


def cmd_up(args: argparse.Namespace) -> int:
    """Build and start the four containers, then set n8n up (owner, credentials, workflows) unless --no-bootstrap."""
    stack.up()
    if not args.no_bootstrap:
        stack.bootstrap()
        print("n8n is ready: http://localhost:5678  |  alert inbox: http://localhost:8025  |  queue API docs: http://localhost:8000/docs")
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    """Stop the stack; --volumes also forgets n8n and the DuckDB warehouse."""
    stack.down(volumes=args.volumes)
    return 0


def cmd_bootstrap(_: argparse.Namespace) -> int:
    """Re-import the workflows into a running n8n (after changing n8n/code or src/lab/workflows)."""
    stack.bootstrap()
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Empty the warehouse, clear the fault and the alert inbox. --fast shrinks retry delays 100x for demos."""
    stack.Lab().reset(time_scale=0.01 if args.fast else 1.0, jitter="none" if args.fast else "equal")
    return 0


def cmd_chaos(args: argparse.Namespace) -> int:
    """Switch the stand-in source to a fault, e.g. `lab chaos http_500 --first-n 2` or `lab chaos ok`."""
    params = dict(p.split("=", 1) for p in args.param)
    params = {k: (float(v) if v.replace(".", "", 1).isdigit() else v) for k, v in params.items()}
    if args.first_n:
        params["first_n"] = args.first_n
    print(json.dumps(stack.Lab().scenario(args.name, p=args.p, ttl_s=args.ttl, **params)))
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Run a workflow once, now, through its webhook."""
    lab = stack.Lab()
    run_id = lab.trigger(args.workflow)
    print(f"started {args.workflow} run {run_id}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    """What the lab knows right now: health, open failures, held batches, recent alerts."""
    lab = stack.Lab()
    print(json.dumps({"scenario": lab.sim.get("/admin/scenario").json(), "health": lab.health()}, indent=2))
    for f in lab.failures():
        if f["status"] in ("pending", "in_progress", "dead"):
            print(f"  {f['status']:<11} {f['error_type']:<18} {f['scope']:<24} attempts {f['attempts']}/{f['max_attempts']}  {f['error_detail'] or ''}")
    for b in lab.batches():
        if b["status"] == "held":
            print(f"  held batch {b['batch_id']}: {b['reason']}")
    return 0


def cmd_sql(args: argparse.Namespace) -> int:
    """Run one read-only SELECT against the live warehouse, e.g. lab sql "SELECT * FROM failure_queue"."""
    r = stack.Lab().api.post("/admin/query", json={"sql": args.query, "limit": args.limit})
    if r.status_code != 200:
        print(r.json().get("detail", r.text))
        return 1
    from lab.rca import Result, to_text

    body = r.json()
    print(to_text(Result("sql", "", args.query, "", "", args.query, body["columns"], [tuple(x) for x in body["rows"]])))
    return 0


def cmd_requeue(args: argparse.Namespace) -> int:
    """Give dead failures a fresh retry budget after the cause is fixed: one id, or --dead for all of them."""
    lab = stack.Lab()
    ids = [f["failure_id"] for f in lab.failures("dead")] if args.dead else args.failure_ids
    for failure_id in ids:
        r = lab.api.post(f"/v1/failures/{failure_id}/requeue")
        print(failure_id, "requeued" if r.status_code == 200 else r.json().get("detail", r.text))
    return 0 if ids else 1


def cmd_approve(args: argparse.Namespace) -> int:
    """Publish a batch the audit held, after checking its numbers against the source."""
    r = stack.Lab().api.post(f"/v1/batches/{args.batch_id}/approve")
    print(r.json().get("status") or r.json().get("detail"))
    return 0 if r.status_code == 200 else 1


def cmd_rollback(args: argparse.Namespace) -> int:
    """Hide a published batch; the values it replaced become visible again."""
    r = stack.Lab().api.post(f"/v1/batches/{args.batch_id}/rollback", json={"reason": args.reason})
    print(r.json().get("status") or r.json().get("detail"))
    return 0 if r.status_code == 200 else 1


def cmd_history(args: argparse.Namespace) -> int:
    """Simulate two weeks of operations (seeded) into a DuckDB file for the RCA queries."""
    from lab.history.simulate import simulate

    out = ROOT / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    store, counters = simulate(str(out), seed=args.seed)
    store.con.close()
    print(f"wrote {out.relative_to(ROOT)}: {counters}")
    return 0


def cmd_rca(args: argparse.Namespace) -> int:
    """Run the SQL root-cause queries in sql/rca on the simulated history (or on --db) and print the tables."""
    import duckdb

    from lab import rca

    db = ROOT / args.db
    if not db.exists():
        print(f"{args.db} does not exist yet: run `uv run lab history` first")
        return 1
    results = rca.run_all(duckdb.connect(str(db)), only=args.only)
    for result in results:
        print(rca.to_text(result), end=chr(10) * 2)
    if args.markdown:
        from lab.history.incidents import INCIDENTS

        con = duckdb.connect(str(db))
        counts = dict(con.execute("SELECT workflow, count(*) FROM runs GROUP BY 1").fetchall())
        events = con.execute("SELECT count(*) FROM failure_events").fetchone()[0]
        facts = f"The database holds {counts.get('ingest', 0):,} ingest runs, {counts.get('retry', 0):,} retry runs and {events:,} ledger events."
        rows = ["| window | what happens | fault |", "|---|---|---|"]
        for i in INCIDENTS:
            when = f"every day {i.start:%H:%M} to {i.end:%H:%M}" if i.daily else f"{i.start:%a %d %b %H:%M} to {i.end:%a %d %b %H:%M}"
            rows.append(f"| {when} | {i.why} | `{i.fault}` |")
        timeline = chr(10).join(rows)
        intro = (ROOT / "docs" / "rca-intro.md").read_text(encoding="utf-8").replace("{{facts}}", facts).replace("{{timeline}}", timeline)
        (ROOT / args.markdown).write_text(rca.to_markdown(results, intro), encoding="utf-8", newline=chr(10))
        print(f"wrote {args.markdown}")
    return 0


def cmd_e2e(args: argparse.Namespace) -> int:
    """Run the end-to-end tests against the running stack (about four minutes)."""
    env = {**os.environ, "LAB_E2E": "1"}
    return subprocess.call([sys.executable, "-m", "pytest", "-m", "e2e", "-v", *args.pytest_args], cwd=ROOT, env=env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lab", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, **kw):
        p = sub.add_parser(name, help=func.__doc__, description=func.__doc__)
        p.set_defaults(func=func)
        return p

    add("init", cmd_init).add_argument("--force", action="store_true")
    add("up", cmd_up).add_argument("--no-bootstrap", action="store_true")
    add("down", cmd_down).add_argument("--volumes", action="store_true")
    add("bootstrap", cmd_bootstrap)
    add("reset", cmd_reset).add_argument("--fast", action="store_true")
    chaos = add("chaos", cmd_chaos)
    chaos.add_argument("name")
    chaos.add_argument("--p", type=float, default=1.0, help="share of requests hit (default 1.0)")
    chaos.add_argument("--first-n", type=int, help="only the first N requests are hit")
    chaos.add_argument("--ttl", type=float, help="go back to ok after this many seconds")
    chaos.add_argument("--param", action="append", default=[], metavar="K=V", help="scenario parameter, repeatable")
    add("trigger", cmd_trigger).add_argument("workflow", choices=sorted(stack.HOOKS))
    add("status", cmd_status)
    requeue = add("requeue", cmd_requeue)
    requeue.add_argument("failure_ids", nargs="*")
    requeue.add_argument("--dead", action="store_true", help="every dead failure")
    add("approve", cmd_approve).add_argument("batch_id")
    rollback = add("rollback", cmd_rollback)
    rollback.add_argument("batch_id")
    rollback.add_argument("--reason", default="rolled back by an operator")
    sql_cmd = add("sql", cmd_sql)
    sql_cmd.add_argument("query")
    sql_cmd.add_argument("--limit", type=int, default=200)
    history = add("history", cmd_history)
    history.add_argument("--out", default="data/history.duckdb")
    history.add_argument("--seed", type=int, default=7)
    rca_cmd = add("rca", cmd_rca)
    rca_cmd.add_argument("--db", default="data/history.duckdb")
    rca_cmd.add_argument("--only", help="run one query by its number, e.g. 04")
    rca_cmd.add_argument("--markdown", help="also write the report, e.g. docs/rca-report.md")
    add("e2e", cmd_e2e).add_argument("pytest_args", nargs="*")
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
