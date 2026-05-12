"""Pull from ``SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY``.

Captures RESUME / SUSPEND / RESIZE events, which feed the auto_suspend
recommender's reactivation-gap calculation.

Snowflake doesn't expose a surrogate key for events.  We synthesize one
deterministically from a sha256 of the natural key
``(timestamp, warehouse_id, event_name, cluster_number)``, which keeps
INSERT OR REPLACE idempotent across re-syncs without polluting the schema
with NULL sentinels.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient


# event_id is computed locally; the rest mirror the Snowflake view.
_COLUMNS = [
    "event_id",
    "timestamp", "warehouse_id", "warehouse_name", "cluster_number",
    "event_name", "event_reason", "event_state", "user_name", "role_name",
    "query_id", "size", "cluster_count",
]


class WarehouseEventsSource(Source):
    name = "warehouse_events_history"
    target_table = "raw.warehouse_events_history"
    watermark_column = "timestamp"

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        since_clause = "timestamp >= %s" if since else "TRUE"
        params: list = [since] if since else []
        sql = f"""
        SELECT
            timestamp, warehouse_id, warehouse_name, cluster_number,
            event_name, event_reason, event_state, user_name, role_name,
            query_id, size, cluster_count
        FROM SNOWFLAKE.ACCOUNT_USAGE.WAREHOUSE_EVENTS_HISTORY
        WHERE {since_clause}
        ORDER BY timestamp ASC
        """
        rows = client.execute(sql, params)
        # Source-level columns (no event_id — that's computed at upsert time).
        snowflake_cols = [c for c in _COLUMNS if c != "event_id"]
        return [dict(zip(snowflake_cols, r)) for r in rows]

    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        col_list = ", ".join(_COLUMNS)
        for r in rows:
            r_with_id = {**r, "event_id": _compute_event_id(r)}
            conn.execute(
                f"INSERT OR REPLACE INTO raw.warehouse_events_history "
                f"({col_list}) VALUES ({placeholders})",
                [r_with_id.get(c) for c in _COLUMNS],
            )


def _compute_event_id(row: dict[str, Any]) -> int:
    """Stable signed BIGINT id from a sha256 of the natural key.

    NULLs are serialized as the literal string ``"None"``; same logical event
    always hashes to the same id, so re-syncing the boundary row at the
    watermark cleanly REPLACEs in place rather than duplicating.
    """
    parts = (
        str(row.get("timestamp")),
        str(row.get("warehouse_id")),
        str(row.get("event_name")),
        str(row.get("cluster_number")),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)
