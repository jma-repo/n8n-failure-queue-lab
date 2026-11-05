"""The bootstrap sequence, checked with a fake docker: the real thing needs the stack (see tests/e2e)."""

import pytest

from lab import stack


class Reply:
    status_code = 200
    text = ""


def test_bootstrap_creates_the_owner_then_imports_publishes_and_restarts_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(stack, "read_env", lambda: {"N8N_OWNER_EMAIL": "owner@lab.test", "N8N_OWNER_PASSWORD": "Pw-1a-test"})
    monkeypatch.setattr(stack, "container", lambda service: f"cid-{service}")
    monkeypatch.setattr(stack, "docker", lambda *args, **kw: calls.append(("docker", *args)))
    monkeypatch.setattr(stack, "n8n_exec", lambda *args: calls.append(("n8n", *args)) or "")
    monkeypatch.setattr(stack, "wait_until", lambda check, **kw: calls.append(("wait", kw.get("what"))) or True)
    monkeypatch.setattr(stack.httpx, "post", lambda url, **kw: calls.append(("post", url.rsplit("/", 2)[-2:])) or Reply())
    stack.bootstrap()
    kinds = [c[0] for c in calls]
    assert kinds[:2] == ["wait", "post"], "n8n must be answering before the owner is created"
    assert ("docker", "exec", "-u", "root", "cid-n8n", "rm", "-rf", "/tmp/workflows") in calls
    imports = [c for c in calls if c[0] == "n8n"]
    assert [c[1] for c in imports] == ["import:credentials", "import:workflow", "publish:workflow", "publish:workflow", "publish:workflow", "publish:workflow"]
    assert [c[2] for c in imports[2:]] == ["--id=labError01", "--id=labIngest01", "--id=labRetry01", "--id=labMonitor01"]
    assert calls.index(("docker", "restart", "cid-n8n")) > calls.index(imports[-1])


def test_bootstrap_stops_if_the_owner_cannot_be_created(monkeypatch):
    class Bad(Reply):
        status_code, text = 500, "boom"

    monkeypatch.setattr(stack, "read_env", lambda: {"N8N_OWNER_EMAIL": "o@lab.test", "N8N_OWNER_PASSWORD": "x"})
    monkeypatch.setattr(stack, "wait_until", lambda check, **kw: True)
    monkeypatch.setattr(stack.httpx, "post", lambda url, **kw: Bad())
    with pytest.raises(SystemExit, match="owner setup failed"):
        stack.bootstrap()


def test_wait_until_names_what_never_happened():
    with pytest.raises(TimeoutError, match="waiting for the moon"):
        stack.wait_until(lambda: False, timeout=0.05, interval=0.01, what="the moon")
    assert stack.wait_until(lambda: "yes", timeout=1) == "yes"
