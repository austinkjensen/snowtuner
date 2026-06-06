"""Autonomous-apply runner.

Walks the open PROPOSED recommendations and applies the ones whose
``(action_type, warehouse)`` matches an enabled config row, subject to:

  * confidence ≥ configured threshold
  * cooldown: at most one apply per ``(action_type, warehouse)`` per
    ``cooldown_hours`` window
  * circuit breaker: if rollback count in the last 7 days reaches
    ``max_rollbacks_per_week``, autonomous skips the warehouse and the
    breaker is tripped (re-enable manually)
  * the action's ``supports_autonomous_apply()`` returns True
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import duckdb

from snowtuner.autonomous.applications import (
    ApplicationState,
    AutonomousApplicationStore,
)
from snowtuner.autonomous.config import AutonomousConfigStore
from snowtuner.recommendations import (
    Recommendation,
    RecommendationStatus,
    RecommendationStore,
)
from snowtuner.storage.db import naive_utcnow


@dataclass
class AutonomousDecision:
    recommendation_id: int
    action_type: str
    warehouse_name: str | None
    decision: str   # "applied" | "skipped" | "failed"
    reason: str
    application_id: int | None = None


@dataclass
class AutonomousRunReport:
    decisions: list[AutonomousDecision] = field(default_factory=list)
    skipped_reason: str | None = None  # populated when the whole run no-ops

    def applied(self) -> list[AutonomousDecision]:
        return [d for d in self.decisions if d.decision == "applied"]

    def failed(self) -> list[AutonomousDecision]:
        return [d for d in self.decisions if d.decision == "failed"]


class AutonomousRunner:
    def __init__(
        self,
        conn: duckdb.DuckDBPyConnection,
        client: Any,  # SnowflakeClient duck-typed (avoid hard import)
    ):
        self.conn = conn
        self.client = client
        self.recs = RecommendationStore(conn)
        self.config = AutonomousConfigStore(conn)
        self.apps = AutonomousApplicationStore(conn)

    def run(self) -> AutonomousRunReport:
        report = AutonomousRunReport()

        # Coordination guard: if any experiment is in RUNNING state, defer
        # autonomous apply.  Applying ALTER WAREHOUSE on a warehouse that's
        # being actively replayed against would corrupt the experiment's
        # measurements (the engine sees the new config mid-replay).  Sync
        # uses a separate connection so it doesn't have this concern; the
        # autonomous runner does, because it issues DDL on Snowflake.
        from snowtuner.experiments import ExperimentStore
        if ExperimentStore(self.conn).has_running_experiment():
            report.skipped_reason = (
                "an experiment is currently RUNNING; deferring autonomous "
                "apply to avoid corrupting in-flight measurements"
            )
            return report

        proposed = self.recs.list(status=RecommendationStatus.PROPOSED, limit=1000)
        for rec in proposed:
            decision = self._evaluate(rec)
            report.decisions.append(decision)
        # If we changed any warehouse, refresh raw.warehouses once so subsequent
        # recommender runs see the new state and don't re-emit duplicate
        # proposals before the next full sync.
        if any(d.decision == "applied" and d.warehouse_name for d in report.decisions):
            self._refresh_warehouses_snapshot()
        return report

    def _refresh_warehouses_snapshot(self) -> None:
        """Re-run SHOW WAREHOUSES and upsert into raw.warehouses.

        Cheap (single round-trip) and avoids a divergence window where
        ``raw.warehouses`` has the pre-apply values until the next sync.
        Failures here are logged but don't fail the autonomous run.
        """
        try:
            from snowtuner.ingestion.sources.warehouses import WarehousesSource
            src = WarehousesSource()
            rows = src.fetch(self.client, since=None)
            src.upsert(self.conn, rows)
        except Exception:
            # Apply already succeeded; a stale local snapshot is a less-bad
            # failure mode than re-raising and confusing the audit log.
            pass

    def _evaluate(self, rec: Recommendation) -> AutonomousDecision:
        action = rec.action
        warehouse = action.target_warehouse_name()

        def skip(reason: str) -> AutonomousDecision:
            return AutonomousDecision(
                recommendation_id=rec.id or -1,
                action_type=action.type.value,
                warehouse_name=warehouse,
                decision="skipped",
                reason=reason,
            )

        if not action.supports_autonomous_apply():
            return skip("action type does not support autonomous apply")

        # Gate on every knob the action affects.  All must resolve to an
        # enabled config row (per-knob row, or a '*' catch-all that matches);
        # if any knob is disabled or missing, the whole rec is skipped — we
        # don't split a multi-knob ALTER into separately-applied pieces.
        knobs = action.autonomous_knobs() or ["*"]
        matched: list = []
        for knob in knobs:
            knob_cfg = self.config.resolve(action.type.value, warehouse, knob)
            if knob_cfg is None or not knob_cfg.enabled:
                return skip(
                    f"autonomous not enabled for knob {knob!r} on this warehouse"
                )
            matched.append(knob_cfg)

        # When a rec touches multiple knobs, use the most restrictive cfg —
        # highest threshold, longest cooldown, smallest rollback budget — so
        # the user's strictest opt-in wins.
        cfg = max(matched, key=lambda c: c.confidence_threshold)
        cfg_cooldown = max(c.cooldown_hours for c in matched)
        cfg_max_rollbacks = min(c.max_rollbacks_per_week for c in matched)
        # The circuit can be tripped on any participating knob.
        for c in matched:
            if c.circuit_open_until and c.circuit_open_until > naive_utcnow():
                return skip(
                    f"circuit open for knob {c.knob!r} until "
                    f"{c.circuit_open_until.isoformat()}; "
                    f"reset with `snowtuner autonomous reset-circuit`"
                )

        now = naive_utcnow()

        confidence = rec.expected_impact.confidence
        if confidence < cfg.confidence_threshold:
            return skip(
                f"confidence {confidence:.2f} < threshold {cfg.confidence_threshold:.2f}"
            )

        # Cooldown: don't apply if a recent successful apply exists.  Both
        # sides of the comparison are naive UTC by convention (DuckDB strips
        # tz on bind, so we always store naive UTC and treat naive on read
        # as UTC).
        recent = self.apps.latest_apply(action.type.value, warehouse)
        if recent is not None:
            applied_at = recent.applied_at
            if applied_at.tzinfo is not None:
                applied_at = applied_at.replace(tzinfo=None)
            since = now - applied_at
            if since < timedelta(hours=cfg_cooldown):
                return skip(
                    f"in cooldown ({since} since last apply; "
                    f"window {cfg_cooldown}h)"
                )

        # Circuit-breaker: rollback budget exhausted?  Trip the circuit on
        # every participating knob so partial recovery doesn't slip through.
        recent_rollbacks = self.apps.count_recent_rollbacks(
            action.type.value, warehouse,
        )
        if recent_rollbacks >= cfg_max_rollbacks:
            for c in matched:
                self.config.trip_circuit(
                    action.type.value, warehouse or "*", knob=c.knob,
                    until=now + timedelta(days=7),
                )
            return skip(
                f"{recent_rollbacks} rollbacks in last 7 days "
                f">= max {cfg_max_rollbacks}; circuit tripped"
            )

        # Compute rollback up front so we record it even if apply succeeds-then-crashes.
        rollback_sql: str | None = None
        if hasattr(action, "rollback_sql"):
            rollback_sql = action.rollback_sql()  # type: ignore[attr-defined]

        sql = action.to_sql()
        # log_event for the autonomous apply timeline.  The full SQL + rollback
        # live in app.autonomous_applications (the canonical store); the event
        # is the queryable timeline marker.
        from snowtuner.events import log_event
        try:
            executed_sql = action.apply(self.client)
        except Exception as e:
            app_id = self.apps.record_failure(
                recommendation_id=rec.id or -1,
                action_type=action.type.value,
                warehouse_name=warehouse,
                applied_sql=sql,
                error=f"{type(e).__name__}: {e}",
            )
            log_event(
                self.conn,
                actor="autonomous",
                action="autonomous.apply",
                subject=warehouse,
                outcome="failed",
                payload={
                    "recommendation_id": rec.id,
                    "action_type": action.type.value,
                    "application_id": app_id,
                },
                error=f"{type(e).__name__}: {e}",
            )
            return AutonomousDecision(
                recommendation_id=rec.id or -1,
                action_type=action.type.value,
                warehouse_name=warehouse,
                decision="failed",
                reason=f"apply raised: {type(e).__name__}: {e}",
                application_id=app_id,
            )

        app_id = self.apps.record_apply(
            recommendation_id=rec.id or -1,
            action_type=action.type.value,
            warehouse_name=warehouse,
            applied_sql=executed_sql,
            rollback_sql=rollback_sql,
        )
        log_event(
            self.conn,
            actor="autonomous",
            action="autonomous.apply",
            subject=warehouse,
            payload={
                "recommendation_id": rec.id,
                "action_type": action.type.value,
                "application_id": app_id,
                "confidence": confidence,
                "has_rollback": rollback_sql is not None,
            },
        )
        # Promote the recommendation to APPLIED + remember the SQL/rollback.
        self.conn.execute(
            """
            UPDATE app.recommendations
            SET status = ?, applied_at = ?, applied_sql = ?, rollback_sql = ?,
                updated_at = ?
            WHERE id = ?
            """,
            [
                RecommendationStatus.APPLIED.value, now, executed_sql,
                rollback_sql, now, rec.id,
            ],
        )
        return AutonomousDecision(
            recommendation_id=rec.id or -1,
            action_type=action.type.value,
            warehouse_name=warehouse,
            decision="applied",
            reason=f"confidence {confidence:.2f} ≥ threshold {cfg.confidence_threshold:.2f}",
            application_id=app_id,
        )

    def rollback(self, application_id: int) -> AutonomousDecision:
        """Execute the recorded rollback SQL for an application.  Marks the
        application ROLLED_BACK on success.  Caller is responsible for any UI."""
        app = self.apps.get(application_id)
        if app is None:
            raise ValueError(f"no application with id={application_id}")
        if app.state != ApplicationState.APPLIED:
            raise ValueError(
                f"application {application_id} is in state {app.state.value}, "
                f"only APPLIED can be rolled back"
            )
        if not app.rollback_sql:
            raise ValueError(f"application {application_id} has no recorded rollback SQL")

        executed = app.rollback_sql
        try:
            self.client.execute(executed)
        except Exception as e:
            self.apps.mark_rolled_back(
                application_id, executed_sql=executed,
                error=f"{type(e).__name__}: {e}",
            )
            return AutonomousDecision(
                recommendation_id=app.recommendation_id,
                action_type=app.action_type,
                warehouse_name=app.warehouse_name,
                decision="failed",
                reason=f"rollback raised: {type(e).__name__}: {e}",
                application_id=app.id,
            )
        self.apps.mark_rolled_back(application_id, executed_sql=executed)
        # Flip the recommendation back to ROLLED_BACK.
        self.conn.execute(
            "UPDATE app.recommendations SET status = ?, updated_at = ? WHERE id = ?",
            [
                RecommendationStatus.ROLLED_BACK.value,
                naive_utcnow(), app.recommendation_id,
            ],
        )
        return AutonomousDecision(
            recommendation_id=app.recommendation_id,
            action_type=app.action_type,
            warehouse_name=app.warehouse_name,
            decision="applied",
            reason="rolled back",
            application_id=app.id,
        )
