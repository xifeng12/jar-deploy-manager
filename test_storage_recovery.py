import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import paramiko
import pytest

import crypto_utils
import db
import ssh_connection


@pytest.fixture
def database(monkeypatch):
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    connection.row_factory = sqlite3.Row
    monkeypatch.setattr(db, "_conn", connection)
    db._init_tables()
    db.add_server("192.0.2.10", ["alpha.jar"], "Alpha")
    db.add_server("192.0.2.20", ["beta.jar"], "Beta")
    yield connection
    connection.close()


def server_rows(connection):
    return [tuple(row) for row in connection.execute("SELECT * FROM servers ORDER BY ip")]


def test_duplicate_add_preserves_existing_server(database):
    before = server_rows(database)
    with pytest.raises(ValueError, match="已存在"):
        db.add_server("192.0.2.10", ["replacement.jar"], "Replacement")
    assert server_rows(database) == before


def test_rename_to_existing_ip_preserves_both_servers(database):
    before = server_rows(database)
    with pytest.raises(ValueError, match="已存在"):
        db.update_server("192.0.2.10", "192.0.2.20", ["alpha.jar"], "Moved")
    assert server_rows(database) == before


def test_missing_source_is_not_created_by_update(database):
    before = server_rows(database)
    with pytest.raises(ValueError, match="不存在"):
        db.update_server("192.0.2.30", "192.0.2.40", ["new.jar"])
    assert server_rows(database) == before


def test_valid_rename_preserves_status_and_creation_time(database):
    database.execute("UPDATE servers SET status='ready', created_at='2020-01-01' WHERE ip='192.0.2.10'")
    database.commit()
    db.update_server("192.0.2.10", "192.0.2.30", ["alpha.jar"], "Moved")
    assert db.get_server("192.0.2.10") is None
    assert db.get_server("192.0.2.30")["status"] == "ready"
    row = database.execute("SELECT created_at FROM servers WHERE ip='192.0.2.30'").fetchone()
    assert row["created_at"] == "2020-01-01"


def test_failed_update_rolls_back_and_does_not_poison_next_write(database):
    before = server_rows(database)
    database.execute("""CREATE TRIGGER fail_server_update AFTER UPDATE ON servers
        BEGIN SELECT RAISE(ABORT, 'simulated update failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="simulated update failure"):
        db.update_server("192.0.2.10", "192.0.2.30", ["alpha.jar"], "Moved")
    assert not database.in_transaction
    db.set_setting("unrelated", "saved")
    assert server_rows(database) == before


@pytest.mark.parametrize("operation", ["encrypt", "decrypt"])
def test_dpapi_os_failure_is_a_sanitized_secret_error(monkeypatch, operation):
    monkeypatch.setattr(crypto_utils, "is_windows", lambda: True)

    def fail(value):
        raise OSError("synthetic-sensitive-detail")

    monkeypatch.setattr(crypto_utils, "_protect", fail)
    monkeypatch.setattr(crypto_utils, "_unprotect", fail)
    value = "synthetic-plaintext" if operation == "encrypt" else "dpapi:AA=="
    with pytest.raises(crypto_utils.SecretEncryptionError) as caught:
        getattr(crypto_utils, operation)(value)
    assert "synthetic" not in str(caught.value)
    assert caught.value.__suppress_context__ is True


def test_malformed_dpapi_ciphertext_is_a_secret_error(monkeypatch):
    monkeypatch.setattr(crypto_utils, "is_windows", lambda: True)
    with pytest.raises(crypto_utils.SecretEncryptionError):
        crypto_utils.decrypt("dpapi:invalid-base64!")


@pytest.fixture
def offline_ssh(monkeypatch, tmp_path):
    state = SimpleNamespace(local=threading.local(), barrier=None, authenticated=[])
    real_client = paramiko.SSHClient

    class OfflineClient(real_client):
        def load_system_host_keys(self, filename=None):
            pass

        def _log(self, *args):
            pass

        def connect(self, hostname, port, **kwargs):
            assert kwargs["look_for_keys"] is False
            assert kwargs["allow_agent"] is False
            key = state.local.key
            if state.barrier:
                state.barrier.wait(timeout=5)
            name = hostname if port == 22 else f"[{hostname}]:{port}"
            known = self.get_host_keys().lookup(name)
            if known:
                expected = known.get(key.get_name()) or next(iter(known.values()))
                if key != expected:
                    raise paramiko.BadHostKeyException(name, key, expected)
            else:
                self._policy.missing_host_key(self, name, key)
            state.authenticated.append((hostname, key))

    monkeypatch.setattr(ssh_connection.paramiko, "SSHClient", OfflineClient)
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))
    return state


@pytest.fixture(scope="module")
def host_keys():
    return paramiko.RSAKey.generate(1024), paramiko.RSAKey.generate(1024)


def connect_offline(state, host, key, policy="accept-new", port=22):
    state.local.key = key
    config = ssh_connection.ConnectionConfig(
        mode="direct", target_host=host, target_port=port,
        target_auth=ssh_connection.AuthConfig(username="dummy", password="dummy"),
        host_key_policy=policy,
    )
    with ssh_connection.SshConnectionFactory(config).connect():
        pass


def test_parallel_first_connections_preserve_all_host_keys(offline_ssh, host_keys, tmp_path):
    offline_ssh.barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_offline, offline_ssh, host, key)
                   for host, key in zip(("192.0.2.10", "192.0.2.20"), host_keys)]
        for future in futures:
            future.result(timeout=10)
    saved = paramiko.HostKeys(str(tmp_path / "known_hosts"))
    assert set(saved) == {"192.0.2.10", "192.0.2.20"}
    for host, key in offline_ssh.authenticated:
        assert saved.check(host, key)


def test_parallel_conflicting_host_keys_reject_before_auth(offline_ssh, host_keys, tmp_path):
    offline_ssh.barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_offline, offline_ssh, "192.0.2.10", key)
                   for key in host_keys]
        errors = [future.exception(timeout=10) for future in futures]
    assert sum(error is None for error in errors) == 1
    assert sum(isinstance(error, paramiko.BadHostKeyException) for error in errors) == 1
    assert len(offline_ssh.authenticated) == 1
    winner = offline_ssh.authenticated[0][1]
    assert paramiko.HostKeys(str(tmp_path / "known_hosts")).check("192.0.2.10", winner)


def test_parallel_same_host_key_is_accepted(offline_ssh, host_keys, tmp_path):
    offline_ssh.barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(connect_offline, offline_ssh, "192.0.2.10", host_keys[0])
                   for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert len(offline_ssh.authenticated) == 2
    saved = paramiko.HostKeys(str(tmp_path / "known_hosts"))
    assert set(saved) == {"192.0.2.10"}
    assert saved.check("192.0.2.10", host_keys[0])


def test_failed_atomic_replace_preserves_prior_host_keys(offline_ssh, host_keys, tmp_path, monkeypatch):
    connect_offline(offline_ssh, "192.0.2.10", host_keys[0])
    path = tmp_path / "known_hosts"
    before = path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replacement failure")

    monkeypatch.setattr(ssh_connection.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replacement failure"):
        connect_offline(offline_ssh, "192.0.2.20", host_keys[1])
    assert path.read_bytes() == before
    assert len(offline_ssh.authenticated) == 1
    assert sorted(item.name for item in tmp_path.iterdir()) == ["known_hosts"]


def test_strict_unknown_and_changed_keys_are_rejected(offline_ssh, host_keys, tmp_path):
    with pytest.raises(paramiko.SSHException):
        connect_offline(offline_ssh, "192.0.2.10", host_keys[0], policy="strict")
    assert not (tmp_path / "known_hosts").exists()
    connect_offline(offline_ssh, "192.0.2.10", host_keys[0], port=2202)
    connect_offline(offline_ssh, "192.0.2.10", host_keys[0], policy="strict", port=2202)
    with pytest.raises(paramiko.BadHostKeyException):
        connect_offline(offline_ssh, "192.0.2.10", host_keys[1], port=2202)
