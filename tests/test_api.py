import pytest
from fastapi.testclient import TestClient

from lab.queue_api.app import create_app

H = {"X-API-Key": "secret"}


@pytest.fixture
def client(store):
    return TestClient(create_app(store, token="secret", allow_admin=True))


def failure(**extra):
    return {"error_type": "http_5xx", "scope": "source", "detail": "503", "http_status": 503, **extra}


def test_every_endpoint_but_healthz_needs_the_key(client):
    assert client.get("/healthz").status_code == 200
    assert client.get("/v1/runs").status_code == 401
    assert client.get("/v1/runs", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/v1/runs", headers=H).status_code == 200


def test_a_failure_goes_through_enqueue_claim_and_result(client, clock):
    out = client.post("/v1/failures", headers=H, json={"run_id": "r1", "failures": [failure()]}).json()
    fid = out["created"][0]["failure_id"]
    assert out["created"][0]["next_retry_at"].endswith("Z")
    assert client.post("/v1/failures/claim", headers=H, json={}).json()["items"] == []
    clock.advance(seconds=61)
    items = client.post("/v1/failures/claim", headers=H, json={"limit": 10}).json()["items"]
    assert [i["failure_id"] for i in items] == [fid]
    body = {"run_id": "r2", "results": [{"failure_id": fid, "ok": True}]}
    assert client.post("/v1/failures/results", headers=H, json=body).json()["resolved"] == [fid]
    assert client.get("/v1/failures?status=resolved", headers=H).json()["items"][0]["failure_id"] == fid


def test_an_unknown_error_type_is_rejected_with_a_message(client):
    r = client.post("/v1/failures", headers=H, json={"run_id": "r1", "failures": [failure(error_type="made_up")]})
    assert r.status_code == 422 and "made_up" in r.json()["detail"]
    assert client.get("/v1/failures", headers=H).json()["items"] == []


def test_requeue_answers_404_for_unknown_and_409_for_a_failure_that_is_not_dead(client):
    fid = client.post("/v1/failures", headers=H, json={"run_id": "r", "failures": [failure()]}).json()["created"][0]["failure_id"]
    assert client.post("/v1/failures/nope/requeue", headers=H).status_code == 404
    assert client.post(f"/v1/failures/{fid}/requeue", headers=H).status_code == 409


def test_rows_are_published_then_a_repeat_is_a_noop(client):
    rows = [{"obs_date": "2026-11-04", "series": "FXUSDCAD", "rate": 1.3961}]
    first = client.post("/v1/batches", headers=H, json={"run_id": "r1", "rows": rows}).json()
    assert first["outcome"] == "published" and first["rows_new"] == 1
    assert client.post("/v1/batches", headers=H, json={"run_id": "r2", "rows": rows}).json()["outcome"] == "noop"
    assert client.get("/v1/rates/current", headers=H).json()["items"][0]["obs_date"] == "2026-11-04"
    assert client.post(f"/v1/batches/{first['batch_id']}/approve", headers=H).status_code == 409
    assert client.post(f"/v1/batches/{first['batch_id']}/rollback", headers=H, json={"reason": "x"}).status_code == 200
    assert client.get("/v1/rates/current", headers=H).json()["items"] == []
    assert client.post("/v1/batches/b-nope/rollback", headers=H).status_code == 404


def test_a_run_reported_the_way_n8n_reports_it_is_stored_in_utc(client):
    run = {"run_id": "r1", "workflow": "ingest", "trigger_kind": "schedule", "status": "partial", "rows_in": 40,
           "rows_ok": 36, "rows_failed": 4, "started_at": "2026-11-05T12:00:00.123Z", "finished_at": "2026-11-05T12:00:02Z"}  # fmt: skip
    assert client.post("/v1/runs", headers=H, json=run).json() == {"run_id": "r1", "status": "partial"}
    (row,) = client.get("/v1/runs", headers=H).json()["items"]
    assert row["started_at"] == "2026-11-05T12:00:00Z" and row["rows_failed"] == 4
    assert client.post("/v1/runs", headers=H, json={**run, "status": "exploded"}).status_code == 422


def test_alerts_say_whether_to_send(client):
    alert = {"key": "dead:http_5xx", "severity": "critical", "subject": "1 dead", "body": "details"}
    assert client.post("/v1/alerts", headers=H, json=alert).json()["send"] is True
    assert client.post("/v1/alerts", headers=H, json=alert).json()["send"] is False
    assert client.get("/v1/health/summary", headers=H).json()["queue"]["dead"] == 0


def test_admin_endpoints_exist_only_when_allowed(store):
    closed = TestClient(create_app(store, token="secret", allow_admin=False))
    assert closed.post("/admin/reset", headers=H).status_code == 404
    opened = TestClient(create_app(store, token="secret", allow_admin=True))
    assert opened.post("/admin/policy", headers=H, json={"time_scale": 0.5}).json()["time_scale"] == 0.5
    assert opened.post("/admin/reset", headers=H).json() == {"status": "empty"}


def test_a_simulated_outage_answers_503_on_v1_only_and_ends_on_time(store, clock):
    client = TestClient(create_app(store, token="secret", allow_admin=True))
    assert client.post("/admin/outage", headers=H, json={"seconds": 30}).status_code == 200
    assert client.get("/v1/runs", headers=H).status_code == 503
    assert client.get("/healthz").status_code == 200
    assert client.post("/admin/policy", headers=H, json={"jitter": "none"}).status_code == 200
    clock.advance(seconds=31)
    assert client.get("/v1/runs", headers=H).status_code == 200
    client.post("/admin/outage", headers=H, json={"seconds": 30})
    assert client.delete("/admin/outage", headers=H).status_code == 200
    assert client.get("/v1/runs", headers=H).status_code == 200


def test_a_held_ingest_batch_queues_one_audit_failure_however_often_it_repeats(store):
    store.stage_and_publish("r0", "ingest", [{"obs_date": "2026-11-02", "series": "FXUSDCAD", "rate": 1.4}])
    held = store.stage_and_publish("r1", "ingest", [{"obs_date": "2026-11-03", "series": "FXUSDCAD", "rate": 1.7}])
    assert held["outcome"] == "held" and held["queued"]["created"][0]["error_type"] == "audit_jump"
    again = store.stage_and_publish("r2", "ingest", [{"obs_date": "2026-11-03", "series": "FXUSDCAD", "rate": 1.71}])
    assert again["outcome"] == "held" and again["queued"]["repeated"] == 1
    retried = store.stage_and_publish("r3", "retry", [{"obs_date": "2026-11-03", "series": "FXUSDCAD", "rate": 1.72}])
    assert retried["outcome"] == "held" and retried["queued"] is None
    assert [f["error_type"] for f in store.list_failures()] == ["audit_jump"]


def test_operators_can_run_one_select_and_nothing_else(store, client):
    store.enqueue("r1", [{"error_type": "http_5xx", "scope": "source"}])
    ok = client.post("/admin/query", headers=H, json={"sql": "SELECT error_type, status FROM failure_queue;"})
    assert ok.status_code == 200 and ok.json() == {"columns": ["error_type", "status"], "rows": [["http_5xx", "pending"]]}
    cte = client.post("/admin/query", headers=H, json={"sql": "WITH x AS (SELECT 1 AS n) SELECT n FROM x", "limit": 5})
    assert cte.json()["rows"] == [[1]]
    for bad in ["DELETE FROM failure_queue", "DROP TABLE runs", "SELECT 1; DELETE FROM runs", "INSERT INTO runs VALUES (1)", "", "SELECT * FROM nope"]:
        assert client.post("/admin/query", headers=H, json={"sql": bad}).status_code == 400, bad
    assert len(store.list_failures()) == 1


def test_sql_cannot_read_files_from_the_server(client):
    r = client.post("/admin/query", headers=H, json={"sql": "SELECT * FROM read_text('/etc/passwd')"})
    assert r.status_code == 400
    assert client.post("/admin/query", headers=H, json={"sql": "SELECT * FROM glob('*')"}).status_code == 400
