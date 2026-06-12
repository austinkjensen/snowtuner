"""Compute per-warehouse idle gaps from query history alone.

The idle gap G - the time between one busy period ending and the next
compute-bearing query arriving - is the decision variable AUTO_SUSPEND
tuning actually optimizes: billed idle is ``min(G, AS)`` and a cold start
is paid iff ``G > AS``.

Earlier versions anchored on WAREHOUSE_EVENTS_HISTORY suspend events,
which dogfooding (2026-06) showed has two structural problems:

  * **Censoring** - a gap only produced a suspend event if it exceeded
    the CURRENT AUTO_SUSPEND, so warehouses that never suspend (the most
    over-provisioned settings, i.e. the best tuning candidates) were
    completely invisible to the recommender.
  * **Config echo** - measuring last-query-end -> suspend-event yields
    approximately the configured AUTO_SUSPEND value itself (suspend fires
    ~AS after the last query), not the workload's gap structure.

Query history has neither problem, and lags ~45 minutes instead of the
events view's hours.  Events remain useful as enrichment - the survival
tuner measures the cold-start cost C from resume durations when they're
available - but the gap distribution itself comes from here.

Mechanics
---------
1. Filter to compute-bearing statements:
   ``COALESCE(execution_ms, total_elapsed_ms, 0) > 0``.  Metadata-only
   statements (USE / SHOW / result-cache hits) report execution_ms = 0
   and don't resume a suspended warehouse, so they must not split gaps.
   The COALESCE fallback keeps synthetic-seed rows (which carry no
   execution_ms) included.  Imperfect filtering shortens gaps, which
   raises the proposed AUTO_SUSPEND - the conservative failure direction.
2. Merge overlapping/adjacent ``[start_time, end_time]`` intervals per
   warehouse into busy islands (running-max sweep) ->
   ``features.warehouse_active_intervals``.
3. Emit the spaces between consecutive islands ->
   ``features.warehouse_idle_gaps``.  The trailing gap (busy island with
   no following query yet) is right-censored and intentionally absent.
"""
from __future__ import annotations

import duckdb

from snowtuner.features.base import FeatureTransform


class WarehouseIdleGapsTransform(FeatureTransform):
    name = "warehouse_idle_gaps"
    inputs = {"raw.query_history"}
    outputs = {
        "features.warehouse_active_intervals",
        "features.warehouse_idle_gaps",
    }

    def run(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("DELETE FROM features.warehouse_active_intervals")
        conn.execute("DELETE FROM features.warehouse_idle_gaps")

        # Stage 1: merge compute-bearing queries into busy islands.
        # Classic running-max island detection: a row starts a new island
        # when its start_time is strictly after every previously-seen
        # end_time for that warehouse.
        conn.execute(
            """
            INSERT INTO features.warehouse_active_intervals
              (warehouse_name, start_time, end_time, duration_sec)
            WITH compute_q AS (
                SELECT warehouse_name, start_time, end_time
                FROM raw.query_history
                WHERE warehouse_name IS NOT NULL
                  AND execution_status = 'SUCCESS'
                  AND start_time IS NOT NULL
                  AND end_time IS NOT NULL
                  AND end_time >= start_time
                  AND COALESCE(execution_ms, total_elapsed_ms, 0) > 0
            ), marked AS (
                SELECT warehouse_name, start_time, end_time,
                       CASE WHEN start_time > COALESCE(
                                MAX(end_time) OVER (
                                    PARTITION BY warehouse_name
                                    ORDER BY start_time, end_time
                                    ROWS BETWEEN UNBOUNDED PRECEDING
                                             AND 1 PRECEDING
                                ),
                                TIMESTAMP '1970-01-01 00:00:00'
                            )
                            THEN 1 ELSE 0 END AS is_island_start
                FROM compute_q
            ), islands AS (
                SELECT warehouse_name, start_time, end_time,
                       SUM(is_island_start) OVER (
                           PARTITION BY warehouse_name
                           ORDER BY start_time, end_time
                           ROWS UNBOUNDED PRECEDING
                       ) AS island_id
                FROM marked
            )
            SELECT warehouse_name,
                   MIN(start_time) AS start_time,
                   MAX(end_time)   AS end_time,
                   date_diff('second', MIN(start_time), MAX(end_time))
                       AS duration_sec
            FROM islands
            GROUP BY warehouse_name, island_id
            """
        )

        # Stage 2: gaps are the spaces between consecutive islands.  LAG
        # leaves the first island gapless and the last island contributes
        # no trailing gap - both correct (right-censored).
        conn.execute(
            """
            INSERT INTO features.warehouse_idle_gaps
              (warehouse_name, gap_start, gap_end, idle_seconds)
            SELECT warehouse_name,
                   prev_end   AS gap_start,
                   start_time AS gap_end,
                   date_diff('second', prev_end, start_time) AS idle_seconds
            FROM (
                SELECT warehouse_name, start_time,
                       LAG(end_time) OVER (
                           PARTITION BY warehouse_name ORDER BY start_time
                       ) AS prev_end
                FROM features.warehouse_active_intervals
            )
            WHERE prev_end IS NOT NULL
              AND start_time > prev_end
            """
        )
