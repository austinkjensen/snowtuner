"""Tests for the query-history-based WarehouseIdleGapsTransform.

The transform's correctness properties, each load-bearing for the
auto-suspend survival tuner downstream:

  * overlapping queries merge into one busy island (no phantom gaps
    inside concurrent activity)
  * metadata-only statements (execution_ms = 0) neither split gaps nor
    create islands
  * synthetic-seed rows (execution_ms NULL, total_elapsed_ms set) still
    count as compute-bearing via the COALESCE fallback
  * failed queries are excluded
  * the trailing busy island emits no gap (right-censored)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import duckdb

from snowtuner.features.library.warehouse_idle_gaps import (
    WarehouseIdleGapsTransform,
)

WH = "GAPS_TEST_WH"
T0 = datetime(2026, 6, 10, 8, 0, 0)

_QID = {"n": 0}


def _q(
    duck: duckdb.DuckDBPyConnection,
    start_offset_s: float,
    duration_s: float,
    *,
    warehouse: str = WH,
    execution_ms: int | None = 1000,
    total_elapsed_ms: int | None = 1500,
    status: str = "SUCCESS",
) -> None:
    _QID["n"] += 1
    start = T0 + timedelta(seconds=start_offset_s)
    duck.execute(
        """
        INSERT INTO raw.query_history
          (query_id, query_text, query_type, execution_status,
           user_name, warehouse_name, warehouse_size,
           start_time, end_time, total_elapsed_ms, execution_ms)
        VALUES (?, 'select 1', 'SELECT', ?, 'svc', ?, 'SMALL', ?, ?, ?, ?)
        """,
        [
            f"g-{_QID['n']:06d}", status, warehouse,
            start, start + timedelta(seconds=duration_s),
            total_elapsed_ms, execution_ms,
        ],
    )


def _run(duck: duckdb.DuckDBPyConnection):
    WarehouseIdleGapsTransform().run(duck)
    intervals = duck.execute(
        "SELECT start_time, end_time, duration_sec "
        "FROM features.warehouse_active_intervals "
        "WHERE warehouse_name = ? ORDER BY start_time",
        [WH],
    ).fetchall()
    gaps = duck.execute(
        "SELECT gap_start, gap_end, idle_seconds "
        "FROM features.warehouse_idle_gaps "
        "WHERE warehouse_name = ? ORDER BY gap_start",
        [WH],
    ).fetchall()
    return intervals, gaps


class TestIslandMerging:
    def test_overlapping_queries_one_island(self, duck: duckdb.DuckDBPyConnection):
        # Three queries overlapping/chained: [0,10], [5,15], [14,20].
        _q(duck, 0, 10)
        _q(duck, 5, 10)
        _q(duck, 14, 6)
        intervals, gaps = _run(duck)
        assert len(intervals) == 1
        start, end, dur = intervals[0]
        assert start == T0
        assert end == T0 + timedelta(seconds=20)
        assert dur == 20
        assert gaps == []

    def test_contained_query_does_not_extend(self, duck: duckdb.DuckDBPyConnection):
        # [0, 30] fully contains [5, 10]; later [100, 110] starts island 2.
        _q(duck, 0, 30)
        _q(duck, 5, 5)
        _q(duck, 100, 10)
        intervals, gaps = _run(duck)
        assert len(intervals) == 2
        assert len(gaps) == 1
        gap_start, gap_end, idle = gaps[0]
        assert gap_start == T0 + timedelta(seconds=30)
        assert gap_end == T0 + timedelta(seconds=100)
        assert idle == 70


class TestGapEmission:
    def test_two_bursts_one_gap(self, duck: duckdb.DuckDBPyConnection):
        _q(duck, 0, 20)
        _q(duck, 200, 20)
        intervals, gaps = _run(duck)
        assert len(intervals) == 2
        assert len(gaps) == 1
        assert gaps[0][2] == 180  # 200 - 20

    def test_trailing_island_right_censored(self, duck: duckdb.DuckDBPyConnection):
        # 3 bursts -> exactly 2 gaps; the open-ended tail after burst 3
        # is unknown and must not be emitted.
        for off in (0, 200, 400):
            _q(duck, off, 20)
        _, gaps = _run(duck)
        assert len(gaps) == 2


class TestComputeBearingFilter:
    def test_metadata_statement_does_not_split_gap(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        """A USE/SHOW-style statement (execution_ms=0) in the middle of an
        idle gap must not split it - it wouldn't have resumed a suspended
        warehouse."""
        _q(duck, 0, 20)
        _q(duck, 100, 1, execution_ms=0, total_elapsed_ms=5)  # metadata blip
        _q(duck, 200, 20)
        _, gaps = _run(duck)
        assert len(gaps) == 1
        assert gaps[0][2] == 180

    def test_seed_rows_with_null_execution_ms_included(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        """The synthetic seed writes execution_ms = NULL; the COALESCE
        fallback to total_elapsed_ms keeps those rows compute-bearing so
        the offline seed demo still produces gaps."""
        _q(duck, 0, 20, execution_ms=None, total_elapsed_ms=20_000)
        _q(duck, 200, 20, execution_ms=None, total_elapsed_ms=20_000)
        intervals, gaps = _run(duck)
        assert len(intervals) == 2
        assert len(gaps) == 1

    def test_failed_queries_excluded(self, duck: duckdb.DuckDBPyConnection):
        _q(duck, 0, 20)
        _q(duck, 100, 5, status="FAILED")
        _q(duck, 200, 20)
        _, gaps = _run(duck)
        assert len(gaps) == 1
        assert gaps[0][2] == 180


class TestRerunIdempotent:
    def test_second_run_replaces_not_duplicates(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        _q(duck, 0, 20)
        _q(duck, 200, 20)
        _run(duck)
        intervals, gaps = _run(duck)  # second run on same data
        assert len(intervals) == 2
        assert len(gaps) == 1
