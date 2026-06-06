"""Source = one pluggable puller from a Snowflake system view into DuckDB."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import duckdb

from snowtuner.storage.db import naive_utcnow


class SnowflakeClient(Protocol):
    """Minimal protocol — what a Source needs from a client."""
    def execute(self, sql: str, params: list | None = None) -> list[tuple[Any, ...]]: ...
    def execute_with_columns(
        self, sql: str, params: list | None = None,
    ) -> tuple[list[str], list[tuple[Any, ...]]]: ...


@dataclass
class SyncResult:
    source_name: str
    rows_ingested: int
    high_water: datetime | None
    duration_seconds: float


class Source(ABC):
    """Base class for an ingestion source.

    Each Source:
    - declares the name/target table
    - declares its watermark column (for incremental sync)
    - implements fetch() to pull rows from Snowflake
    - implements upsert() to write rows into DuckDB
    """

    name: str
    target_table: str  # fully-qualified in DuckDB, e.g. 'raw.query_history'
    watermark_column: str | None  # None = full refresh each sync
    # On the very first run (no stored watermark), look back this far.  None
    # means "no cap" — only safe for small sources like SHOW WAREHOUSES.
    default_initial_lookback_days: int | None = 14

    # Schema drift detection.
    # ``source_view`` is the fully-qualified Snowflake view the source pulls
    # from, e.g. ``'SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY'``.  Sources backed
    # by ``SHOW`` commands or table functions set this to None and are
    # skipped by drift checks.
    # ``expected_source_columns`` is the list of column names the source's
    # SELECT pulls from that view, lowercased.  The drift checker diffs this
    # against ``INFORMATION_SCHEMA.COLUMNS`` for the view.
    source_view: str | None = None
    expected_source_columns: list[str] = []

    @abstractmethod
    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        """Pull rows from Snowflake since the watermark."""

    @abstractmethod
    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
        """Insert or upsert rows into DuckDB."""

    def get_high_water(self, conn: duckdb.DuckDBPyConnection) -> datetime | None:
        row = conn.execute(
            "SELECT high_water FROM app.sync_watermarks WHERE source_name = ?",
            [self.name],
        ).fetchone()
        return row[0] if row else None

    def set_high_water(
        self,
        conn: duckdb.DuckDBPyConnection,
        high_water: datetime | None,
        rows_count: int,
    ) -> None:
        now = naive_utcnow()
        # Coerce high_water to naive UTC: DuckDB silently converts tz-aware
        # values to local time before stripping tz on bind, so we have to
        # normalize at the boundary.  Naive values are assumed already-UTC.
        if high_water is not None and high_water.tzinfo is not None:
            high_water = high_water.astimezone(timezone.utc).replace(tzinfo=None)
        conn.execute(
            """
            INSERT INTO app.sync_watermarks
              (source_name, high_water, last_sync_at, rows_last_sync)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (source_name) DO UPDATE
              SET high_water = excluded.high_water,
                  last_sync_at = excluded.last_sync_at,
                  rows_last_sync = excluded.rows_last_sync
            """,
            [self.name, high_water, now, rows_count],
        )
