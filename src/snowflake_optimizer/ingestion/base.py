"""Source = one pluggable puller from a Snowflake system view into DuckDB."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

import duckdb


class SnowflakeClient(Protocol):
    """Minimal protocol — whatever exposes an `execute(sql, params) -> iterable of rows`."""
    def execute(self, sql: str, params: list | None = None) -> list[tuple[Any, ...]]: ...


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
        now = datetime.now(timezone.utc)
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
