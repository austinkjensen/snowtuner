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

    source_view = "SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY"
    expected_source_columns = [
        "warehouse_id", "warehouse_name", "start_time", "end_time",
        "credits_used", "credits_used_compute", "credits_used_cloud_services",
    ]

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        since_clause = "start_time >= %s" if since else "TRUE"
        params: list = [since] if since else []
        # WAREHOUSE_METERING_HISTORY legitimately has rows with NULL
        # warehouse_name - these are cloud-services / serverless metering
        # not attributable to a single warehouse.  We can't store them
        # because raw.warehouse_metering_history's primary key is
        # (warehouse_name, start_time) and DuckDB enforces NOT NULL on
        # PK columns.  More importantly, the per-warehouse recommenders
        # have nothing useful to do with non-attributed credit rows, so
        # filtering at the source matches intent.
        # Discovered in prod 2026-06-07: without this filter, ingestion
        # raises ConstraintException, fail-fast skips features +
        # recommenders, and the product produces zero recommendations
        # on any account with cloud-services metering (i.e. most accounts).
        sql = f"""
        SELECT
            warehouse_id, warehouse_name, start_time, end_time,
            credits_used, credits_used_compute, credits_used_cloud_services
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_METERING_HISTORY
        WHERE {since_clause}
          AND warehouse_name IS NOT NULL
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
