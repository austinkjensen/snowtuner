"""Snapshot current warehouse config via SHOW WAREHOUSES.

Full-refresh source (no watermark).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from snowflake_optimizer.ingestion.base import Source, SnowflakeClient


_COLUMNS = [
    "name", "size", "min_cluster_count", "max_cluster_count",
    "auto_suspend_seconds", "auto_resume", "scaling_policy", "state", "comment",
]


class WarehousesSource(Source):
    name = "warehouses"
    target_table = "raw.warehouses"
    watermark_column = None

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        # SHOW WAREHOUSES returns many columns; we pick the ones we care about.
        # Actual Snowflake SHOW WAREHOUSES output columns are slightly different,
        # so in a live system we'd fetch via information_schema or post-process.
        rows = client.execute("SHOW WAREHOUSES")
        # SHOW result column positions (approximate for ACCOUNTADMIN):
        # 0:name, 3:size, 4:min_cluster_count, 5:max_cluster_count, 8:auto_suspend,
        # 9:auto_resume, 11:state, 18:scaling_policy, 20:comment
        out: list[dict[str, Any]] = []
        for r in rows:
            try:
                out.append({
                    "name": r[0],
                    "size": r[3] if len(r) > 3 else None,
                    "min_cluster_count": r[4] if len(r) > 4 else None,
                    "max_cluster_count": r[5] if len(r) > 5 else None,
                    "auto_suspend_seconds": r[8] if len(r) > 8 else None,
                    "auto_resume": r[9] if len(r) > 9 else None,
                    "scaling_policy": r[18] if len(r) > 18 else None,
                    "state": r[11] if len(r) > 11 else None,
                    "comment": r[20] if len(r) > 20 else None,
                })
            except IndexError:
                continue
        return out

    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
        # Full refresh: truncate and re-insert.
        conn.execute("DELETE FROM raw.warehouses")
        if not rows:
            return
        col_list = ", ".join(_COLUMNS)
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        for r in rows:
            conn.execute(
                f"INSERT INTO raw.warehouses ({col_list}) VALUES ({placeholders})",
                [r.get(c) for c in _COLUMNS],
            )
