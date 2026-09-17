import os
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

import paramiko
from paramiko import SSHException

from runtime_paths import get_runtime_paths


_host_keys_lock = Lock()


def _save_host_keys_atomic(known_hosts: Path, keys: paramiko.HostKeys):
    descriptor, filename = tempfile.mkstemp(prefix=f".{known_hosts.name}-", dir=known_hosts.parent)
    temporary_path = Path(filename)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            for hostname, host_keys in keys.items():
                for key_type, key in host_keys.items():
                    stream.write(f"{hostname} {key_type} {key.get_base64()}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, known_hosts)
    finally:
        temporary_path.unlink(missing_ok=True)


class _AcceptNewHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def __init__(self, known_hosts: Path):
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        with _host_keys_lock:
            keys = paramiko.HostKeys()
            if self.known_hosts.exists():
                keys.load(str(self.known_hosts))
            existing = keys.lookup(hostname)
            if existing:
                expected = existing.get(key.get_name()) or next(iter(existing.values()))
                if expected != key:
                    raise paramiko.BadHostKeyException(hostname, key, expected)
            else:
                keys.add(hostname, key.get_name(), key)
                _save_host_keys_atomic(self.known_hosts, keys)
            client.get_host_keys().add(hostname, key.get_name(), key)


class ConnectionConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class AuthConfig:
    username: str
    password: str = ""
    use_private_key: bool = False
    key_path: str = ""
    key_passphrase: str = ""


@dataclass(frozen=True)
class ConnectionConfig:
    mode: str
    target_host: str
    target_port: int
    target_auth: AuthConfig
    jump_host: str = ""
    jump_port: int = 22
    jump_auth: AuthConfig | None = None
    host_key_policy: str = "accept-new"


class ManagedSshConnection:
    def __init__(self, client, resources: ExitStack):
        self.client = client
        self._resources = resources

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def close(self):
        self._resources.close()


class SshConnectionFactory:
    def __init__(self, config: ConnectionConfig):
        self.config = config

    def connect(self) -> ManagedSshConnection:
        resources = ExitStack()
        try:
            if self.config.mode == "direct":
                client = self._connect_client(
                    self.config.target_host,
                    self.config.target_port,
                    self.config.target_auth,
                )
                resources.callback(client.close)
            elif self.config.mode == "jump":
                client = self._connect_via_jump(resources)
            else:
                raise ConnectionConfigurationError("connection_mode must be direct or jump")
        except (ConnectionConfigurationError, OSError, RuntimeError, SSHException):
            resources.close()
            raise
        return ManagedSshConnection(client, resources)

    def _connect_via_jump(self, resources: ExitStack):
        if not self.config.jump_host or self.config.jump_auth is None:
            raise ConnectionConfigurationError("jump mode requires jump host and authentication")
        jump_client = self._connect_client(
            self.config.jump_host,
            self.config.jump_port,
            self.config.jump_auth,
        )
        resources.callback(jump_client.close)
        transport = jump_client.get_transport()
        if transport is None:
            raise ConnectionConfigurationError("jump SSH transport is unavailable")
        channel = transport.open_channel(
            "direct-tcpip",
            (self.config.target_host, self.config.target_port),
            ("", 0),
        )
        resources.callback(channel.close)
        target_client = self._connect_client(
            self.config.target_host,
            self.config.target_port,
            self.config.target_auth,
            sock=channel,
        )
        resources.callback(target_client.close)
        return target_client

    def _connect_client(self, host: str, port: int, auth: AuthConfig, sock=None):
        known_hosts = self._known_hosts_path()
        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
            with _host_keys_lock:
                if known_hosts.exists():
                    client.load_host_keys(str(known_hosts))
            client.set_missing_host_key_policy(self._host_key_policy(known_hosts))
            client.connect(
                hostname=host,
                port=port,
                username=auth.username,
                timeout=30,
                look_for_keys=False,
                allow_agent=False,
                **self._auth_kwargs(auth),
                **({"sock": sock} if sock is not None else {}),
            )
        except (ConnectionConfigurationError, OSError, RuntimeError, SSHException):
            client.close()
            raise
        return client

    def _auth_kwargs(self, auth: AuthConfig) -> dict[str, str]:
        if auth.use_private_key:
            key_path = Path(auth.key_path).expanduser()
            if not key_path.is_file():
                raise ConnectionConfigurationError("private key path does not exist")
            values = {"key_filename": str(key_path)}
            if auth.key_passphrase:
                values["passphrase"] = auth.key_passphrase
            return values
        if not auth.password:
            raise ConnectionConfigurationError("password is required when private key authentication is disabled")
        return {"password": auth.password}

    def _known_hosts_path(self) -> Path:
        known_hosts = get_runtime_paths().known_hosts
        known_hosts.parent.mkdir(parents=True, exist_ok=True)
        return known_hosts

    def _host_key_policy(self, known_hosts: Path):
        if self.config.host_key_policy == "accept-new":
            return _AcceptNewHostKeyPolicy(known_hosts)
        if self.config.host_key_policy == "strict":
            return paramiko.RejectPolicy()
        raise ConnectionConfigurationError("host_key_policy must be accept-new or strict")
