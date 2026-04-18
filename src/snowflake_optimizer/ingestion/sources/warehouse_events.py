"""Pull from SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY (resume/suspend/resize)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from snowflake_optimizer.ingestion.base import Source, SnowflakeClient


_COLUMNS = [
    "event_id", "timestamp", "warehouse_id", "warehouse_name", "event_name",
    "event_reason", "event_state", "user_name", "role_name", "cluster_number", "size",
]


class WarehouseEventsSource(Source):
    name = "warehouse_events_history"
    target_table = "raw.warehouse_events_history"
    watermark_column = "timestamp"

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        since_clause = "timestamp >= %s" if since else "TRUE"
        params: list = [since] if since else []
        # Note: WAREHOUSE_EVENTS_HISTORY column names vary slightly by Snowflake
        # edition.  event_id is our synthetic key here — if not present, use a hash.
        sql = f"""
        SELECT
            event_id, timestamp, warehouse_id, warehouse_name, event_name,
            event_reason, event_state, user_name, role_name,
            cluster_number, size
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY
        WHERE {since_clause}
        ORDER BY timestamp ASC
        """
        rows = client.execute(sql, params)
        return [dict(zip(_COLUMNS, r)) for r in rows]

    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        col_list = ", ".join(_COLUMNS)
        for r in rows:
            conn.execute(
                f"INSERT OR REPLACE INTO raw.warehouse_events_history "
                f"({col_list}) VALUES ({placeholders})",
                [r.get(c) for c in _COLUMNS],
            )
