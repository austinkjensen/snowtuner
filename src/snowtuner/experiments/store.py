"""Persistence for Experiments (DuckDB-backed).

Mirrors the role ``RecommendationStore`` plays for recommendations.  The
``ProposedExperiment`` spec and the final ``ExperimentReport`` are stored as
JSON blobs (the engine roundtrips them via Pydantic) — only the columns we
actually filter or aggregate by are kept first-class.

Per-(arm, query, rep) observations live in ``app.experiment_runs`` and are
written as the engine completes each replay.  Storing them individually
(rather than only the aggregates on the report) means we can re-aggregate
under different exclusion rules without re-running the experiment.
"""
from __future__ import annotations

import json
from datetime import datetime

import duckdb

from snowtuner.experiments.model import (
    Experiment,
    ExperimentReport,
    ExperimentRun,
    ExperimentStatus,
    ProposedExperiment,
    RunStatus,
)
from snowtuner.storage.db import naive_utcnow


_EXP_COLUMNS = [
    "id", "recipe_name", "target_warehouse", "hypothesis", "proposed_by",
    "status", "spec", "cost_estimate", "proposed_at", "accepted_at",
    "started_at", "completed_at", "aborted_reason", "actual_cost_credits",
    "cost_cap_hit", "report", "derived_recommendation_id", "test_warehouses",
    "test_warehouses_cleaned",
]


_RUN_COLUMNS = [
    "experiment_id", "arm_name", "rep_index", "sampled_query_id",
    "parameterized_hash", "replay_query_id", "elapsed_ms",
    "queued_overload_ms", "bytes_scanned", "bytes_spilled_local",
    "bytes_spilled_remote", "credits_used_estimate", "status",
    "error_message", "started_at", "completed_at",
]


class ExperimentStore:
    def __init__(self, conn: duckdb.DuckDBPyConnection):
        self.conn = conn

    # ── ProposedExperiment → row ─────────────────────────────────────

    def insert(self, proposed: ProposedExperiment) -> int:
        """Persist a new ``ProposedExperiment`` and return its assigned id."""
        spec = proposed.model_dump_json()
        cost = proposed.cost_estimate.model_dump_json()
        row = self.conn.execute(
            """
            INSERT INTO app.experiments
              (recipe_name, target_warehouse, hypothesis, proposed_by,
               status, spec, cost_estimate)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            [
                proposed.recipe_name,
                proposed.target_warehouse,
                proposed.hypothesis,
                proposed.proposed_by,
                ExperimentStatus.PROPOSED.value,
                spec,
                cost,
            ],
        ).fetchone()
        return int(row[0])

    # ── reads ────────────────────────────────────────────────────────

    def get(self, experiment_id: int) -> Experiment | None:
        cols = ", ".join(_EXP_COLUMNS)
        row = self.conn.execute(
            f"SELECT {cols} FROM app.experiments WHERE id = ?",
            [experiment_id],
        ).fetchone()
        if not row:
            return None
        return self._hydrate(dict(zip(_EXP_COLUMNS, row)))

    def list(
        self,
        *,
        status: ExperimentStatus | None = None,
        target_warehouse: str | None = None,
        limit: int = 100,
    ) -> list[Experiment]:
        where: list[str] = []
        params: list = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if target_warehouse is not None:
            where.append("target_warehouse = ?")
            params.append(target_warehouse.upper())
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        cols = ", ".join(_EXP_COLUMNS)
        rows = self.conn.execute(
            f"SELECT {cols} FROM app.experiments {where_sql} "
            f"ORDER BY proposed_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._hydrate(dict(zip(_EXP_COLUMNS, r))) for r in rows]

    # ── lifecycle transitions ────────────────────────────────────────

    def set_status(
        self,
        experiment_id: int,
        status: ExperimentStatus,
        *,
        aborted_reason: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        """Move an experiment to a new status, stamping the appropriate
        lifecycle column.

        Status → column written:
          ACCEPTED → accepted_at
          RUNNING  → started_at
          COMPLETED → completed_at
          ABORTED / FAILED → completed_at  (+ aborted_reason if provided)
          REJECTED → (no timestamp column — terminal-from-proposed)
        """
        now = timestamp or naive_utcnow()
        timestamp_col = {
            ExperimentStatus.ACCEPTED: "accepted_at",
            ExperimentStatus.RUNNING: "started_at",
            ExperimentStatus.COMPLETED: "completed_at",
            ExperimentStatus.ABORTED: "completed_at",
            ExperimentStatus.FAILED: "completed_at",
        }.get(status)

        if timestamp_col is None:
            self.conn.execute(
                "UPDATE app.experiments SET status = ? WHERE id = ?",
                [status.value, experiment_id],
            )
            return

        if aborted_reason is not None:
            self.conn.execute(
                f"""
                UPDATE app.experiments
                SET status = ?, {timestamp_col} = ?, aborted_reason = ?
                WHERE id = ?
                """,
                [status.value, now, aborted_reason, experiment_id],
            )
        else:
            self.conn.execute(
                f"""
                UPDATE app.experiments
                SET status = ?, {timestamp_col} = ?
                WHERE id = ?
                """,
                [status.value, now, experiment_id],
            )

    def set_test_warehouses(
        self, experiment_id: int, names: list[str],
    ) -> None:
        """Record the side-by-side warehouse names the engine created.
        Persisted so we can always clean up — even after a process crash."""
        self.conn.execute(
            "UPDATE app.experiments SET test_warehouses = ? WHERE id = ?",
            [json.dumps(names), experiment_id],
        )

    def mark_test_warehouses_cleaned(
        self, experiment_id: int, cleaned: bool = True,
    ) -> None:
        self.conn.execute(
            """
            UPDATE app.experiments
            SET test_warehouses_cleaned = ?
            WHERE id = ?
            """,
            [cleaned, experiment_id],
        )

    def set_actual_cost(
        self,
        experiment_id: int,
        actual_cost_credits: float,
        *,
        cost_cap_hit: bool = False,
    ) -> None:
        """Persist the running cost (called from the engine's polling loop)."""
        self.conn.execute(
            """
            UPDATE app.experiments
            SET actual_cost_credits = ?, cost_cap_hit = ?
            WHERE id = ?
            """,
            [actual_cost_credits, cost_cap_hit, experiment_id],
        )

    def set_report(
        self, experiment_id: int, report: ExperimentReport,
    ) -> None:
        self.conn.execute(
            "UPDATE app.experiments SET report = ? WHERE id = ?",
            [report.model_dump_json(), experiment_id],
        )

    def set_derived_recommendation_id(
        self, experiment_id: int, recommendation_id: int,
    ) -> None:
        self.conn.execute(
            """
            UPDATE app.experiments
            SET derived_recommendation_id = ?
            WHERE id = ?
            """,
            [recommendation_id, experiment_id],
        )

    # ── invariants ───────────────────────────────────────────────────

    def has_running_experiment(self) -> bool:
        """Single-experiment-at-a-time guard.

        v0.2 keeps this conservative: only ever one ``RUNNING`` (or
        ``ACCEPTED`` waiting to start) experiment at a time.  The engine
        consults this before transitioning to RUNNING.
        """
        row = self.conn.execute(
            """
            SELECT COUNT(*) FROM app.experiments
            WHERE status IN (?, ?)
            """,
            [ExperimentStatus.ACCEPTED.value, ExperimentStatus.RUNNING.value],
        ).fetchone()
        return bool(row and row[0])

    def needing_cleanup(self) -> list[Experiment]:
        """Experiments whose test warehouses haven't been torn down yet.

        Called at engine startup to recover from a crash mid-run: any
        completed/aborted/failed experiment with ``test_warehouses_cleaned = FALSE``
        and a non-empty ``test_warehouses`` list is a janitorial backlog item.
        """
        cols = ", ".join(_EXP_COLUMNS)
        rows = self.conn.execute(
            f"""
            SELECT {cols} FROM app.experiments
            WHERE test_warehouses_cleaned = FALSE
              AND test_warehouses IS NOT NULL
              AND test_warehouses != '[]'
              AND status IN (?, ?, ?)
            """,
            [
                ExperimentStatus.COMPLETED.value,
                ExperimentStatus.ABORTED.value,
                ExperimentStatus.FAILED.value,
            ],
        ).fetchall()
        return [self._hydrate(dict(zip(_EXP_COLUMNS, r))) for r in rows]

    # ── per-run rows ─────────────────────────────────────────────────

    def record_run(self, run: ExperimentRun) -> None:
        """Insert a single (arm, query, rep) observation.

        The PK is composite; the engine schedules so no duplicates occur, but
        we use INSERT (not UPSERT) — a duplicate is a bug worth surfacing.
        """
        self.conn.execute(
            """
            INSERT INTO app.experiment_runs
              (experiment_id, arm_name, rep_index, sampled_query_id,
               parameterized_hash, replay_query_id, elapsed_ms,
               queued_overload_ms, bytes_scanned, bytes_spilled_local,
               bytes_spilled_remote, credits_used_estimate, status,
               error_message, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                run.experiment_id, run.arm_name, run.rep_index,
                run.sampled_query_id, run.parameterized_hash,
                run.replay_query_id, run.elapsed_ms, run.queued_overload_ms,
                run.bytes_scanned, run.bytes_spilled_local,
                run.bytes_spilled_remote, run.credits_used_estimate,
                run.status.value, run.error_message,
                run.started_at, run.completed_at,
            ],
        )

    def runs_for(
        self,
        experiment_id: int,
        *,
        arm_name: str | None = None,
        status: RunStatus | None = None,
    ) -> list[ExperimentRun]:
        cols = ", ".join(_RUN_COLUMNS)
        where = ["experiment_id = ?"]
        params: list = [experiment_id]
        if arm_name is not None:
            where.append("arm_name = ?")
            params.append(arm_name)
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        rows = self.conn.execute(
            f"""
            SELECT {cols} FROM app.experiment_runs
            WHERE {' AND '.join(where)}
            ORDER BY arm_name, rep_index, sampled_query_id
            """,
            params,
        ).fetchall()
        return [
            ExperimentRun(**{
                **dict(zip(_RUN_COLUMNS, r)),
                "status": RunStatus(r[_RUN_COLUMNS.index("status")]),
            })
            for r in rows
        ]

    # ── internals ────────────────────────────────────────────────────

    def _hydrate(self, row: dict) -> Experiment:
        """Rebuild an Experiment domain object from a DB row.

        The ``spec`` and ``report`` columns are JSON-encoded Pydantic models;
        ``test_warehouses`` is a JSON list of strings.  DuckDB returns JSON
        columns as Python strings, so we parse here.
        """
        proposed = ProposedExperiment.model_validate_json(row["spec"])
        report = (
            ExperimentReport.model_validate_json(row["report"])
            if row["report"]
            else None
        )
        test_warehouses = (
            json.loads(row["test_warehouses"])
            if row["test_warehouses"]
            else []
        )
        return Experiment(
            id=int(row["id"]),
            proposed=proposed,
            status=ExperimentStatus(row["status"]),
            proposed_at=row["proposed_at"],
            accepted_at=row["accepted_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            aborted_reason=row["aborted_reason"],
            actual_cost_credits=row["actual_cost_credits"],
            cost_cap_hit=bool(row["cost_cap_hit"]) if row["cost_cap_hit"] is not None else False,
            report=report,
            derived_recommendation_id=row["derived_recommendation_id"],
            test_warehouse_names=test_warehouses,
            test_warehouses_cleaned=bool(row["test_warehouses_cleaned"]) if row["test_warehouses_cleaned"] is not None else False,
        )
