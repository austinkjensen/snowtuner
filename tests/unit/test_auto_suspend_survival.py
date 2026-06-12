"""Tests for the gap-based auto-suspend survival tuner.

History, because this recommender has been bitten twice:

* 2026-06-11 round 1: a fresh Snowflake account recorded suspend/resume
  events ONLY under the *_CLUSTER vocabulary while every consumer filtered
  on the legacy *_WAREHOUSE names - zero events seen, no rec.
* 2026-06-11 round 2 (the structural fix): the events-based measurement
  was the wrong observable anyway.  T = suspend->resume is G - AS0,
  censored at G > AS0: a warehouse whose AUTO_SUSPEND sits far above its
  real gaps never suspends, produces no events, and was invisible - the
  exact warehouses that most need tuning.  The tuner now reads true idle
  gaps from features.warehouse_idle_gaps (query-history derived); events
  only enrich the cold-start cost C.

The tests drive the REAL pipeline: raw.query_history -> the
WarehouseIdleGapsTransform -> gate -> fit -> predict.  No rows are
hand-inserted into the feature table, so the transform's busy-island
merging is exercised on every path.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from snowtuner.actions import WarehouseKnob
from snowtuner.features.library.warehouse_idle_gaps import (
    WarehouseIdleGapsTransform,
)
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


_QID = {"n": 0}


def _insert_query(
    duck: duckdb.DuckDBPyConnection,
    warehouse: str,
    start: datetime,
    *,
    duration_s: float = 5.0,
    execution_ms: int | None = 4000,
) -> None:
    _QID["n"] += 1
    duck.execute(
        """
        INSERT INTO raw.query_history
          (query_id, query_text, query_type, execution_status,
           user_name, warehouse_name, warehouse_size,
           start_time, end_time, total_elapsed_ms, execution_ms)
        VALUES (?, 'select 1', 'SELECT', 'SUCCESS', 'svc', ?, 'SMALL',
                ?, ?, ?, ?)
        """,
        [
            f"q-{_QID['n']:06d}", warehouse,
            start, start + timedelta(seconds=duration_s),
            int(duration_s * 1000), execution_ms,
        ],
    )


def _insert_bursts(
    duck: duckdb.DuckDBPyConnection,
    *,
    n_bursts: int,
    gap_seconds: float,
    warehouse: str = WH,
    queries_per_burst: int = 3,
    busy_seconds: float = 20.0,
) -> None:
    """Write n_bursts busy periods separated by gap_seconds of silence.

    Each burst is queries_per_burst overlapping-ish queries spanning
    busy_seconds total.  n_bursts bursts produce n_bursts - 1 idle gaps.
    """
    t = datetime(2026, 6, 10, 8, 0, 0)
    per_q = busy_seconds / queries_per_burst
    for _ in range(n_bursts):
        for i in range(queries_per_burst):
            _insert_query(
                duck, warehouse,
                t + timedelta(seconds=i * per_q),
                duration_s=per_q + 1,  # +1s overlap into the next query
            )
        t = t + timedelta(seconds=busy_seconds + gap_seconds)


def _insert_resume_pairs(
    duck: duckdb.DuckDBPyConnection,
    *,
    n_pairs: int,
    resume_name: str,
    duration_s: float = 8.0,
    warehouse: str = WH,
) -> None:
    """Write RESUME STARTED->COMPLETED event pairs for C-measurement."""
    t = datetime(2026, 6, 10, 8, 0, 0)
    eid = hash(warehouse) % 10_000_000
    for _ in range(n_pairs):
        for state, offset in (("STARTED", 0.0), ("COMPLETED", duration_s)):
            eid += 1
            duck.execute(
                """
                INSERT INTO raw.warehouse_events_history
                  (event_id, timestamp, warehouse_id, warehouse_name,
                   cluster_number, event_name, event_state, size)
                VALUES (?, ?, 'wh-id', ?, 1, ?, ?, 'SMALL')
                """,
                [eid, t + timedelta(seconds=offset), warehouse,
                 resume_name, state],
            )
        t += timedelta(minutes=10)


def _run_pipeline(duck: duckdb.DuckDBPyConnection):
    WarehouseIdleGapsTransform().run(duck)
    tuner = AutoSuspendSurvivalTuner()
    state = tuner.fit(duck)
    return tuner, state, tuner.predict(duck, state)


class TestGapBasedRecommendation:
    """The happy path: bursty query history alone produces the rec."""

    def test_gate_ready_from_query_gaps(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        WarehouseIdleGapsTransform().run(duck)
        report = AutoSuspendSurvivalTuner().training_gate.evaluate(duck)
        assert report.is_ready, report.reason

    def test_recommendation_fires(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck, auto_suspend=120)
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        _, state, recs = _run_pipeline(duck)

        assert WH in state["per_warehouse"]
        # 13 bursts -> 12 gaps (the trailing burst is right-censored).
        assert state["per_warehouse"][WH]["n"] == 12

        assert len(recs) == 1
        change = recs[0].action.changes[0]
        assert change.knob == WarehouseKnob.AUTO_SUSPEND
        # Constant ~180s gaps with SMALL's default cold-start cost: the
        # cost model's optimum is the grid floor (60s).  Current 120 ->
        # delta 60 >= MIN_DELTA_SECONDS.
        assert change.proposed_value == 60
        assert change.current_value == 120


class TestCensoringFixed:
    """THE headline regression: a warehouse that never suspends.

    Under the events-based model this warehouse produced zero events and
    was invisible - despite being the best possible tuning candidate
    (AUTO_SUSPEND=600 against ~2-minute real gaps burns ~10 minutes of
    idle billing per gap).  Under the gap-based model it gets a rec from
    query history alone.
    """

    def test_never_suspended_warehouse_gets_rec(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        _insert_warehouse(duck, auto_suspend=600)
        _insert_bursts(duck, n_bursts=13, gap_seconds=120.0)

        # The point: NOT ONE suspend/resume event exists.
        n_events = duck.execute(
            "SELECT COUNT(*) FROM raw.warehouse_events_history"
        ).fetchone()[0]
        assert n_events == 0

        _, state, recs = _run_pipeline(duck)
        assert len(recs) == 1
        change = recs[0].action.changes[0]
        assert change.knob == WarehouseKnob.AUTO_SUSPEND
        assert change.current_value == 600
        assert change.proposed_value < 600


class TestSubFloorGapsExcluded:
    """Gaps below the grid floor (60s) are optimization-inert: no candidate
    AUTO_SUSPEND can suspend inside them.  They must not count toward
    readiness or inflate the fit."""

    def test_short_gaps_produce_no_rec(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        _insert_bursts(duck, n_bursts=20, gap_seconds=45.0)
        _, state, recs = _run_pipeline(duck)
        assert state["per_warehouse"] == {}
        assert recs == []
        report = AutoSuspendSurvivalTuner().training_gate.evaluate(duck)
        assert not report.is_ready


class TestBelowMinGaps:
    def test_too_few_gaps_no_rec(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        # 5 bursts -> 4 gaps, below MIN_CYCLES_PER_WAREHOUSE=10.
        _insert_bursts(duck, n_bursts=5, gap_seconds=180.0)
        _, state, recs = _run_pipeline(duck)
        assert WH not in state["per_warehouse"]
        assert recs == []


class TestColdStartEnrichment:
    """Events demoted to C-enrichment: measured when resume pairs exist
    (under EITHER vocabulary - the round-1 regression), per-size default
    otherwise."""

    def test_no_events_uses_default(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck, size="SMALL")
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        _, state, _ = _run_pipeline(duck)
        fit = state["per_warehouse"][WH]
        assert fit["cold_start_cost_source"] == "default"
        assert fit["cold_start_cost_seconds"] == 10  # SMALL's default

    def test_cluster_vocab_resume_pairs_measured(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        _insert_warehouse(duck)
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        _insert_resume_pairs(
            duck, n_pairs=5, resume_name="RESUME_CLUSTER", duration_s=8.0,
        )
        _, state, _ = _run_pipeline(duck)
        fit = state["per_warehouse"][WH]
        assert fit["cold_start_cost_source"] == "measured"
        # C = p95 resume (~8s) + billing floor (60 - ~21s median busy ≈ 39).
        # Exact value depends on island durations; pin the band, not the
        # decimal.
        assert 30 <= fit["cold_start_cost_seconds"] <= 60
        assert fit["cold_start_cost_detail"]["resume_samples"] == 5

    def test_legacy_vocab_resume_pairs_measured(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        _insert_warehouse(duck)
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        _insert_resume_pairs(
            duck, n_pairs=5, resume_name="RESUME_WAREHOUSE", duration_s=8.0,
        )
        _, state, _ = _run_pipeline(duck)
        assert state["per_warehouse"][WH]["cold_start_cost_source"] == "measured"

    def test_too_few_pairs_falls_back(self, duck: duckdb.DuckDBPyConnection):
        _insert_warehouse(duck)
        _insert_bursts(duck, n_bursts=13, gap_seconds=180.0)
        _insert_resume_pairs(duck, n_pairs=2, resume_name="RESUME_CLUSTER")
        _, state, _ = _run_pipeline(duck)
        fit = state["per_warehouse"][WH]
        assert fit["cold_start_cost_source"] == "default"
        assert fit["cold_start_cost_detail"]["resume_samples"] == 2
