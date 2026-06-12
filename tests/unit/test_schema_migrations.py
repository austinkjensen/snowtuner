"""Forward-migration coverage for derived feature tables.

init_schema uses CREATE TABLE IF NOT EXISTS, which silently keeps an
old-shape table in place - so shape changes to features.* need an
explicit drop in _forward_migrations.  These tests pin that path for the
warehouse_idle_gaps rework (event-anchored columns -> query-history gap
columns).
"""
from __future__ import annotations

import duckdb

from snowtuner.storage.schema import init_schema


def _columns(conn: duckdb.DuckDBPyConnection, table: str) -> set[str]:
    return {
        r[0] for r in conn.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = 'features' AND table_name = ?
            """,
            [table],
        ).fetchall()
    }


class TestIdleGapsMigration:
    def test_old_shape_dropped_and_recreated(self):
        conn = duckdb.connect(":memory:")
        # Simulate a database created before the gap-based rework.
        conn.execute("CREATE SCHEMA features")
        conn.execute(
            """
            CREATE TABLE features.warehouse_idle_gaps (
                warehouse_name        VARCHAR,
                last_query_end_time   TIMESTAMP,
                suspend_time          TIMESTAMP,
                idle_seconds          DOUBLE,
                PRIMARY KEY (warehouse_name, last_query_end_time)
            )
            """
        )
        init_schema(conn)
        cols = _columns(conn, "warehouse_idle_gaps")
        assert "gap_start" in cols and "gap_end" in cols
        assert "suspend_time" not in cols
        conn.close()

    def test_fresh_database_unaffected(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        cols = _columns(conn, "warehouse_idle_gaps")
        assert "gap_start" in cols
        conn.close()

    def test_idempotent_reruns(self):
        conn = duckdb.connect(":memory:")
        init_schema(conn)
        init_schema(conn)  # second run must not raise or drop data shape
        assert "gap_start" in _columns(conn, "warehouse_idle_gaps")
        conn.close()
