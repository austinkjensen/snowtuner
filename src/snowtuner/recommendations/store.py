"""Persistence for Recommendations (DuckDB-backed)."""
from __future__ import annotations

import json

import duckdb

from snowtuner.recommendations.model import (
    Recommendation,
    RecommendationStatus,
)
from snowtuner.storage.db import naive_utcnow


_COLUMNS = [
    "id", "generated_by", "action_type", "target_resource", "action_payload",
    "rationale", "evidence", "expected_impact", "status", "apply_plan",
    "created_at", "updated_at", "applied_at", "applied_sql", "rollback_sql",
    "superseded_by", "notes",
]


class RecommendationStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def insert(self, rec: Recommendation) -> int:
        payload = json.dumps(rec.action.to_payload())
        evidence = json.dumps([e.model_dump(mode="json") for e in rec.evidence])
        impact = json.dumps(rec.expected_impact.model_dump(mode="json"))
        rollback = None
        if hasattr(rec.action, "rollback_sql"):
            rollback = rec.action.rollback_sql()  # type: ignore[attr-defined]
        row = self.conn.execute(
            """
            INSERT INTO app.recommendations
              (generated_by, action_type, target_resource, action_payload,
               rationale, evidence, expected_impact, status, rollback_sql)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                rec.generated_by,
                rec.action.type.value,
                rec.action.target_resource(),
                payload,
                rec.rationale,
                evidence,
                impact,
                rec.status.value,
                rollback,
            ],
        ).fetchone()
        return int(row[0])

    def list(
        self,
        status: RecommendationStatus | None = None,
        action_type: str | None = None,
        limit: int = 100,
    ) -> list[Recommendation]:
        where = []
        params: list = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if action_type is not None:
            where.append("action_type = ?")
            params.append(action_type)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        cols = ", ".join(_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {cols} FROM app.recommendations {where_sql} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [Recommendation.from_row(dict(zip(_COLUMNS, r))) for r in rows]

    def get(self, rec_id: int) -> Recommendation | None:
        cols = ", ".join(_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM app.recommendations WHERE id = ?",
            [rec_id],
        ).fetchone()
        if not row:
            return None
        return Recommendation.from_row(dict(zip(_COLUMNS, row)))

    def set_status(
        self,
        rec_id: int,
        status: RecommendationStatus,
        notes: str | None = None,
    ) -> None:
        now = naive_utcnow()
        self.conn.execute(
            """
            UPDATE app.recommendations
            SET status = ?, updated_at = ?, notes = COALESCE(?, notes)
            WHERE id = ?
            """,
            [status.value, now, notes, rec_id],
        )

    def supersede_all_from(
        self,
        generated_by: str,
        action_type: str,
    ) -> None:
        """Mark every PROPOSED recommendation from this (recommender, action_type) as SUPERSEDED.
        Called before a recommender re-emits, so stale proposals from prior runs don't linger."""
        now = naive_utcnow()
        self.conn.execute(
            """
            UPDATE app.recommendations
            SET status = ?, updated_at = ?
            WHERE status = ? AND generated_by = ? AND action_type = ?
            """,
            [
                RecommendationStatus.SUPERSEDED.value, now,
                RecommendationStatus.PROPOSED.value, generated_by, action_type,
            ],
        )

    def supersede_overlapping(
        self,
        target_resource: str,
        action_type: str,
        except_id: int | None = None,
    ) -> None:
        """Mark any prior PROPOSED recommendations targeting the same resource as SUPERSEDED."""
        now = naive_utcnow()
        if except_id is None:
            self.conn.execute(
                """
                UPDATE app.recommendations
                SET status = ?, updated_at = ?
                WHERE status = ? AND target_resource = ? AND action_type = ?
                """,
                [
                    RecommendationStatus.SUPERSEDED.value, now,
                    RecommendationStatus.PROPOSED.value, target_resource, action_type,
                ],
            )
        else:
            self.conn.execute(
                """
                UPDATE app.recommendations
                SET status = ?, updated_at = ?, superseded_by = ?
                WHERE status = ? AND target_resource = ? AND action_type = ? AND id != ?
                """,
                [
                    RecommendationStatus.SUPERSEDED.value, now, except_id,
                    RecommendationStatus.PROPOSED.value, target_resource, action_type, except_id,
                ],
            )
