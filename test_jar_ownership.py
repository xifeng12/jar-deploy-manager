import sqlite3
from types import SimpleNamespace

import pytest

import db
import deploy_service as service_module


SERVERS = [
    {"ip": "192.0.2.8", "jars": ["land-es7-biz.jar", {"name": "land-file-biz.jar"}]},
    {"ip": "192.0.2.9", "jars": [{"name": "land-gis-biz.jar"}]},
]


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(db, "_conn", connection)
    db._init_tables()
    for server in SERVERS:
        db.add_server(server["ip"], server["jars"])
    yield connection
    connection.close()


@pytest.mark.parametrize("operation", ["add", "update"])
@pytest.mark.parametrize("jar_name", ["land-gis-biz.jar", "land-gis-biz (1).jar"])
def test_a_jar_cannot_be_assigned_to_a_second_server(database, operation, jar_name):
    before = db.get_all_servers()
    with pytest.raises(ValueError, match="land-gis-biz.jar"):
        if operation == "add":
            db.add_server("192.0.2.10", [jar_name])
        else:
            db.update_server("192.0.2.8", "192.0.2.8", [jar_name])
    assert db.get_all_servers() == before


def test_editing_the_owner_preserves_its_jar(database):
    db.update_server("192.0.2.9", "192.0.2.10", ["land-gis-biz.jar"], "GIS")
    assert db.get_server("192.0.2.10")["jars"] == ["land-gis-biz.jar"]


@pytest.mark.parametrize("jars, expected_calls, expected_fail", [
    (["land-gis-biz.jar"], ["192.0.2.9"], 1),
    (["land-es7-biz.jar", "land-file-biz.jar", "land-gis-biz.jar"],
     ["192.0.2.8", "192.0.2.9"], 3),
])
def test_deploy_only_connects_to_owners_and_counts_real_tasks(monkeypatch, tmp_path, jars, expected_calls, expected_fail):
    monkeypatch.setattr(service_module, "get_all_settings", lambda: {})
    monkeypatch.setattr(service_module, "get_all_servers", lambda: SERVERS)
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    (tmp_path / "jars").mkdir()
    for name in jars:
        (tmp_path / "jars" / name).write_bytes(b"test-jar")
    service = service_module.DeployService(SimpleNamespace(emit=lambda *args: None))
    calls = []

    def fail_connection(ip):
        calls.append(ip)
        raise OSError("simulated SSH failure")

    monkeypatch.setattr(service, "open_ssh_connection", fail_connection)
    result = service.deploy([s["ip"] for s in SERVERS], jars)
    assert sorted(calls) == expected_calls
    assert result["fail"] == expected_fail
    assert result["skip"] == 0


def test_restart_rejects_wrong_server_before_connecting(monkeypatch, tmp_path):
    monkeypatch.setattr(service_module, "get_all_settings", lambda: {})
    monkeypatch.setattr(service_module, "get_all_servers", lambda: SERVERS)
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    service = service_module.DeployService(SimpleNamespace(emit=lambda *args: None))
    calls = []
    monkeypatch.setattr(service, "open_ssh_connection", lambda ip: calls.append(ip))
    result = service.restart([{"server": "192.0.2.8", "jar": "land-gis-biz.jar"}])
    assert result["success"] == 0 and result["fail"] == 1
    assert calls == []


@pytest.mark.parametrize("servers, selected, jars", [
    (SERVERS, ["192.0.2.8"], ["land-gis-biz.jar"]),
    (SERVERS, ["192.0.2.9"], ["unknown.jar"]),
    (SERVERS + [{"ip": "192.0.2.10", "jars": ["land-gis-biz.jar"]}],
     ["192.0.2.9"], ["land-gis-biz.jar"]),
])
def test_plan_rejects_missing_wrong_or_ambiguous_owners(servers, selected, jars):
    with pytest.raises(ValueError):
        service_module.build_jar_plan(servers, selected, jars)


def test_deploy_api_removes_unrelated_servers(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    import app as deploy_app

    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: SERVERS)
    monkeypatch.setattr(deploy_app, "BASE_DIR", str(tmp_path))
    (tmp_path / "jars").mkdir(exist_ok=True)
    (tmp_path / "jars" / "land-gis-biz.jar").write_bytes(b"test")
    calls = []
    monkeypatch.setattr(deploy_app.deploy_service, "reserve_operation", lambda operation: "test-operation")
    monkeypatch.setattr(deploy_app.deploy_service, "deploy", lambda servers, jars, operation_id=None: calls.append((servers, jars)))

    class ImmediateThread:
        ident = 1

        def __init__(self, target):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(deploy_app.threading, "Thread", ImmediateThread)
    response = deploy_app.app.test_client().post("/api/deploy", json={
        "servers": [s["ip"] for s in SERVERS], "jars": ["land-gis-biz.jar"],
    })
    assert response.status_code == 200
    assert calls == [(["192.0.2.9"], ["land-gis-biz.jar"])]


def test_restart_api_rejects_wrong_owner(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    import app as deploy_app

    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: SERVERS)
    response = deploy_app.app.test_client().post("/api/restart", json={
        "items": [{"server": "192.0.2.8", "jar": "land-gis-biz.jar"}],
    })
    assert response.status_code == 400
    assert response.get_json()["error"] == "land-gis-biz.jar 的所属服务器 192.0.2.9 未选中"
