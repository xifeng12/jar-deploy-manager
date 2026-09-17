import ast
import importlib
import io
import os
from pathlib import Path
from types import SimpleNamespace
from ipaddress import ip_address

import pytest
from flask import Flask, jsonify, request

from jar_names import normalize_jar_name, normalize_jar_names


class ImmediateThread:
    ident = 17

    def __init__(self, target):
        self.target = target

    def start(self):
        self.target()


@pytest.fixture
def api(tmp_path):
    source = ast.parse(Path(__file__).with_name("app.py").read_text(encoding="utf-8"))
    functions = ast.Module(body=[node for node in source.body if isinstance(node, ast.FunctionDef)], type_ignores=[])
    servers = [{"ip": "192.0.2.1", "jars": ["land-es7-biz.jar"]}]
    calls = []
    released = []
    events = []
    state = {"operation_id": None, "operation": None, "status": "idle", "success": 0, "fail": 0, "skip": 0}

    def reserve(operation):
        if state["status"] == "running":
            return None
        state.update(operation_id="op-test", operation=operation, status="running")
        return state["operation_id"]

    def operation(*args, operation_id=None):
        calls.append((args, operation_id))
        state.update(status="failed", success=0, fail=1)
        return dict(state)

    def validate(_servers, items):
        if any(item != {"server": "192.0.2.1", "jar": "land-es7-biz.jar"} for item in items):
            raise ValueError("JAR不属于目标服务器")

    def plan(_servers, selected, jars):
        validate(_servers, [{"server": ip, "jar": jar} for ip in selected for jar in jars])
        return {ip: list(jars) for ip in selected}

    service = SimpleNamespace(
        reserve_operation=reserve,
        release_reservation=lambda operation_id: released.append(operation_id),
        get_operation_status=lambda: dict(state),
        deploy=operation,
        restart=operation,
        rollback=operation,
        cancel_deploy=lambda: calls.append("cancel"),
        test_connection=lambda _ip: {"success": False, "message": "SSH认证失败", "steps": []},
        _extract_core=lambda name: normalize_jar_name(name).lower(),
        last_deploy_success=0,
        last_deploy_fail=0,
    )
    app = Flask(__name__)
    namespace = {
        "app": app, "os": os, "jsonify": jsonify, "request": request,
        "ip_address": ip_address, "BASE_DIR": str(tmp_path),
        "normalize_jar_name": normalize_jar_name, "normalize_jar_names": normalize_jar_names,
        "threading": SimpleNamespace(Thread=ImmediateThread),
        "deploy_service": service,
        "socketio": SimpleNamespace(emit=lambda event, payload: events.append((event, payload))),
        "get_all_servers": lambda: servers,
        "get_deploy_history": lambda: [{"id": 4, "status": "failed", "success_count": 0, "fail_count": 1}],
        "validate_restart_items": validate, "build_jar_plan": plan,
    }
    exec(compile(functions, "app.py", "exec"), namespace)
    (tmp_path / "jars").mkdir()
    (tmp_path / "jars" / "land-es7-biz.jar").write_bytes(b"original")
    return SimpleNamespace(client=app.test_client(), namespace=namespace, service=service, servers=servers,
                           calls=calls, released=released, events=events, state=state, home=tmp_path)


def test_history_route_reads_persisted_records(api):
    response = api.client.get("/api/deploy/history")
    assert response.status_code == 200
    assert response.get_json() == [{"id": 4, "status": "failed", "success_count": 0, "fail_count": 1}]


def test_status_route_exposes_operation_identity(api):
    api.state.update(operation_id="another-client", operation="restart", status="running")
    assert api.client.get("/api/deploy/status").get_json() == api.state


@pytest.mark.parametrize("jar", ["land-es7-biz.jar", {"name": "land-es7-biz.jar"}])
def test_upload_accepts_both_supported_server_jar_shapes(api, jar):
    api.servers[0]["jars"] = [jar]
    response = api.client.post("/api/jars", data={"file": (io.BytesIO(b"new"), "land-es7-biz.jar")})
    assert response.status_code == 200
    assert response.get_json()["name"] == "land-es7-biz.jar"
    assert (api.home / "jars" / "land-es7-biz.jar").read_bytes() == b"new"


def test_connection_result_echoes_request_identity_and_real_failure(api):
    response = api.client.post("/api/test-connection", json={"ip": "192.0.2.1", "request_id": "attempt-2"})
    assert response.get_json()["success"] is True
    assert api.events == [("test_result", {"ip": "192.0.2.1", "request_id": "attempt-2",
                                         "result": {"success": False, "message": "SSH认证失败", "steps": []}})]


OPERATIONS = [
    ("deploy", {"servers": ["192.0.2.1"], "jars": ["land-es7-biz.jar"]}),
    ("restart", {"items": [{"server": "192.0.2.1", "jar": "land-es7-biz.jar"}]}),
    ("rollback", {"server_ip": "192.0.2.1", "jar_name": "land-es7-biz.jar"}),
]


@pytest.mark.parametrize("operation,payload", OPERATIONS)
def test_operation_reserves_before_dispatch_and_does_not_fabricate_success_event(api, operation, payload):
    response = api.client.post(f"/api/{operation}", json=payload)
    assert response.status_code == 200
    assert response.get_json()["operation_id"] == "op-test"
    assert api.calls[0][1] == "op-test"
    assert api.state["fail"] == 1
    assert api.events == []


@pytest.mark.parametrize("operation,payload", OPERATIONS)
def test_busy_operation_is_rejected_before_starting_another_worker(api, operation, payload):
    api.state.update(status="running", operation="deploy", operation_id="existing")
    response = api.client.post(f"/api/{operation}", json=payload)
    assert response.status_code == 409
    assert response.get_json()["success"] is False
    assert api.calls == []


@pytest.mark.parametrize("operation,payload", OPERATIONS)
def test_thread_start_failure_releases_reservation(api, operation, payload):
    class FailingThread(ImmediateThread):
        def start(self):
            raise RuntimeError("cannot start thread")

    api.namespace["threading"].Thread = FailingThread
    response = api.client.post(f"/api/{operation}", json=payload)
    assert response.status_code == 500
    assert response.get_json()["success"] is False
    assert api.released == ["op-test"]
    assert api.calls == []


def test_rollback_rejects_wrong_owner_before_reserving_or_connecting(api):
    response = api.client.post("/api/rollback", json={"server_ip": "192.0.2.2", "jar_name": "land-es7-biz.jar"})
    assert response.status_code == 400
    assert api.state["status"] == "idle"
    assert api.calls == []


def test_cancel_requests_cancellation_without_emitting_a_completion(api):
    response = api.client.post("/api/deploy/cancel")
    assert response.get_json()["success"] is True
    assert api.calls == ["cancel"]
    assert api.events == []


def test_real_flask_application_reads_isolated_history_and_status(api, monkeypatch):
    monkeypatch.setenv("DEPLOY_HOME", str(api.home))
    db = importlib.import_module("db")
    service_module = importlib.import_module("deploy_service")
    monkeypatch.setattr(db, "_conn", None)
    monkeypatch.setattr(db, "DB_PATH", str(api.home / "data" / "deploy.db"))
    monkeypatch.setattr(service_module.ScheduleTaskManager, "recover", lambda self: None)
    try:
        module = importlib.import_module("app")
        monkeypatch.setattr(module, "deploy_service", api.service)
        db.add_deploy_history(["192.0.2.1"], ["land-es7-biz.jar"], 0, 1, status="failed")
        client = module.app.test_client()
        response = client.get("/api/deploy/history")
        assert response.status_code == 200
        assert response.get_json()[0]["fail_count"] == 1
        assert response.get_json()[0]["status"] == "failed"
        assert client.get("/api/deploy/status").get_json() == api.state
    finally:
        if db._conn is not None:
            db._conn.close()
            db._conn = None
