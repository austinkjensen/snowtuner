"""Snapshot current warehouse config via ``SHOW WAREHOUSES``.

Full-refresh source (no watermark).

``SHOW WAREHOUSES`` column order varies by Snowflake edition and release, so we
map by column name (from ``cursor.description``), not by position.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient


_COLUMNS = [
    "name", "size", "min_cluster_count", "max_cluster_count",
    "auto_suspend_seconds", "auto_resume", "scaling_policy", "state", "comment",
]


class WarehousesSource(Source):
    name = "warehouses"
    target_table = "raw.warehouses"
    watermark_column = None
    default_initial_lookback_days = None  # full refresh, no lookback concept

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        cols, rows = client.execute_with_columns("SHOW WAREHOUSES")
        col_idx = {c: i for i, c in enumerate(cols)}

        def pick(row: tuple[Any, ...], *candidates: str) -> Any:
            for c in candidates:
                if c in col_idx:
                    return row[col_idx[c]]
            return None

        out: list[dict[str, Any]] = []
        for r in rows:
            auto_resume_raw = pick(r, "auto_resume")
            if isinstance(auto_resume_raw, bool) or auto_resume_raw is None:
                auto_resume = auto_resume_raw
            else:
                # Some Snowflake editions return "true"/"false" as strings.
                auto_resume = str(auto_resume_raw).lower() == "true"

            out.append({
                "name": pick(r, "name"),
                "size": pick(r, "size"),
                "min_cluster_count": pick(r, "min_cluster_count"),
                "max_cluster_count": pick(r, "max_cluster_count"),
                "auto_suspend_seconds": pick(r, "auto_suspend"),
                "auto_resume": auto_resume,
                "scaling_policy": pick(r, "scaling_policy"),
                "state": pick(r, "state"),
                "comment": pick(r, "comment"),
            })
        return out

    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
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
