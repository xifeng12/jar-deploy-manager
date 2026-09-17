import io
import shlex
import threading
from types import SimpleNamespace

import pytest

import deploy_service as module


JAR = "demo.jar"
IP = "10.0.0.8"
REMOTE = "/opt/app/jars/demo.jar"


class Output(io.BytesIO):
    def __init__(self, text="", code=0):
        super().__init__(text.encode())
        self.channel = SimpleNamespace(recv_exit_status=lambda: code)


class Remote:
    def __init__(self, failure=None):
        self.files = {REMOTE: b"old"}
        self.failure = failure
        self.commands = []
        self.uploads = []
        self.pid = 12
        self.restarts = 0
        self.closed = False

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        output, code = "", 0
        tokens = shlex.split(command)
        if "printf 'script:'" in command:
            output = "script:exists\njar:exists\ndir:exists"
        elif command.endswith("jps -l"):
            output = f"{self.pid} /opt/app/jars/{JAR}\n999 unrelated-demo.jar"
        elif command.endswith(" restart"):
            self.restarts += 1
            if self.failure == "restart" and self.restarts == 1:
                code = 1
            elif self.failure != "old_pid":
                self.pid += 1
        elif "cp -p --" in command:
            if self.failure == "backup" and ".restore-" not in command:
                code = 1
            else:
                at = tokens.index("cp")
                source, target = tokens[at + 3:at + 5]
                self.files[target] = self.files[source]
                if "mv" in tokens:
                    at = tokens.index("mv")
                    source, target = tokens[at + 3:at + 5]
                    self.files[target] = self.files.pop(source)
        elif command.startswith("rm -f --"):
            self.files.pop(tokens[-1], None)
        elif command.startswith("find "):
            output = "\n".join(f"100 {name}" for name in self.files if name.startswith("/opt/app/backup/"))
        return None, Output(output, code), Output("simulated failure" if code else "")

    def open_sftp(self):
        return self

    def put(self, local, remote):
        self.uploads.append(remote)
        self.files[remote] = b"partial" if self.failure == "upload" else b"new"
        if self.failure == "upload":
            raise OSError("upload disconnected")

    def stat(self, path):
        return SimpleNamespace(st_size=len(self.files[path]), st_mode=0o100644)

    def chmod(self, path, mode):
        assert mode == 0o644

    def posix_rename(self, source, target):
        self.files[target] = self.files.pop(source)
        if self.failure == "rename_ack":
            raise OSError("rename acknowledgement lost")

    def remove(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        del self.files[path]

    def close(self):
        self.closed = True


@pytest.fixture
def service(monkeypatch, tmp_path):
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    monkeypatch.setattr(module, "get_all_settings", lambda: {"start_wait": "0", "deploy_interval": "0"})
    monkeypatch.setattr(module, "get_all_servers", lambda: [{"ip": IP, "jars": [JAR]}])
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    history, events = [], []
    monkeypatch.setattr(module, "add_deploy_history", lambda *args: history.append(args))
    monkeypatch.setattr(module, "deploy_lock", threading.Lock())
    monkeypatch.setattr(module, "deploy_cancel_flag", threading.Event())
    monkeypatch.setattr(module, "current_task", None)
    monkeypatch.setattr(module, "task_status", {})
    value = module.DeployService(SimpleNamespace(emit=lambda *args: events.append(args)))
    value.history, value.events = history, events
    (tmp_path / "jars").mkdir()
    (tmp_path / "jars" / JAR).write_bytes(b"new")
    return value


def connect(monkeypatch, service, remote):
    monkeypatch.setattr(service, "open_ssh_connection", lambda ip: SimpleNamespace(client=remote, close=remote.close))


@pytest.mark.parametrize("failure", ["backup", "upload", "restart", "old_pid", "rename_ack"])
def test_failed_deployment_preserves_or_restores_old_jar(monkeypatch, service, failure):
    remote = Remote(failure)
    connect(monkeypatch, service, remote)
    result = service.deploy([IP], [JAR])
    assert result["success"] == 0 and result["fail"] == 1
    assert result["status"] == "failed"
    assert remote.files[REMOTE] == b"old"
    assert not any(".upload-" in name or ".restore-" in name for name in remote.files)
    assert all(name != REMOTE for name in remote.uploads)
    if failure == "backup":
        assert remote.uploads == []
    if failure in ("restart", "old_pid", "rename_ack"):
        assert any(name.startswith("/opt/app/backup/") for name in remote.files)
    assert service.events[-1] == ("operation_done", result)
    assert service.history[-1][3] == 1
    assert not module.deploy_lock.locked()


def test_successful_deployment_replaces_only_after_backup_and_new_process(monkeypatch, service):
    remote = Remote()
    connect(monkeypatch, service, remote)
    result = service.deploy([IP], [JAR])
    assert result["success"] == 1 and result["fail"] == 0
    assert remote.files[REMOTE] == b"new" and remote.pid == 13
    assert any(content == b"old" for name, content in remote.files.items() if name.startswith("/opt/app/backup/"))
    assert service.history[-1][-1] == "deploy:done"


@pytest.mark.parametrize("operation", ["deploy", "restart", "rollback"])
def test_busy_operation_cannot_clear_cancellation_or_connect(monkeypatch, service, operation):
    reservation = service.reserve_operation("deploy")
    service.cancel_deploy()
    calls = []
    monkeypatch.setattr(service, "open_ssh_connection", lambda ip: calls.append(ip))
    if operation == "deploy":
        result = service.deploy([IP], [JAR])
    elif operation == "restart":
        result = service.restart([{"server": IP, "jar": JAR}])
    else:
        result = service.rollback(IP, JAR)
    assert result["status"] == "busy" and calls == []
    assert module.deploy_cancel_flag.is_set()
    assert service.events == [("log", {"message": "⏹️ 用户请求停止；当前单项将完成或恢复后退出", "level": "warning"})]
    service.release_reservation(reservation)


def test_rollback_rejects_wrong_owner_before_ssh(monkeypatch, service):
    calls = []
    monkeypatch.setattr(service, "open_ssh_connection", lambda ip: calls.append(ip))
    result = service.rollback("10.0.0.9", JAR)
    assert calls == [] and result["fail"] == 1


def test_rollback_preserves_backup_and_reports_verified_result(monkeypatch, service):
    remote = Remote()
    backup = "/opt/app/backup/demo.jar.原123"
    remote.files[backup] = b"backup"
    connect(monkeypatch, service, remote)
    result = service.rollback(IP, JAR)
    assert result["success"] == 1 and result["status"] == "done"
    assert remote.files[backup] == remote.files[REMOTE] == b"backup"


def test_rollback_connection_failure_is_not_success(monkeypatch, service):
    def unavailable(ip):
        raise OSError("offline")
    monkeypatch.setattr(service, "open_ssh_connection", unavailable)
    result = service.rollback(IP, JAR)
    assert result["success"] == 0 and result["fail"] == 1 and result["status"] == "failed"


def test_restart_nonzero_exit_cannot_succeed_with_old_pid(monkeypatch, service):
    remote = Remote("restart")
    connect(monkeypatch, service, remote)
    result = service.restart([{"server": IP, "jar": JAR}])
    assert result["success"] == 0 and result["fail"] == 1
    assert remote.pid == 12


def test_scheduler_preserves_cancelled_partial_result(monkeypatch, service):
    manager = module.ScheduleTaskManager(service, service.socketio)
    manager._save_tasks([{"id": "t", "name": "test", "type": "deploy", "status": "pending", "servers": [IP], "jars": [JAR]}])
    monkeypatch.setattr(service, "deploy", lambda *args: {"status": "cancelled", "success": 1, "fail": 0, "skip": 2})
    manager._execute_task("t")
    task = manager.get_all()[0]
    assert task["status"] == "cancelled" and task["result"]["skip"] == 2


def test_recovery_marks_running_interrupted_without_replay(monkeypatch, service):
    manager = module.ScheduleTaskManager(service, service.socketio)
    manager._save_tasks([{"id": "t", "status": "running"}])
    assert manager.recover() == 0
    assert manager.get_all()[0]["status"] == "interrupted"


def test_cancelled_deployment_reports_terminal_state_only_after_worker_returns(monkeypatch, service):
    remote = Remote()
    connect(monkeypatch, service, remote)
    original = remote.put
    def cancel_upload(local, path):
        original(local, path)
        service.cancel_deploy()
        assert not any(event == "operation_done" for event, payload in service.events)
    monkeypatch.setattr(remote, "put", cancel_upload)
    result = service.deploy([IP], [JAR])
    assert result["status"] == "cancelled" and result["success"] == 1
    assert service.get_operation_status()["status"] == "cancelled"


def test_dpapi_failure_is_not_reported_as_network_timeout(monkeypatch, service):
    def invalid_credentials(ip):
        raise module.SecretEncryptionError("invalid DPAPI state")
    monkeypatch.setattr(service, "_connection_config", invalid_credentials)
    result = service.test_connection(IP)
    assert result["success"] is False
    assert "重新保存" in result["message"] and "网络" not in result["message"]


def test_failed_socket_emits_do_not_leave_operation_locked(monkeypatch, service):
    def unavailable(*args):
        raise OSError("socket disconnected")
    monkeypatch.setattr(service.socketio, "emit", unavailable)
    connect(monkeypatch, service, Remote())
    result = service.deploy([IP], [JAR])
    assert result["success"] == 1
    assert not module.deploy_lock.locked()
    assert service.get_operation_status()["status"] == "done"


def test_failed_worker_start_broadcasts_terminal_reservation(service):
    reservation = service.reserve_operation("restart")
    service.release_reservation(reservation)
    event, result = service.events[-1]
    assert event == "operation_done" and result["operation_id"] == reservation
    assert result["status"] == "failed" and not module.deploy_lock.locked()
