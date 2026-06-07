"""Incremental pull from SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY."""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
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

    # Schema drift inputs — see Source base class.
    source_view = "SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY"
    expected_source_columns = [
        "query_id", "query_text", "query_type", "execution_status", "user_name",
        "role_name", "warehouse_name", "warehouse_size", "database_name",
        "schema_name", "start_time", "end_time", "total_elapsed_time",
        "compilation_time", "execution_time", "queued_overload_time",
        "queued_provisioning_time", "bytes_scanned",
        "bytes_spilled_to_local_storage", "bytes_spilled_to_remote_storage",
        "rows_produced", "credits_used_cloud_services", "query_hash",
        "query_parameterized_hash", "error_message",
    ]

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        """Single-shot pull.  Used by tests and by the chunked path internally.

        Production sync goes through ``fetch_chunked`` below — pulling the
        full lookback in one call blew past DuckDB's ingest memory ceiling
        on AWS dogfooding (2.9 GB / 3 GB hit while loading 14 days).
        """
        return self._fetch_window(client, since, None)

    def fetch_chunked(
        self, client: SnowflakeClient, since: datetime | None,
    ) -> Iterator[list[dict[str, Any]]]:
        """Slice the lookback window into chunks to bound per-batch memory.

        ``QUERY_HISTORY`` is by far the largest source — one row per query.
        On a busy account, 14 days is millions of rows; funneling them
        through a single DuckDB transaction blows the ingest memory cap
        (see ``storage/db.py:_apply_runtime_pragmas`` for the limit).

        Default slice is 1 day, tunable via
        ``SNOWTUNER_QUERY_HISTORY_CHUNK_DAYS``.  The orchestrator
        ``upsert``s each chunk independently so neither the Python row
        buffer nor the DuckDB transaction ever holds more than one
        window's worth of data.
        """
        chunk_days = int(os.environ.get("SNOWTUNER_QUERY_HISTORY_CHUNK_DAYS", "1"))
        if chunk_days < 1:
            chunk_days = 1

        # Need a concrete start to iterate windows.  If the orchestrator
        # passed since=None (no watermark, no default lookback), fall back
        # to the source's own default_initial_lookback_days.
        end = datetime.now(timezone.utc)
        if since is None:
            start = end - timedelta(days=self.default_initial_lookback_days or 14)
        else:
            start = since
            # DuckDB stores watermarks as naive UTC (see naive_utcnow).
            # Tag them tz-aware here so the < comparison against `end`
            # (tz-aware) doesn't blow up.
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)

        cursor = start
        step = timedelta(days=chunk_days)
        while cursor < end:
            window_end = min(cursor + step, end)
            rows = self._fetch_window(client, cursor, window_end)
            if rows:
                yield rows
            cursor = window_end

    def _fetch_window(
        self,
        client: SnowflakeClient,
        since: datetime | None,
        until: datetime | None,
    ) -> list[dict[str, Any]]:
        """One round-trip to QUERY_HISTORY bounded by [since, until)."""
        clauses: list[str] = []
        params: list = []
        if since is not None:
            clauses.append("start_time >= %s")
            params.append(since)
        if until is not None:
            clauses.append("start_time < %s")
            params.append(until)
        where = " AND ".join(clauses) if clauses else "TRUE"
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
        WHERE {where}
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
