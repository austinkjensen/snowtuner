"""Integration tests for ``GET /events`` and the log_event side-effect.

Two layers:
  1. State-changing endpoints write events as a side-effect — verify the
     row count grows after the operation.
  2. The /events query surface filters correctly by actor / action /
     subject / time window.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from snowtuner.events import log_event
from snowtuner.storage.db import get_connection


def _seed_events(n_user: int = 3, n_automation: int = 2, n_failed: int = 1) -> None:
    """Drop a known mix of events directly into the DB.  Used by /events
    query tests so we don't depend on which endpoints fire which events."""
    conn = get_connection()
    for i in range(n_user):
        log_event(
            conn,
            actor="user",
            action="recommendation.accept",
            subject=str(i),
            payload={"target": f"WH_{i}"},
        )
    for i in range(n_automation):
        log_event(
            conn,
            actor="automation",
            action="automation.tick.complete",
            outcome="success",
            payload={"stages": []},
        )
    for i in range(n_failed):
        log_event(
            conn,
            actor="sync",
            action="sync.source.failure",
            subject="query_history",
            outcome="failed",
            error="connection refused",
        )


class TestEventsQueryFilters:
    def test_no_filter_returns_all(self, api_client):
        _seed_events()
        r = api_client.get("/events")
        assert r.status_code == 200
        body = r.json()
        # 3 user + 2 automation + 1 failed = 6
        assert body["total"] == 6
        assert len(body["rows"]) == 6

    def test_filter_by_actor(self, api_client):
        _seed_events()
        r = api_client.get("/events?actor=user")
        assert r.json()["total"] == 3

    def test_filter_by_action(self, api_client):
        _seed_events()
        r = api_client.get("/events?action=sync.source.failure")
        assert r.json()["total"] == 1
        assert r.json()["rows"][0]["error"] == "connection refused"

    def test_filter_by_action_prefix(self, api_client):
        _seed_events()
        # 'recommendation.' matches all 3 user events (which use recommendation.accept)
        r = api_client.get("/events?action_prefix=recommendation.")
        assert r.json()["total"] == 3

    def test_filter_by_outcome(self, api_client):
        _seed_events()
        r = api_client.get("/events?outcome=failed")
        assert r.json()["total"] == 1

    def test_filter_by_subject(self, api_client):
        _seed_events()
        r = api_client.get("/events?subject=0")
        assert r.json()["total"] == 1

    def test_ordering_is_newest_first(self, api_client):
        _seed_events()
        r = api_client.get("/events")
        rows = r.json()["rows"]
        # Each event has a strictly-increasing ID (the sequence) so
        # descending-by-(timestamp, id) puts the highest ID first.
        ids = [row["id"] for row in rows]
        assert ids == sorted(ids, reverse=True)

    def test_pagination(self, api_client):
        _seed_events(n_user=10, n_automation=0, n_failed=0)
        page1 = api_client.get("/events?limit=3&offset=0").json()
        page2 = api_client.get("/events?limit=3&offset=3").json()
        # Total stays the same across pages
        assert page1["total"] == page2["total"] == 10
        # No overlap between pages
        ids1 = {r["id"] for r in page1["rows"]}
        ids2 = {r["id"] for r in page2["rows"]}
        assert ids1.isdisjoint(ids2)

    def test_filter_by_time_window(self, api_client):
        conn = get_connection()
        old_ts = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)
        # Manually insert one old + one new
        conn.execute(
            """INSERT INTO app.events (timestamp, actor, action, outcome)
               VALUES (?, 'user', 'old.event', 'success')""",
            [old_ts],
        )
        log_event(conn, actor="user", action="new.event")
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)
        ).isoformat()
        r = api_client.get(f"/events?since={cutoff}")
        actions = {row["action"] for row in r.json()["rows"]}
        assert "new.event" in actions
        assert "old.event" not in actions


class TestEventsAsAuditSideEffect:
    """State-changing endpoints should write events.  We exercise a couple
    of high-value paths to ensure the wiring stays connected."""

    def _seed_one_recommendation(self) -> int:
        """Insert a minimal recommendation row directly and return its id."""
        import json
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
                "test_recommender@0.1.0",
                "ALTER_WAREHOUSE",
                "warehouse:TEST_WH:WAREHOUSE_SIZE",
                json.dumps({
                    "type": "ALTER_WAREHOUSE",
                    "warehouse_name": "TEST_WH",
                    "changes": [{
                        "knob": "WAREHOUSE_SIZE",
                        "current_value": "MEDIUM",
                        "proposed_value": "SMALL",
                    }],
                }),
                "test rationale",
                json.dumps([]),
                json.dumps({"credits_delta_daily": -1.0, "confidence": 0.9}),
                "PROPOSED",
            ],
        ).fetchone()
        return int(row[0])

    def test_accept_writes_event(self, api_client):
        rec_id = self._seed_one_recommendation()
        # No events yet
        assert api_client.get("/events?actor=user").json()["total"] == 0

        r = api_client.post(
            f"/recommendations/{rec_id}/accept",
            json={"note": "looks good"},
        )
        assert r.status_code == 200

        events = api_client.get("/events?action=recommendation.accept").json()
        assert events["total"] == 1
        event = events["rows"][0]
        assert event["subject"] == str(rec_id)
        assert event["payload"]["note"] == "looks good"
        assert event["payload"]["target_resource"] == "warehouse:TEST_WH:WAREHOUSE_SIZE"

    def test_reject_writes_event(self, api_client):
        rec_id = self._seed_one_recommendation()
        r = api_client.post(
            f"/recommendations/{rec_id}/reject",
            json={"note": "not this one"},
        )
        assert r.status_code == 200
        events = api_client.get("/events?action=recommendation.reject").json()
        assert events["total"] == 1
        assert events["rows"][0]["subject"] == str(rec_id)
