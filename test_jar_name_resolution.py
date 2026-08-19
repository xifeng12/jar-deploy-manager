import io

import app as deploy_app


def test_resolve_jar_name_uses_unique_database_name(monkeypatch):
    # Given
    servers = [{"jars": [{"name": "land-bus-biz.jar"}]}]
    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: servers)

    # When
    result = deploy_app.resolve_jar_name("land-bus(1）（1）.jar")

    # Then
    assert result == "land-bus-biz.jar"


def test_upload_saves_with_database_name(monkeypatch, tmp_path):
    # Given
    servers = [{"jars": [{"name": "land-bus-biz.jar"}]}]
    monkeypatch.setattr(deploy_app, "get_all_servers", lambda: servers)
    monkeypatch.setattr(deploy_app, "BASE_DIR", str(tmp_path))

    # When
    response = deploy_app.app.test_client().post(
        "/api/jars",
        data={"file": (io.BytesIO(b"jar"), "land-bus(1）（1）.jar")},
    )

    # Then
    assert response.get_json()["name"] == "land-bus-biz.jar"
    assert (tmp_path / "jars" / "land-bus-biz.jar").read_bytes() == b"jar"
