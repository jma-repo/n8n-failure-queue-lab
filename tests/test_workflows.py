"""Structural checks on the generated n8n workflows. The behaviour itself is covered by tests/e2e."""

import json
import re

import pytest

from lab import stack
from lab.paths import N8N_DIR, ROOT
from lab.taxonomy import load_taxonomy
from lab.workflows.build import OUT_DIR, render

FILES = sorted(OUT_DIR.glob("*.json"))


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_the_committed_workflows_are_what_the_definitions_build():
    assert {p.name for p in FILES} == set(render()), "run: uv run python scripts/build_workflows.py"
    for name, text in render().items():
        assert (OUT_DIR / name).read_text(encoding="utf-8") == text, f"{name} is stale: run scripts/build_workflows.py"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_connections_point_at_real_nodes_and_names_are_unique(path):
    wf = load(path)
    names = [n["name"] for n in wf["nodes"]]
    assert len(names) == len(set(names))
    for src, outputs in wf["connections"].items():
        assert src in names
        for branch in outputs["main"]:
            for link in branch:
                assert link["node"] in names, f"{src} -> {link['node']}"
    ids = [n["id"] for n in wf["nodes"]]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_every_node_that_does_work_is_reachable_from_a_trigger(path):
    wf = load(path)
    triggers = [n["name"] for n in wf["nodes"] if n["type"].endswith(("Trigger", "webhook"))]
    assert triggers, "a workflow needs a trigger"
    seen, todo = set(triggers), list(triggers)
    while todo:
        for branch in wf["connections"].get(todo.pop(), {"main": []})["main"]:
            for link in branch:
                if link["node"] not in seen:
                    seen.add(link["node"])
                    todo.append(link["node"])
    idle = [n["name"] for n in wf["nodes"] if n["name"] not in seen and not n["type"].endswith("stickyNote")]
    assert idle == []


def test_the_three_working_workflows_report_their_failures_to_the_error_handler():
    for name in ("labIngest01", "labRetry01", "labMonitor01"):
        assert load(OUT_DIR / f"{name}.json")["settings"]["errorWorkflow"] == "labError01"
    assert "errorWorkflow" not in load(OUT_DIR / "labError01.json")["settings"]


def test_calls_to_the_queue_api_carry_the_key_and_a_timeout():
    for path in FILES:
        for node in load(path)["nodes"]:
            if node["type"].endswith("httpRequest") and "LAB_QUEUE_URL" in node["parameters"]["url"]:
                headers = node["parameters"]["headerParameters"]["parameters"]
                assert {"name": "X-API-Key", "value": "={{ $env.LAB_API_TOKEN }}"} in headers, node["name"]
                assert node["parameters"]["options"]["timeout"] <= 15000, node["name"]


def test_the_source_fetch_retries_in_the_flow_and_never_throws_on_a_status_code():
    for name in ("labIngest01", "labRetry01"):
        fetch = next(n for n in load(OUT_DIR / f"{name}.json")["nodes"] if n["name"] == "Fetch source")
        assert (fetch["retryOnFail"], fetch["maxTries"], fetch["onError"]) == (True, 3, "continueRegularOutput")
        response = fetch["parameters"]["options"]["response"]["response"]
        assert response["neverError"] is True and response["fullResponse"] is True
        assert fetch["parameters"]["options"]["timeout"] == 10000


def test_alerts_fail_open_so_a_down_queue_api_cannot_swallow_them():
    for path in FILES:
        nodes = {n["name"]: n for n in load(path)["nodes"]}
        if "Throttle alert" in nodes:
            assert nodes["Throttle alert"]["onError"] == "continueRegularOutput"
            gate = nodes["Send now?"]["parameters"]["conditions"]["conditions"][0]["leftValue"]
            assert "!== false" in gate


def test_webhook_paths_match_the_command_line_and_are_unique():
    paths = [n["parameters"]["path"] for p in FILES for n in load(p)["nodes"] if n["type"].endswith("webhook")]
    assert sorted(paths) == sorted(stack.HOOKS.values())


def test_the_workflows_only_use_the_smtp_credential_and_hold_no_secret():
    token = stack.read_env().get("LAB_API_TOKEN") if (ROOT / ".env").exists() else None
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        assert token is None or token not in text
        for node in load(path)["nodes"]:
            assert set(node.get("credentials", {})) <= {"smtp"}
    assert stack.SMTP_CREDENTIAL[0]["data"]["password"] == ""


def test_every_error_type_the_javascript_can_raise_exists_in_the_taxonomy():
    source = (N8N_DIR / "code" / "validate.js").read_text(encoding="utf-8")
    raised = set(re.findall(r'(?:failure|dataset|bad)\("([a-z_0-9]+)"', source)) | set(re.findall(r'error_type: "([a-z_0-9]+)"', source))
    assert raised and raised <= set(load_taxonomy()), raised - set(load_taxonomy())
