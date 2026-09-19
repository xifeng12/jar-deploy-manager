import json
from pathlib import Path

import pytest

import app as deploy_app
import deploy_service
from crypto_utils import SecretEncryptionError
from ssh_connection import ConnectionConfigurationError


class _FakeChannel:
    def recv_exit_status(self):
        return 0


class _FakeOutput:
    def __init__(self, data=b""):
        self._data = data
        self.channel = _FakeChannel()

    def read(self):
        return self._data


class _FakeClient:
    def __init__(self):
        self.commands = []

    def exec_command(self, command, timeout=None):
        self.commands.append(command)
        if "---JARS---" in command:
            return None, _FakeOutput(b"---JARS---\n---SCRIPTS---\n---LOGDIRS---"), _FakeOutput()
        if command.endswith("jps"):
            return None, _FakeOutput(), _FakeOutput()
        return None, _FakeOutput(), _FakeOutput()


class _FakeConnection:
    def __init__(self):
        self.client = _FakeClient()

    def close(self):
        pass


def test_connection_api_rejects_an_unregistered_target_before_starting_work(monkeypatch):
    # Given
    calls = []
    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: [{"ip": "10.0.0.8"}])
    monkeypatch.setattr(deploy_app.deploy_service, "test_connection", lambda value: calls.append(value))

    # When
    response = deploy_app.app.test_client().post("/api/test-connection", json={"ip": "10.0.0.99"})

    # Then
    assert response.status_code == 404
    assert calls == []


def test_ssh_configuration_rejects_an_unregistered_target_before_reading_credentials(monkeypatch):
    # Given
    monkeypatch.setattr(deploy_service, "get_all_servers", lambda: [{"ip": "10.0.0.8"}])
    deploy_app.deploy_service.config = {"password": "plain-text"}

    # When / Then
    with pytest.raises(ConnectionConfigurationError):
        deploy_app.deploy_service._connection_config("10.0.0.99")


def test_server_api_rejects_an_address_with_shell_syntax(monkeypatch):
    # Given
    calls = []
    monkeypatch.setattr(deploy_app, "add_server", lambda *args: calls.append(args))

    # When
    response = deploy_app.app.test_client().post("/api/servers", json={"ip": "10.0.0.8; id"})

    # Then
    assert response.status_code == 400
    assert calls == []


def test_resolve_jar_name_rejects_a_database_name_that_can_escape_the_jars_directory(monkeypatch):
    # Given
    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: [{"jars": [{"name": "../../escape.jar"}]}])

    # When
    result = deploy_app.resolve_jar_name("escape.jar")

    # Then
    assert result is None


def test_unencrypted_legacy_secret_is_never_returned_to_ssh(monkeypatch):
    # Given
    monkeypatch.setattr(deploy_service, "is_encrypted", lambda value: False)
    deploy_app.deploy_service.config = {"password": "plain-text"}

    # When / Then
    with pytest.raises(SecretEncryptionError):
        deploy_app.deploy_service._secret_value("password")


def test_discover_jars_quotes_a_configured_remote_path(monkeypatch):
    # Given
    connection = _FakeConnection()
    service = deploy_app.deploy_service
    service.config = {
        "jar_dir": "/srv/app; touch /tmp/owned",
        "script_dir": "/srv/scripts",
        "script_prefix": "start-",
        "script_suffix": ".sh",
        "log_dir": "/srv/logs",
    }
    monkeypatch.setattr(service, "open_ssh_connection", lambda value: connection)

    # When
    service.discover_jars("10.0.0.8")

    # Then
    assert "'/srv/app; touch /tmp/owned'" in connection.client.commands[0]


def test_restart_quotes_a_configured_script_path(monkeypatch):
    # Given
    monkeypatch.setattr(deploy_service, "get_all_servers", lambda: [{"ip": "10.0.0.8", "jars": ["demo.jar"]}])
    connection = _FakeConnection()
    service = deploy_app.deploy_service
    service.config = {"script_dir": "/srv/scripts; touch /tmp/owned", "start_wait": 0, "deploy_interval": 0}
    monkeypatch.setattr(service, "open_ssh_connection", lambda value: connection)
    monkeypatch.setattr(service, "_find_jar_script", lambda value: "demo.sh")

    # When
    service.restart([{"server": "10.0.0.8", "jar": "demo.jar"}])

    # Then
    assert any("'/srv/scripts; touch /tmp/owned/demo.sh'" in command for command in connection.client.commands)


def test_local_scripts_and_release_dependencies_are_pinned():
    # Given
    root = Path(__file__).parent

    # When
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    workflow = (root / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    # Then
    assert "--host 0.0.0.0" not in package["scripts"]["dev"]
    assert "--host 0.0.0.0" not in package["scripts"]["preview"]
    assert "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683" in workflow
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065" in workflow
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in workflow
    assert "actions/upload-artifact@65c4c4a1ddee5b72f698fdd19549f0f0fb45cf08" in workflow
    assert "pyinstaller==6.21.0" in workflow
    assert "pytest==9.1.1" in workflow
