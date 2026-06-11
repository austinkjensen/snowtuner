"""Compute per-warehouse idle gaps: time between last query end and suspend event.

This is the core feature for AUTO_SUSPEND tuning.  If a warehouse consistently
idles for ~N seconds before being suspended, we can recommend AUTO_SUSPEND ~= N.
"""
from __future__ import annotations

import duckdb

from snowtuner.features.base import FeatureTransform
from snowtuner.ingestion.event_vocab import SUSPEND_EVENT_NAMES, sql_in_list


class WarehouseIdleGapsTransform(FeatureTransform):
    name = "warehouse_idle_gaps"
    inputs = {"raw.query_history", "raw.warehouse_events_history"}
    outputs = {"features.warehouse_idle_gaps"}

    def run(self, conn: duckdb.DuckDBPyConnection) -> None:
        conn.execute("DELETE FROM features.warehouse_idle_gaps")
        # For every suspend event (either vocabulary - see event_vocab.py),
        # find the most-recent query_history row on that warehouse that
        # ended before the event.  idle_seconds is the difference.  Queries
        # that ended *after* the event are ignored.
        conn.execute(
            f"""
            INSERT INTO features.warehouse_idle_gaps
              (warehouse_name, last_query_end_time, suspend_time, idle_seconds)
            SELECT
              e.warehouse_name,
              q.last_end,
              e.timestamp AS suspend_time,
              date_diff('second', q.last_end, e.timestamp) AS idle_seconds
            FROM raw.warehouse_events_history e
            LEFT JOIN LATERAL (
                SELECT MAX(end_time) AS last_end
                FROM raw.query_history qh
                WHERE qh.warehouse_name = e.warehouse_name
                  AND qh.end_time <= e.timestamp
                  -- Restrict to queries within an hour of the suspend to keep
                  -- gaps interpretable (long-gone queries aren't real idle).
                  AND qh.end_time >= e.timestamp - INTERVAL 1 HOUR
            ) q ON TRUE
            WHERE e.event_name IN ({sql_in_list(SUSPEND_EVENT_NAMES)})
              AND q.last_end IS NOT NULL
            """
        )
