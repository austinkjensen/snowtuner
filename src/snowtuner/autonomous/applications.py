"""Audit log of autonomous applications, plus rollback execution."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable

import duckdb

from snowtuner.storage.db import naive_utcnow


class ApplicationState(str, Enum):
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"


@dataclass
class AutonomousApplication:
    id: int
    recommendation_id: int
    action_type: str
    warehouse_name: str | None
    applied_sql: str
    rollback_sql: str | None
    applied_at: datetime
    state: ApplicationState
    error: str | None
    rolled_back_at: datetime | None
    rolled_back_sql: str | None
    rollback_error: str | None


_COLUMNS = [
    "id", "recommendation_id", "action_type", "warehouse_name",
    "applied_sql", "rollback_sql", "applied_at", "state", "error",
    "rolled_back_at", "rolled_back_sql", "rollback_error",
]


class AutonomousApplicationStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def record_apply(
        self,
        *,
        recommendation_id: int,
        action_type: str,
        warehouse_name: str | None,
        applied_sql: str,
        rollback_sql: str | None,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO app.autonomous_applications
              (recommendation_id, action_type, warehouse_name,
               applied_sql, rollback_sql, applied_at, state)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                recommendation_id, action_type, warehouse_name,
                applied_sql, rollback_sql,
                naive_utcnow(), ApplicationState.APPLIED.value,
            ],
        ).fetchone()
        return int(row[0])

    def record_failure(
        self,
        *,
        recommendation_id: int,
        action_type: str,
        warehouse_name: str | None,
        applied_sql: str,
        error: str,
    ) -> int:
        row = self.conn.execute(
            """
            INSERT INTO app.autonomous_applications
              (recommendation_id, action_type, warehouse_name,
               applied_sql, rollback_sql, applied_at, state, error)
            VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
            RETURNING id
            """,
            [
                recommendation_id, action_type, warehouse_name, applied_sql,
                naive_utcnow(), ApplicationState.FAILED.value, error,
            ],
        ).fetchone()
        return int(row[0])

    def list(
        self,
        *,
        warehouse_name: str | None = None,
        action_type: str | None = None,
        state: ApplicationState | None = None,
        limit: int = 100,
    ) -> list[AutonomousApplication]:
        where, params = [], []
        if warehouse_name is not None:
            where.append("warehouse_name = ?")
            params.append(warehouse_name.upper())
        if action_type is not None:
            where.append("action_type = ?")
            params.append(action_type)
        if state is not None:
            where.append("state = ?")
            params.append(state.value)
        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.autonomous_applications "
            f"{where_sql} ORDER BY applied_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_app(r) for r in rows]

    def get(self, application_id: int) -> AutonomousApplication | None:
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.autonomous_applications "
            f"WHERE id = ?",
            [application_id],
        ).fetchone()
        return _row_to_app(row) if row else None

    def latest_apply(
        self, action_type: str, warehouse_name: str | None,
    ) -> AutonomousApplication | None:
        if warehouse_name is None:
            return None
        row = self.conn.execute(
            f"""
            SELECT {', '.join(_COLUMNS)} FROM app.autonomous_applications
            WHERE action_type = ? AND warehouse_name = ?
              AND state = ?
            ORDER BY applied_at DESC LIMIT 1
            """,
            [action_type, warehouse_name.upper(), ApplicationState.APPLIED.value],
        ).fetchone()
        return _row_to_app(row) if row else None

    def count_recent_rollbacks(
        self, action_type: str, warehouse_name: str | None,
        *, within: timedelta = timedelta(days=7),
    ) -> int:
        if warehouse_name is None:
            return 0
        cutoff = naive_utcnow() - within
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM app.autonomous_applications
            WHERE action_type = ? AND warehouse_name = ?
              AND state = ?
              AND rolled_back_at >= ?
            """,
            [
                action_type, warehouse_name.upper(),
                ApplicationState.ROLLED_BACK.value, cutoff,
            ],
        ).fetchone()
        return int(row[0]) if row else 0

    def mark_rolled_back(
        self, application_id: int, *, executed_sql: str,
        error: str | None = None,
    ) -> None:
        now = naive_utcnow()
        self.conn.execute(
            """
            UPDATE app.autonomous_applications
            SET state = ?, rolled_back_at = ?, rolled_back_sql = ?, rollback_error = ?
            WHERE id = ?
            """,
            [
                ApplicationState.ROLLED_BACK.value if error is None
                else ApplicationState.APPLIED.value,
                now, executed_sql, error, application_id,
            ],
        )


def _row_to_app(row: Iterable | None) -> AutonomousApplication | None:
    if row is None:
        return None
    (
        id_, rec_id, action_type, warehouse_name, applied_sql, rollback_sql,
        applied_at, state, error, rolled_back_at, rolled_back_sql, rollback_error,
    ) = row
    return AutonomousApplication(
        id=int(id_),
        recommendation_id=int(rec_id),
        action_type=action_type,
        warehouse_name=warehouse_name,
        applied_sql=applied_sql,
        rollback_sql=rollback_sql,
        applied_at=applied_at,
        state=ApplicationState(state),
        error=error,
        rolled_back_at=rolled_back_at,
        rolled_back_sql=rolled_back_sql,
        rollback_error=rollback_error,
    )
