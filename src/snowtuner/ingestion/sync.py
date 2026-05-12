"""Orchestrate a sync pass across one or more Sources."""
from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient, SyncResult


@dataclass
class SyncError:
    source_name: str
    error: str


def sync_source(
    source: Source,
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
    *,
    initial_lookback_days: int | None = None,
) -> SyncResult:
    t0 = time.time()

    # Resolve the "since" watermark.  Preference order:
    # 1. Stored watermark in app.sync_watermarks
    # 2. Source's default_initial_lookback_days
    # 3. Caller-supplied initial_lookback_days override
    # 4. None (sources with no watermark_column)
    since: datetime | None = None
    if source.watermark_column:
        since = source.get_high_water(conn)
        if since is None:
            lookback = (
                initial_lookback_days
                if initial_lookback_days is not None
                else source.default_initial_lookback_days
            )
            if lookback is not None:
                since = datetime.now(timezone.utc) - timedelta(days=lookback)

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
    *,
    initial_lookback_days: int | None = None,
) -> tuple[list[SyncResult], list[SyncError]]:
    """Run each source.  One failing source does not abort the rest."""
    results: list[SyncResult] = []
    errors: list[SyncError] = []
    for s in sources:
        try:
            results.append(
                sync_source(s, client, conn, initial_lookback_days=initial_lookback_days)
            )
        except Exception as e:
            errors.append(SyncError(source_name=s.name, error=f"{type(e).__name__}: {e}"))
    return results, errors
