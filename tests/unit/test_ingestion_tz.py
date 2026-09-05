"""Timezone-boundary regression tests for the sync layer.

The `since` watermark crosses two subsystems with opposite tz preferences:
the DuckDB store wants naive-UTC (see storage.db.naive_utcnow), while the
Snowflake connector binds a naive datetime in the session timezone and so
wants an explicit aware-UTC instant. Historically `since` was naive on the
stored-watermark path but tz-aware on the initial-lookback path, and only
`query_history` normalized it (its windowing did a naive-vs-aware compare
that would otherwise raise TypeError). These tests pin the invariants that
removed that inconsistency:

  - `since` is naive-UTC everywhere internally,
  - every Snowflake bind site tags it aware-UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source
from snowtuner.ingestion.sources.query_history import QueryHistorySource
from snowtuner.ingestion.sources.warehouse_events import WarehouseEventsSource
from snowtuner.ingestion.sources.warehouse_metering import WarehouseMeteringSource
from snowtuner.ingestion.sync import sync_source
from snowtuner.storage.db import as_aware_utc, as_naive_utc, naive_utcnow


class _RecordingClient:
    """Records (sql, params) of every execute; returns no rows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def execute(self, sql: str, params: list | None = None) -> list[tuple]:
        self.calls.append((sql, params or []))
        return []

    def execute_with_columns(self, sql: str, params: list | None = None):
        self.calls.append((sql, params or []))
        return ([], [])


# ── the two boundary helpers ──────────────────────────────────────────────


class TestBoundaryHelpers:
    def test_as_naive_utc_strips_aware(self):
        aware = datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)
        out = as_naive_utc(aware)
        assert out == datetime(2026, 9, 5, 17, 0)
        assert out.tzinfo is None

    def test_as_naive_utc_converts_offset_before_stripping(self):
        # 10:00 at -07:00 is 17:00 UTC.
        pdt = datetime(2026, 9, 5, 10, 0, tzinfo=timezone(timedelta(hours=-7)))
        assert as_naive_utc(pdt) == datetime(2026, 9, 5, 17, 0)

    def test_as_naive_utc_passes_naive_through(self):
        naive = datetime(2026, 9, 5, 17, 0)
        assert as_naive_utc(naive) == naive
        assert as_naive_utc(naive).tzinfo is None

    def test_as_naive_utc_none(self):
        assert as_naive_utc(None) is None

    def test_as_aware_utc_tags_naive_as_utc(self):
        naive = datetime(2026, 9, 5, 17, 0)
        out = as_aware_utc(naive)
        assert out == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)
        assert out.tzinfo == timezone.utc

    def test_as_aware_utc_normalizes_offset(self):
        pdt = datetime(2026, 9, 5, 10, 0, tzinfo=timezone(timedelta(hours=-7)))
        assert as_aware_utc(pdt) == datetime(2026, 9, 5, 17, 0, tzinfo=timezone.utc)

    def test_as_aware_utc_none(self):
        assert as_aware_utc(None) is None


# ── the sync layer produces naive-UTC `since` ─────────────────────────────


class _RecordingSource(Source):
    """Watermarked source that records the `since` it is handed."""

    name = "recording"
    target_table = "raw.recording"
    watermark_column = "start_time"
    default_initial_lookback_days = 14

    def __init__(self) -> None:
        self.seen_since: datetime | None = "UNSET"  # type: ignore[assignment]

    def fetch(self, client, since):
        return []

    def fetch_chunked(self, client, since):
        self.seen_since = since
        return iter(())

    def upsert(self, conn, rows):
        pass


class TestInitialSinceIsNaiveUtc:
    def test_first_sync_since_is_naive(self, duck: duckdb.DuckDBPyConnection):
        """No stored watermark -> initial lookback. The `since` handed to the
        source must be naive-UTC, matching what get_high_water returns on
        later syncs (rather than the old tz-aware value)."""
        src = _RecordingSource()
        sync_source(src, _RecordingClient(), duck, initial_lookback_days=14)
        assert src.seen_since is not None
        assert src.seen_since.tzinfo is None
        # ~14 days before now, give or take the test's wall-clock.
        delta = naive_utcnow() - src.seen_since
        assert timedelta(days=13, hours=23) < delta < timedelta(days=14, hours=1)

    def test_stored_watermark_since_is_naive(self, duck: duckdb.DuckDBPyConnection):
        """A stored (naive) watermark flows through unchanged."""
        wm = datetime(2026, 9, 5, 17, 45, 12)
        duck.execute(
            "INSERT INTO app.sync_watermarks (source_name, high_water) VALUES (?, ?)",
            ["recording", wm],
        )
        src = _RecordingSource()
        sync_source(src, _RecordingClient(), duck)
        assert src.seen_since == wm
        assert src.seen_since.tzinfo is None


# ── query_history tolerates either flavor and binds aware-UTC ──────────────


class TestQueryHistoryTz:
    def test_fetch_window_binds_aware_utc(self):
        cli = _RecordingClient()
        QueryHistorySource()._fetch_window(
            cli, datetime(2026, 9, 5, 17, 0), datetime(2026, 9, 5, 18, 0),
        )
        _, params = cli.calls[-1]
        assert len(params) == 2
        assert all(p.tzinfo == timezone.utc for p in params)

    def test_fetch_chunked_accepts_naive_since(self):
        """The historical failure mode: a naive watermark compared against a
        tz-aware `end`. Must not raise now."""
        cli = _RecordingClient()
        naive_since = naive_utcnow() - timedelta(hours=3)
        list(QueryHistorySource().fetch_chunked(cli, naive_since))

    def test_fetch_chunked_accepts_aware_since(self):
        """A defensive caller passing tz-aware must also work (as_naive_utc
        normalizes it before the naive `end` comparison)."""
        cli = _RecordingClient()
        aware_since = (naive_utcnow() - timedelta(hours=3)).replace(tzinfo=timezone.utc)
        list(QueryHistorySource().fetch_chunked(cli, aware_since))

    def test_fetch_chunked_windows_bind_aware_utc(self):
        cli = _RecordingClient()
        naive_since = naive_utcnow() - timedelta(hours=3)
        list(QueryHistorySource().fetch_chunked(cli, naive_since))
        # Every window bound its boundaries aware-UTC.
        bound = [p for _, params in cli.calls for p in params if isinstance(p, datetime)]
        assert bound  # at least one window ran
        assert all(p.tzinfo == timezone.utc for p in bound)


# ── metering / events bind aware-UTC ──────────────────────────────────────


class TestMeteringEventsTz:
    def test_metering_binds_aware_utc(self):
        cli = _RecordingClient()
        WarehouseMeteringSource().fetch(cli, datetime(2026, 9, 5, 17, 0))
        _, params = cli.calls[-1]
        assert params and params[0].tzinfo == timezone.utc

    def test_events_binds_aware_utc(self):
        cli = _RecordingClient()
        WarehouseEventsSource().fetch(cli, datetime(2026, 9, 5, 17, 0))
        _, params = cli.calls[-1]
        assert params and params[0].tzinfo == timezone.utc

    def test_none_since_binds_nothing(self):
        cli = _RecordingClient()
        WarehouseMeteringSource().fetch(cli, None)
        _, params = cli.calls[-1]
        assert params == []
