"""Tests for ``snowtuner.ingestion.sources.warehouse_metering``.

Regression coverage for the 2026-06-07 production bug: WAREHOUSE_METERING_
HISTORY legitimately contains rows with NULL warehouse_name (cloud-services
/ serverless metering not attributable to a single warehouse).  Without
filtering, those rows hit the (warehouse_name, start_time) primary key
constraint and crash the whole sync stage - which then skips features +
recommenders, producing zero recommendations on any account with such
rows (i.e. most real accounts).

The fix filters at the source SQL.  These tests pin both the filter
mechanics (the SQL contains the filter) and the behavior (an end-to-end
round-trip with realistic post-filter data lands in DuckDB cleanly).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import duckdb

from snowtuner.ingestion.sources.warehouse_metering import WarehouseMeteringSource


class _RecordingClient:
    """Stub that records SQL/params and returns a configured row set."""

    def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.rows = rows or []
        self.last_sql: str | None = None
        self.last_params: list | None = None

    def execute(self, sql: str, params: list | None = None) -> list[tuple]:
        self.last_sql = sql
        self.last_params = params
        return self.rows


class TestSqlFilter:
    """The SQL must contain the NULL filter regardless of whether `since`
    is set.  This is the load-bearing piece of the fix - if a future
    refactor removes the filter, this test screams."""

    def test_filter_present_with_since(self):
        src = WarehouseMeteringSource()
        client = _RecordingClient(rows=[])
        src.fetch(client, since=datetime(2026, 6, 1))
        assert client.last_sql is not None
        # Normalize whitespace so the test isn't fragile to formatting.
        normalized = " ".join(client.last_sql.split()).upper()
        assert "WAREHOUSE_NAME IS NOT NULL" in normalized

    def test_filter_present_without_since(self):
        """The 'no watermark yet' code path is a different branch in fetch();
        the filter must be there too."""
        src = WarehouseMeteringSource()
        client = _RecordingClient(rows=[])
        src.fetch(client, since=None)
        assert client.last_sql is not None
        normalized = " ".join(client.last_sql.split()).upper()
        assert "WAREHOUSE_NAME IS NOT NULL" in normalized


class TestEndToEndUpsert:
    """Full path: fetch (against a stubbed client returning rows shaped like
    Snowflake's post-filter response) -> upsert into a real in-memory DuckDB
    with the canonical schema.  Validates that named-warehouse rows land
    cleanly and that the PK doesn't trip."""

    def test_named_rows_round_trip(self, duck: duckdb.DuckDBPyConnection):
        src = WarehouseMeteringSource()
        base = datetime(2026, 6, 1, 0, 0, 0)
        rows = [
            ("wh-id-1", "ETL_WH",       base,                       base + timedelta(hours=1), 1.5, 1.2, 0.3),
            ("wh-id-1", "ETL_WH",       base + timedelta(hours=1),  base + timedelta(hours=2), 0.8, 0.7, 0.1),
            ("wh-id-2", "ANALYTICS_WH", base,                       base + timedelta(hours=1), 0.4, 0.3, 0.1),
        ]
        client = _RecordingClient(rows=rows)
        fetched = src.fetch(client, since=None)
        src.upsert(duck, fetched)

        count = duck.execute(
            "SELECT COUNT(*) FROM raw.warehouse_metering_history"
        ).fetchone()[0]
        assert count == 3

        # Spot-check that the columns landed correctly.
        etl = duck.execute(
            "SELECT credits_used FROM raw.warehouse_metering_history "
            "WHERE warehouse_name = 'ETL_WH' ORDER BY start_time"
        ).fetchall()
        assert [r[0] for r in etl] == [1.5, 0.8]

    def test_null_warehouse_row_would_fail_pk(self, duck: duckdb.DuckDBPyConnection):
        """This test pins WHY the filter exists.  If the SQL filter ever
        gets removed and a NULL warehouse_name row reaches upsert(), it
        crashes with a ConstraintException - this test demonstrates the
        failure mode so the comment in warehouse_metering.py has
        executable backing.

        We bypass the filter by passing the null row directly to upsert,
        simulating what would happen without the SQL fix.
        """
        src = WarehouseMeteringSource()
        base = datetime(2026, 6, 1, 0, 0, 0)
        # The exact row shape that production found in the wild:
        # warehouse_id NULL, warehouse_name NULL, credits attributed to
        # cloud-services metering only.
        null_row = [{
            "warehouse_id": None,
            "warehouse_name": None,
            "start_time": base,
            "end_time": base + timedelta(hours=1),
            "credits_used": 0.05,
            "credits_used_compute": 0.0,
            "credits_used_cloud_services": 0.05,
        }]

        # This should raise on the PK NOT NULL constraint - exactly the
        # crash the SQL filter exists to prevent.
        raised = False
        try:
            src.upsert(duck, null_row)
        except duckdb.ConstraintException:
            raised = True
        except Exception as e:
            # Older DuckDB versions raise different subclasses; treat any
            # constraint-shaped error as expected.
            if "constraint" in str(e).lower() or "not null" in str(e).lower():
                raised = True
            else:
                raise
        assert raised, (
            "Upsert of a NULL-warehouse_name row should fail on the PK "
            "constraint - if it doesn't, the schema's PK definition changed "
            "and the SQL filter may no longer be needed"
        )

    def test_empty_fetch_is_noop(self, duck: duckdb.DuckDBPyConnection):
        """An account with literally zero metering history shouldn't error."""
        src = WarehouseMeteringSource()
        client = _RecordingClient(rows=[])
        fetched = src.fetch(client, since=None)
        src.upsert(duck, fetched)
        count = duck.execute(
            "SELECT COUNT(*) FROM raw.warehouse_metering_history"
        ).fetchone()[0]
        assert count == 0
