"""Integration tests for the auth middleware (none / token modes).

Auth is foundational — if these regress, every other endpoint either
becomes unauthenticated (security incident) or unreachable (every test
fails).  Specific scenarios:

  * ``SNOWTUNER_AUTH_MODE=none`` from loopback works (the api_client default)
  * ``SNOWTUNER_AUTH_MODE=token`` rejects missing/wrong tokens
  * ``SNOWTUNER_AUTH_MODE=token`` accepts the right bearer
  * Public paths (``/health``, ``/openapi.json``) bypass auth in token mode
"""
from __future__ import annotations

import secrets
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


def _make_token_client(monkeypatch, tmp_path) -> Iterator[tuple[TestClient, str]]:
    """Build a TestClient in token mode and return both the client and
    the active token.  Used by tests in this file only — moving it into
    conftest would force every other test to deal with auth, which is
    wasteful."""
    token = secrets.token_urlsafe(24)
    monkeypatch.setenv("SNOWTUNER_AUTH_MODE", "token")
    monkeypatch.setenv("SNOWTUNER_API_TOKEN", token)
    monkeypatch.setenv("SNOWTUNER_AUTOMATION_INTERVAL", "0")

    from snowtuner.storage import db as storage_db
    monkeypatch.setattr(storage_db, "db_path", lambda: tmp_path / "test.duckdb")
    storage_db.close_connection()

    from snowtuner.api.app import create_app
    app = create_app()
    with TestClient(app) as client:
        yield client, token
    storage_db.close_connection()


class TestNoneMode:
    def test_loopback_request_succeeds(self, api_client):
        # TestClient connects from 127.0.0.1; 'none' mode authorizes it.
        r = api_client.get("/recommendations")
        assert r.status_code == 200

    def test_health_endpoint_always_accessible(self, api_client):
        r = api_client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestTokenMode:
    @pytest.fixture
    def token_client(self, monkeypatch, tmp_path):
        yield from _make_token_client(monkeypatch, tmp_path)

    def test_missing_bearer_returns_401(self, token_client):
        client, _ = token_client
        r = client.get("/recommendations")
        assert r.status_code == 401
        assert "missing Authorization" in r.json()["detail"]

    def test_wrong_bearer_returns_401(self, token_client):
        client, _ = token_client
        r = client.get(
            "/recommendations",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert r.status_code == 401
        assert "invalid bearer token" in r.json()["detail"]

    def test_correct_bearer_passes(self, token_client):
        client, token = token_client
        r = client.get(
            "/recommendations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200

    def test_health_bypasses_auth_in_token_mode(self, token_client):
        """/health is the load-balancer probe path — must work without
        a token even when token mode is on, or LBs can't probe."""
        client, _ = token_client
        r = client.get("/health")
        assert r.status_code == 200

    def test_openapi_bypasses_auth_in_token_mode(self, token_client):
        """The OpenAPI schema doc is conventionally public; tools like
        codegen / clients fetch it without credentials."""
        client, _ = token_client
        r = client.get("/openapi.json")
        assert r.status_code == 200
