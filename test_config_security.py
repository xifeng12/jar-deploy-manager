from cryptography.fernet import Fernet
import pytest

import app as deploy_app
import crypto_utils
from runtime_paths import get_runtime_paths


SECRET_KEYS = (
    "password",
    "jump_password",
    "server_key_passphrase",
    "jump_key_passphrase",
)


def test_config_masks_all_secrets_and_preserves_masked_values(monkeypatch):
    # Given
    stored = {
        "connection_mode": "jump",
        "password": "encrypted-server-password",
        "jump_password": "encrypted-jump-password",
        "server_key_passphrase": "encrypted-server-passphrase",
        "jump_key_passphrase": "encrypted-jump-passphrase",
    }
    written = []
    monkeypatch.setattr(deploy_app, "get_all_settings", lambda: stored.copy())
    monkeypatch.setattr(deploy_app, "set_settings", written.append)
    monkeypatch.setattr(deploy_app.deploy_service, "update_config", lambda value: None)
    monkeypatch.setattr(deploy_app, "is_encrypted", lambda value: value.startswith("encrypted-"))
    monkeypatch.setattr(deploy_app, "encrypt", lambda value: f"encrypted-{value}")
    client = deploy_app.app.test_client()

    # When
    response = client.post(
        "/api/config",
        json={
            "connection_mode": "jump",
            "password": "********",
            "jump_password": "",
            "server_key_passphrase": "********",
            "jump_key_passphrase": "",
        },
    )
    rendered = client.get("/api/config").get_json()

    # Then
    assert response.status_code == 200
    assert written == [{"connection_mode": "jump", **stored}]
    assert all(rendered[key] == "********" for key in SECRET_KEYS)


def test_config_refuses_plaintext_secret_when_encryption_is_unavailable(monkeypatch):
    # Given
    monkeypatch.setattr(deploy_app, "get_all_settings", lambda: {})
    monkeypatch.setattr(deploy_app, "set_settings", lambda value: None)
    monkeypatch.setattr(deploy_app, "encrypt", lambda value: (_ for _ in ()).throw(crypto_utils.SecretEncryptionError("missing key")))
    client = deploy_app.app.test_client()

    # When
    response = client.post("/api/config", json={"password": "secret"})

    # Then
    assert response.status_code == 400
    assert "secret" not in response.get_data(as_text=True)


def test_non_windows_fernet_requires_key_and_round_trips(monkeypatch):
    # Given
    monkeypatch.setattr(crypto_utils, "is_windows", lambda: False)
    monkeypatch.delenv("DEPLOY_CONFIG_KEY", raising=False)

    # When / Then
    with pytest.raises(crypto_utils.SecretEncryptionError):
        crypto_utils.encrypt("secret")

    monkeypatch.setenv("DEPLOY_CONFIG_KEY", Fernet.generate_key().decode("ascii"))
    encrypted = crypto_utils.encrypt("secret")

    assert encrypted.startswith("fernet:")
    assert crypto_utils.decrypt(encrypted) == "secret"


def test_deploy_home_overrides_default_runtime_paths(monkeypatch, tmp_path):
    # Given
    monkeypatch.setenv("DEPLOY_HOME", str(tmp_path))

    # When
    paths = get_runtime_paths()

    # Then
    assert paths.database == tmp_path / "data" / "deploy.db"
    assert paths.jars == tmp_path / "jars"
    assert paths.logs == tmp_path / "logs"
    assert paths.scheduled_tasks == tmp_path / "scheduled_tasks.json"
    assert paths.known_hosts == tmp_path / "known_hosts"
