"""Per-recommender training state store (app.training_state)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import duckdb

from snowtuner.storage.db import naive_utcnow


@dataclass
class TrainingRecord:
    recommender_name: str
    is_ready: bool
    readiness_report: dict[str, Any] | None
    model_state: dict[str, Any] | None
    last_fit_at: datetime | None
    last_predict_at: datetime | None


class TrainingStateStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    def get(self, name: str) -> TrainingRecord | None:
        row = self.conn.execute(
            """
            SELECT recommender_name, is_ready, readiness_report,
                   model_state, last_fit_at, last_predict_at
            FROM app.training_state WHERE recommender_name = ?
            """,
            [name],
        ).fetchone()
        if row is None:
            return None
        return TrainingRecord(
            recommender_name=row[0],
            is_ready=bool(row[1]),
            readiness_report=_loads(row[2]),
            model_state=_loads(row[3]),
            last_fit_at=row[4],
            last_predict_at=row[5],
        )

    def upsert(
        self,
        name: str,
        *,
        is_ready: bool | None = None,
        readiness_report: dict[str, Any] | None = None,
        model_state: dict[str, Any] | None = None,
        fit_now: bool = False,
        predict_now: bool = False,
    ) -> None:
        now = naive_utcnow()
        existing = self.get(name)
        rec = existing or TrainingRecord(
            recommender_name=name,
            is_ready=False,
            readiness_report=None,
            model_state=None,
            last_fit_at=None,
            last_predict_at=None,
        )
        if is_ready is not None:
            rec.is_ready = is_ready
        if readiness_report is not None:
            rec.readiness_report = readiness_report
        if model_state is not None:
            rec.model_state = model_state
        if fit_now:
            rec.last_fit_at = now
        if predict_now:
            rec.last_predict_at = now

        self.conn.execute(
            """
            INSERT INTO app.training_state
              (recommender_name, is_ready, readiness_report, model_state,
               last_fit_at, last_predict_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (recommender_name) DO UPDATE SET
              is_ready = excluded.is_ready,
              readiness_report = excluded.readiness_report,
              model_state = excluded.model_state,
              last_fit_at = excluded.last_fit_at,
              last_predict_at = excluded.last_predict_at,
              updated_at = excluded.updated_at
            """,
            [
                name, rec.is_ready,
                json.dumps(rec.readiness_report) if rec.readiness_report is not None else None,
                json.dumps(rec.model_state) if rec.model_state is not None else None,
                rec.last_fit_at, rec.last_predict_at, now,
            ],
        )


def _loads(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return None
    return v
