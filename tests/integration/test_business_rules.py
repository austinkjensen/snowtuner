"""Integration tests for the business rules enforced by API endpoints.

These tests catch the "you forgot the validation" class of regression:
status-state-machine violations, foreign-key sanity, etc.  Each test
exercises a rule that's explicit in the API code's HTTPException raises.
"""
from __future__ import annotations

import json

from snowtuner.storage.db import get_connection


def _seed_recommendation(status: str = "PROPOSED") -> int:
    """Insert one recommendation in the given state, return its id."""
    conn = get_connection()
    row = conn.execute(
        """
        INSERT INTO app.recommendations
          (generated_by, action_type, target_resource,
           action_payload, rationale, evidence, expected_impact, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        [
            "test@0.1.0", "ALTER_WAREHOUSE",
            "warehouse:WH:WAREHOUSE_SIZE",
            json.dumps({
                "type": "ALTER_WAREHOUSE",
                "warehouse_name": "WH",
                "changes": [
                    {
                        "knob": "WAREHOUSE_SIZE",
                        "current_value": "MEDIUM",
                        "proposed_value": "SMALL",
                    }
                ],
            }),
            "test", json.dumps([]),
            json.dumps({"confidence": 0.9}),
            status,
        ],
    ).fetchone()
    return int(row[0])


class TestRecommendationBusinessRules:
    def test_accept_nonexistent_returns_404(self, api_client):
        r = api_client.post("/recommendations/99999/accept", json={})
        assert r.status_code == 404

    def test_reject_nonexistent_returns_404(self, api_client):
        r = api_client.post("/recommendations/99999/reject", json={})
        assert r.status_code == 404

    def test_accept_then_status_is_accepted(self, api_client):
        rid = _seed_recommendation()
        r = api_client.post(f"/recommendations/{rid}/accept", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "ACCEPTED"


class TestSyncBackfillValidation:
    """``POST /sync/backfill`` validates ``days`` via Pydantic Query
    constraints (gt=0, le=365).  Verify those fire."""

    def test_zero_days_is_rejected(self, api_client):
        r = api_client.post("/sync/backfill?days=0")
        assert r.status_code == 422  # FastAPI validation error

    def test_negative_days_is_rejected(self, api_client):
        r = api_client.post("/sync/backfill?days=-1")
        assert r.status_code == 422

    def test_over_max_days_is_rejected(self, api_client):
        r = api_client.post("/sync/backfill?days=400")
        assert r.status_code == 422


class TestEventsQueryValidation:
    """``GET /events?limit=`` is constrained gt=0 le=1000."""

    def test_limit_zero_rejected(self, api_client):
        r = api_client.get("/events?limit=0")
        assert r.status_code == 422

    def test_limit_too_large_rejected(self, api_client):
        r = api_client.get("/events?limit=10000")
        assert r.status_code == 422

    def test_offset_negative_rejected(self, api_client):
        r = api_client.get("/events?offset=-1")
        assert r.status_code == 422


class TestHealthAndStatus:
    """Read-only endpoints that should always succeed on a fresh DB."""

    def test_health(self, api_client):
        r = api_client.get("/health")
        assert r.status_code == 200

    def test_version_info(self, api_client):
        # GET / is reserved for the SPA's index.html (StaticFiles mount in
        # production); the JSON discovery payload moved to /version when we
        # added the SPA-on-same-origin deploy.
        r = api_client.get("/version")
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "snowtuner"

    def test_recommendations_empty_list(self, api_client):
        # No recs seeded — should return empty list, not 500
        r = api_client.get("/recommendations")
        assert r.status_code == 200
        assert r.json() == []

    def test_events_empty(self, api_client):
        r = api_client.get("/events")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["rows"] == []
