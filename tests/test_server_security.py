import base64

from fastapi.testclient import TestClient

from poc.server import app


def test_optional_basic_auth_protects_app_and_api(monkeypatch):
    monkeypatch.setenv("FORM_LAB_AUTH_USER", "reviewer")
    monkeypatch.setenv("FORM_LAB_AUTH_PASSWORD", "secret-test-value")
    client = TestClient(app)
    assert client.get("/api/health").status_code == 200
    for path in ("/", "/generator", "/static/app.js", "/static/generator.js",
                 "/api/templates", "/api/templates/bank_account/preview/1"):
        denied = client.get(path)
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == 'Basic realm="Form Lab"'
    token = base64.b64encode(b"reviewer:secret-test-value").decode("ascii")
    allowed = client.get("/api/templates", headers={"Authorization": f"Basic {token}"})
    assert allowed.status_code == 200
    assert client.get("/api/templates", headers={"Authorization": "Basic invalid"}).status_code == 401


def test_partial_basic_auth_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("FORM_LAB_AUTH_USER", "reviewer")
    monkeypatch.delenv("FORM_LAB_AUTH_PASSWORD", raising=False)
    assert TestClient(app).get("/").status_code == 503
