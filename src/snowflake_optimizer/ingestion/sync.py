"""Orchestrate a sync pass across one or more Sources."""
from __future__ import annotations

import time
from collections.abc import Iterable
from datetime import datetime

import duckdb

from snowflake_optimizer.ingestion.base import Source, SnowflakeClient, SyncResult


def sync_source(
    source: Source,
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
) -> SyncResult:
    t0 = time.time()
    since = source.get_high_water(conn) if source.watermark_column else None
    rows = source.fetch(client, since)
    source.upsert(conn, rows)

    new_high_water: datetime | None = None
    if source.watermark_column and rows:
        wm = source.watermark_column
        vals = [r.get(wm) for r in rows if r.get(wm) is not None]
        if vals:
            new_high_water = max(vals)
    source.set_high_water(conn, new_high_water or since, len(rows))

    return SyncResult(
        source_name=source.name,
        rows_ingested=len(rows),
        high_water=new_high_water,
        duration_seconds=time.time() - t0,
    )


def sync_all(
    sources: Iterable[Source],
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
) -> list[SyncResult]:
    return [sync_source(s, client, conn) for s in sources]
