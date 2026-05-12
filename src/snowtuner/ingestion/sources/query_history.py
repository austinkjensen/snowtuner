"""Incremental pull from SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient


_COLUMNS = [
    "query_id", "query_text", "query_type", "execution_status", "user_name",
    "role_name", "warehouse_name", "warehouse_size", "database_name", "schema_name",
    "start_time", "end_time", "total_elapsed_ms", "compilation_ms", "execution_ms",
    "queued_overload_ms", "queued_provisioning_ms", "bytes_scanned",
    "bytes_spilled_to_local", "bytes_spilled_to_remote", "rows_produced",
    "credits_used_cloud_services", "query_hash", "query_parameterized_hash",
    "error_message",
]


class QueryHistorySource(Source):
    name = "query_history"
    target_table = "raw.query_history"
    watermark_column = "start_time"

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        since_clause = "start_time >= %s" if since else "TRUE"
        params: list = [since] if since else []
        sql = f"""
        SELECT
            query_id, query_text, query_type, execution_status, user_name,
            role_name, warehouse_name, warehouse_size, database_name, schema_name,
            start_time, end_time, total_elapsed_time, compilation_time,
            execution_time, queued_overload_time, queued_provisioning_time,
            bytes_scanned, bytes_spilled_to_local_storage,
            bytes_spilled_to_remote_storage, rows_produced,
            credits_used_cloud_services, query_hash, query_parameterized_hash,
            error_message
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
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
        conn.execute("BEGIN")
        try:
            for r in rows:
                conn.execute(
                    f"INSERT OR REPLACE INTO raw.query_history ({col_list}) "
                    f"VALUES ({placeholders})",
                    [r.get(c) for c in _COLUMNS],
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
