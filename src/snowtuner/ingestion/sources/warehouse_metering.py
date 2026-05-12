"""Pull from SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY (hourly credits)."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient

_COLUMNS = [
    "warehouse_id", "warehouse_name", "start_time", "end_time",
    "credits_used", "credits_used_compute", "credits_used_cloud_services",
]


class WarehouseMeteringSource(Source):
    name = "warehouse_metering_history"
    target_table = "raw.warehouse_metering_history"
    watermark_column = "start_time"

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        since_clause = "start_time >= %s" if since else "TRUE"
        params: list = [since] if since else []
        sql = f"""
        SELECT
            warehouse_id, warehouse_name, start_time, end_time,
            credits_used, credits_used_compute, credits_used_cloud_services
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE {since_clause}
        ORDER BY start_time ASC
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
                f"INSERT OR REPLACE INTO raw.warehouse_metering_history "
                f"({col_list}) VALUES ({placeholders})",
                [r.get(c) for c in _COLUMNS],
            )
