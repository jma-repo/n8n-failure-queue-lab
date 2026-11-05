"""Drives the docker compose stack: start it, bootstrap n8n (owner, credentials, workflows), and poke the services."""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import httpx

from lab.paths import N8N_DIR, ROOT

N8N = "http://localhost:5678"
API = "http://localhost:8000"
SIM = "http://localhost:8080"
MAIL = "http://localhost:8025"
WORKFLOW_IDS = ["labError01", "labIngest01", "labRetry01", "labMonitor01"]  # the error handler first
HOOKS = {"ingest": "lab-ingest", "retry": "lab-retry", "monitor": "lab-monitor"}
SMTP_CREDENTIAL = [
    {
        "id": "labSmtp01",
        "name": "Mailpit SMTP",
        "type": "smtp",
        "data": {"host": "mailpit", "port": 1025, "secure": False, "disableStartTls": True, "user": "", "password": "", "hostName": ""},
    }
]


def read_env() -> dict[str, str]:
    path = ROOT / ".env"
    if not path.exists():
        raise SystemExit("no .env yet: run `uv run lab init` first")
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines() if "=" in line)


def compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "compose", *args], cwd=ROOT, check=check, text=True, capture_output=capture)


def wait_until(check, *, timeout: float, interval: float = 1.0, what: str = "condition"):
    """Poll check() until it returns something truthy; raise with the name of what never happened."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


def up() -> None:
    compose("up", "-d", "--build", "--wait")


def down(volumes: bool = False) -> None:
    compose("down", *(["-v"] if volumes else []))


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], cwd=ROOT, check=check, text=True, capture_output=True)


def container(service: str) -> str:
    """Container id of a compose service, found by label so it does not depend on the compose plugin or on naming."""
    project = "label=com.docker.compose.project=n8n-failure-queue-lab"
    ids = docker("ps", "-aq", "--filter", project, "--filter", f"label=com.docker.compose.service={service}").stdout.split()
    if not ids:
        raise SystemExit(f"service {service} has no container: run `uv run lab up` first")
    return ids[0]


def n8n_exec(*args: str) -> str:
    out = docker("exec", container("n8n"), "n8n", *args)
    return chr(10).join(line for line in out.stdout.splitlines() if "license SDK" not in line)


def bootstrap() -> None:
    """Idempotent: create the n8n owner, import the SMTP credential and the workflows, publish them, restart n8n."""
    env = read_env()
    wait_until(lambda: _ok(f"{N8N}/healthz"), timeout=180, what="n8n to answer /healthz")
    owner = {"email": env["N8N_OWNER_EMAIL"], "firstName": "Lab", "lastName": "Owner", "password": env["N8N_OWNER_PASSWORD"]}
    r = httpx.post(f"{N8N}/rest/owner/setup", json=owner, timeout=30)
    if r.status_code not in (200, 400):
        raise SystemExit(f"n8n owner setup failed: HTTP {r.status_code} {r.text[:200]}")
    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    (out / "credentials.json").write_text(json.dumps(SMTP_CREDENTIAL, indent=2), encoding="utf-8", newline="\n")
    cid = container("n8n")
    docker("exec", "-u", "root", cid, "rm", "-rf", "/tmp/workflows")  # docker cp leaves root-owned files
    docker("cp", str(out / "credentials.json"), f"{cid}:/tmp/credentials.json")
    docker("cp", str(N8N_DIR / "workflows"), f"{cid}:/tmp/workflows")
    n8n_exec("import:credentials", "--input=/tmp/credentials.json")
    n8n_exec("import:workflow", "--separate", "--input=/tmp/workflows")
    for wid in WORKFLOW_IDS:
        n8n_exec("publish:workflow", f"--id={wid}")
    docker("restart", cid)
    wait_until(lambda: _ok(f"{N8N}/healthz"), timeout=180, what="n8n to restart")
    # the webhooks register a moment after /healthz; the monitor webhook is harmless to call
    wait_until(lambda: httpx.post(f"{N8N}/webhook/{HOOKS['monitor']}", json={}, timeout=30).status_code != 404, timeout=120, what="workflows to activate")


def _ok(url: str) -> bool:
    try:
        return httpx.get(url, timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


class Lab:
    """Thin clients for the four services, used by the CLI and the end-to-end tests."""

    def __init__(self) -> None:
        env = read_env()
        self.api = httpx.Client(base_url=API, headers={"X-API-Key": env["LAB_API_TOKEN"]}, timeout=60)
        self.sim = httpx.Client(base_url=SIM, timeout=60)
        self.n8n = httpx.Client(base_url=N8N, timeout=60)
        self.mail = httpx.Client(base_url=MAIL, timeout=30)

    def reset(self, time_scale: float = 1.0, jitter: str = "equal") -> None:
        self.api.post("/admin/reset").raise_for_status()
        self.api.post("/admin/policy", json={"time_scale": time_scale, "jitter": jitter}).raise_for_status()
        self.sim.delete("/admin/scenario")
        self.mail.delete("/api/v1/messages")

    def scenario(self, name: str, *, p: float = 1.0, ttl_s: float | None = None, **params: Any) -> dict[str, Any]:
        r = self.sim.post("/admin/scenario", json={"name": name, "p": p, "ttl_s": ttl_s, "params": params})
        r.raise_for_status()
        return r.json()

    def trigger(self, workflow: str, run_id: str | None = None) -> str:
        run_id = run_id or f"{workflow}-{int(time.time() * 1000)}"
        r = self.n8n.post(f"/webhook/{HOOKS[workflow]}", json={"run_id": run_id})
        r.raise_for_status()
        return run_id

    def run(self, run_id: str) -> dict[str, Any] | None:
        return next((r for r in self.api.get("/v1/runs", params={"limit": 200}).json()["items"] if r["run_id"] == run_id), None)

    def run_ingest(self, timeout: float = 60) -> dict[str, Any]:
        """Trigger one ingest and wait until its run record exists."""
        run_id = self.trigger("ingest")
        return wait_until(lambda: self.run(run_id), timeout=timeout, what=f"run {run_id} to be logged")

    def drive_retries(self, done, timeout: float = 60):
        """Fire the retry worker every 1.5 seconds until done() is truthy, the way its schedule would."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.trigger("retry")
            time.sleep(1.5)
            value = done()
            if value:
                return value
        raise TimeoutError("the retry worker did not get there in time")

    def failures(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.api.get("/v1/failures", params={"status": status, "limit": 500} if status else {"limit": 500}).json()["items"]

    def alerts(self) -> list[dict[str, Any]]:
        return self.api.get("/v1/alerts", params={"limit": 200}).json()["items"]

    def rates(self) -> list[dict[str, Any]]:
        return self.api.get("/v1/rates/current", params={"limit": 1000}).json()["items"]

    def batches(self) -> list[dict[str, Any]]:
        return self.api.get("/v1/batches", params={"limit": 100}).json()["items"]

    def mails(self) -> list[dict[str, Any]]:
        return self.mail.get("/api/v1/messages").json().get("messages", [])

    def health(self) -> dict[str, Any]:
        return self.api.get("/v1/health/summary").json()
