from types import SimpleNamespace

import pytest

import ssh_connection
from ssh_connection import AuthConfig, ConnectionConfig, SshConnectionFactory


class FakeChannel:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self):
        self.channel = FakeChannel()
        self.open_calls = []

    def open_channel(self, kind, destination, source):
        self.open_calls.append((kind, destination, source))
        return self.channel


class FakeClient:
    instances = []
    fail_target = False

    def __init__(self):
        self.calls = []
        self.closed = False
        self.transport = FakeTransport()
        FakeClient.instances.append(self)

    def load_system_host_keys(self):
        self.calls.append(("load_system_host_keys",))

    def load_host_keys(self, path):
        self.calls.append(("load_host_keys", path))

    def set_missing_host_key_policy(self, policy):
        self.calls.append(("set_policy", policy))

    def connect(self, **kwargs):
        self.calls.append(("connect", kwargs))
        if FakeClient.fail_target and len(FakeClient.instances) == 2:
            raise RuntimeError("target failed")

    def get_transport(self):
        return self.transport

    def save_host_keys(self, path):
        self.calls.append(("save_host_keys", path))

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def fake_paramiko(monkeypatch, tmp_path):
    # Given
    FakeClient.instances = []
    FakeClient.fail_target = False
    fake = SimpleNamespace(
        SSHClient=FakeClient,
        AutoAddPolicy=lambda: "accept-new",
        RejectPolicy=lambda: "strict",
    )
    monkeypatch.setattr(ssh_connection, "paramiko", fake)
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))


def password_auth(username, password):
    return AuthConfig(username=username, password=password)


def private_key_auth(username, key_path, passphrase=""):
    return AuthConfig(
        username=username,
        use_private_key=True,
        key_path=key_path,
        key_passphrase=passphrase,
    )


def test_connects_direct_with_password_and_custom_target_port():
    # Given
    config = ConnectionConfig(
        mode="direct",
        target_host="10.0.0.8",
        target_port=2202,
        target_auth=password_auth("deploy", "server-password"),
    )

    # When
    with SshConnectionFactory(config).connect() as connection:
        client = connection.client

    # Then
    assert client.calls[-1] == (
        "connect",
        {
            "hostname": "10.0.0.8",
            "port": 2202,
            "username": "deploy",
            "password": "server-password",
            "timeout": 30,
            "look_for_keys": False,
            "allow_agent": False,
        },
    )
    assert client.closed is True


def test_connects_jump_with_passwords_and_closes_every_resource():
    # Given
    config = ConnectionConfig(
        mode="jump",
        target_host="10.0.0.8",
        target_port=2202,
        target_auth=password_auth("deploy", "server-password"),
        jump_host="10.0.0.4",
        jump_port=2222,
        jump_auth=password_auth("jump", "jump-password"),
    )

    # When
    with SshConnectionFactory(config).connect() as connection:
        target = connection.client

    # Then
    jump, target = FakeClient.instances
    assert jump.calls[-1][1]["hostname"] == "10.0.0.4"
    assert jump.calls[-1][1]["password"] == "jump-password"
    assert jump.transport.open_calls == [("direct-tcpip", ("10.0.0.8", 2202), ("", 0))]
    assert target.calls[-1][1]["sock"] is jump.transport.channel
    assert target.closed is True
    assert jump.transport.channel.closed is True
    assert jump.closed is True


def test_uses_private_key_for_jump_and_password_for_target(tmp_path):
    # Given
    key_path = tmp_path / "jump.key"
    key_path.write_text("key", encoding="utf-8")
    config = ConnectionConfig(
        mode="jump",
        target_host="10.0.0.8",
        target_port=22,
        target_auth=password_auth("deploy", "server-password"),
        jump_host="10.0.0.4",
        jump_port=22,
        jump_auth=private_key_auth("jump", str(key_path), "jump-passphrase"),
    )

    # When
    with SshConnectionFactory(config).connect():
        pass

    # Then
    jump, target = FakeClient.instances
    assert jump.calls[-1][1]["key_filename"] == str(key_path)
    assert jump.calls[-1][1]["passphrase"] == "jump-passphrase"
    assert "password" not in jump.calls[-1][1]
    assert target.calls[-1][1]["password"] == "server-password"


def test_uses_password_for_jump_and_private_key_for_target(tmp_path):
    # Given
    key_path = tmp_path / "target.key"
    key_path.write_text("key", encoding="utf-8")
    config = ConnectionConfig(
        mode="jump",
        target_host="10.0.0.8",
        target_port=22,
        target_auth=private_key_auth("deploy", str(key_path)),
        jump_host="10.0.0.4",
        jump_port=22,
        jump_auth=password_auth("jump", "jump-password"),
    )

    # When
    with SshConnectionFactory(config).connect():
        pass

    # Then
    jump, target = FakeClient.instances
    assert jump.calls[-1][1]["password"] == "jump-password"
    assert target.calls[-1][1]["key_filename"] == str(key_path)
    assert "password" not in target.calls[-1][1]


def test_selects_strict_or_atomic_accept_new_policy(tmp_path):
    # Given
    strict = ConnectionConfig(
        mode="direct",
        target_host="10.0.0.8",
        target_port=22,
        target_auth=password_auth("deploy", "server-password"),
        host_key_policy="strict",
    )
    accept_new = ConnectionConfig(
        mode="direct",
        target_host="10.0.0.9",
        target_port=22,
        target_auth=password_auth("deploy", "server-password"),
        host_key_policy="accept-new",
    )

    # When
    with SshConnectionFactory(strict).connect():
        pass
    with SshConnectionFactory(accept_new).connect():
        pass

    # Then
    strict_client, accept_new_client = FakeClient.instances
    assert ("set_policy", "strict") in strict_client.calls
    policy = next(call[1] for call in accept_new_client.calls if call[0] == "set_policy")
    assert isinstance(policy, ssh_connection._AcceptNewHostKeyPolicy)
    assert policy.known_hosts == tmp_path / "known_hosts"
    assert not any(call[0] == "save_host_keys" for call in accept_new_client.calls)


def test_cleans_jump_client_and_channel_when_target_connection_fails():
    # Given
    FakeClient.fail_target = True
    config = ConnectionConfig(
        mode="jump",
        target_host="10.0.0.8",
        target_port=22,
        target_auth=password_auth("deploy", "server-password"),
        jump_host="10.0.0.4",
        jump_port=22,
        jump_auth=password_auth("jump", "jump-password"),
    )

    # When / Then
    with pytest.raises(RuntimeError, match="target failed"):
        SshConnectionFactory(config).connect()

    jump, target = FakeClient.instances
    assert target.closed is True
    assert jump.transport.channel.closed is True
    assert jump.closed is True
