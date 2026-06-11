"""Regression tests for the auto-suspend survival tuner's event handling.

Production bug (dogfood 2026-06-11): a fresh Snowflake account recorded
suspend/resume activity ONLY as SUSPEND_CLUSTER / RESUME_CLUSTER, while
every consumer filtered on the legacy SUSPEND_WAREHOUSE / RESUME_WAREHOUSE
names - so the recommender saw zero events and never fired.  The mismatch
survived testing because the synthetic seed emits the legacy names: the
recommender was validated against data generated to match its own filter.

These tests drive the recommender end-to-end (gate -> fit -> predict)
against an in-memory DuckDB under BOTH vocabularies, plus the
STARTED/COMPLETED duplicate-row shape the modern view produces.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from snowtuner.actions import WarehouseKnob
from snowtuner.recommenders.builtins.auto_suspend_survival import (
    AutoSuspendSurvivalTuner,
)

WH = "SNOWTUNER_DEMO_BURSTY_WH"


def _insert_warehouse(
    duck: duckdb.DuckDBPyConnection,
    *,
    name: str = WH,
    auto_suspend: int = 120,
    size: str = "SMALL",
) -> None:
    duck.execute(
        """
        INSERT INTO raw.warehouses
          (name, size, min_cluster_count, max_cluster_count,
           auto_suspend_seconds, auto_resume, scaling_policy, state, comment)
        VALUES (?, ?, 1, 1, ?, TRUE, 'STANDARD', 'SUSPENDED', NULL)
        """,
        [name, size, auto_suspend],
    )


def _insert_cycles(
    duck: duckdb.DuckDBPyConnection,
    *,
    n_cycles: int,
    suspend_name: str,
    resume_name: str,
    gap_seconds: float = 180.0,
    warehouse: str = WH,
    duplicate_states: bool = False,
) -> None:
    """Write n_cycles of suspend->resume event pairs into raw events.

    ``duplicate_states=True`` mimics the modern view's STARTED+COMPLETED
    double rows: each event appears twice, 1s apart.
    """
    t = datetime(2026, 6, 10, 8, 0, 0)
    event_id = 0
    for _ in range(n_cycles):
        suspend_rows = [(suspend_name, t, "STARTED" if duplicate_states else "COMPLETED")]
        if duplicate_states:
            suspend_rows.append((suspend_name, t + timedelta(seconds=1), "COMPLETED"))
        resume_t = t + timedelta(seconds=gap_seconds)
        resume_rows = [(resume_name, resume_t, "STARTED" if duplicate_states else "COMPLETED")]
        if duplicate_states:
            resume_rows.append((resume_name, resume_t + timedelta(seconds=1), "COMPLETED"))

        for name, ts, state in suspend_rows + resume_rows:
            event_id += 1
            duck.execute(
                """
                INSERT INTO raw.warehouse_events_history
                  (event_id, timestamp, warehouse_id, warehouse_name,
                   cluster_number, event_name, event_state, size)
                VALUES (?, ?, 'wh-id', ?, 1, ?, ?, 'SMALL')
                """,
                [event_id + hash(warehouse) % 10_000_000, ts, warehouse, name, state],
            )
        # Next burst ~30s after the resume.
        t = resume_t + timedelta(seconds=30)


def _run_recommender(duck: duckdb.DuckDBPyConnection):
    tuner = AutoSuspendSurvivalTuner()
    state = tuner.fit(duck)
    return tuner, state, tuner.predict(duck, state)


class TestClusterVocabulary:
    """The production shape: *_CLUSTER names only."""

    def test_gate_ready_with_cluster_events(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        _insert_cycles(
            duck, n_cycles=12,
            suspend_name="SUSPEND_CLUSTER", resume_name="RESUME_CLUSTER",
        )
        tuner = AutoSuspendSurvivalTuner()
        report = tuner.training_gate.evaluate(duck)
        assert report.is_ready, report.reason

    def test_recommendation_fires(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck, auto_suspend=120)
        _insert_cycles(
            duck, n_cycles=12,
            suspend_name="SUSPEND_CLUSTER", resume_name="RESUME_CLUSTER",
            gap_seconds=180.0,
        )
        _, state, recs = _run_recommender(duck)

        assert WH in state["per_warehouse"], (
            "fit must produce gaps from cluster-vocabulary events"
        )
        assert state["per_warehouse"][WH]["n"] == 12

        assert len(recs) == 1
        change = recs[0].action.changes[0]
        assert change.knob == WarehouseKnob.AUTO_SUSPEND
        # Constant 180s gaps with SMALL's cold-start cost: the cost model's
        # optimum is the grid floor (60s).  Current 120 -> delta 60 >= 30.
        assert change.proposed_value == 60
        assert change.current_value == 120


class TestLegacyVocabulary:
    """The synthetic seed's shape: *_WAREHOUSE names.  Must keep working."""

    def test_recommendation_fires(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck, auto_suspend=120)
        _insert_cycles(
            duck, n_cycles=12,
            suspend_name="SUSPEND_WAREHOUSE", resume_name="RESUME_WAREHOUSE",
            gap_seconds=180.0,
        )
        _, state, recs = _run_recommender(duck)
        assert state["per_warehouse"][WH]["n"] == 12
        assert len(recs) == 1
        assert recs[0].action.changes[0].knob == WarehouseKnob.AUTO_SUSPEND


class TestStartedCompletedDuplicates:
    """Modern view emits STARTED+COMPLETED rows per event.  The kind-based
    pairing collapses consecutive same-kind rows, so each cycle still
    yields exactly one reactivation gap."""

    def test_duplicates_collapse_to_one_gap_per_cycle(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        _insert_warehouse(duck, auto_suspend=120)
        _insert_cycles(
            duck, n_cycles=12,
            suspend_name="SUSPEND_CLUSTER", resume_name="RESUME_CLUSTER",
            gap_seconds=180.0, duplicate_states=True,
        )
        _, state, recs = _run_recommender(duck)
        # 12 cycles x 4 rows each, but exactly 12 gaps - not 24, not 6.
        assert state["per_warehouse"][WH]["n"] == 12
        assert len(recs) == 1


class TestBelowMinCycles:
    def test_too_few_cycles_no_rec(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        _insert_cycles(
            duck, n_cycles=5,
            suspend_name="SUSPEND_CLUSTER", resume_name="RESUME_CLUSTER",
        )
        _, state, recs = _run_recommender(duck)
        assert WH not in state["per_warehouse"]
        assert recs == []
