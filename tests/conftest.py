import pytest


@pytest.fixture(autouse=True)
def isolated_auth_environment(monkeypatch):
    monkeypatch.delenv("FORM_LAB_AUTH_USER", raising=False)
    monkeypatch.delenv("FORM_LAB_AUTH_PASSWORD", raising=False)
