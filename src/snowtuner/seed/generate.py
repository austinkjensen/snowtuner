"""Generate plausible fake Snowflake telemetry into the raw.* tables.

Creates six fictional warehouses, three for the auto_suspend recommender and
three for the right-sizing recommender, so a single seed exercises both.

Auto-suspend candidates
-----------------------
ANALYTICS_WH   AUTO_SUSPEND=600s, idles only ~120s before re-resume → propose lower.
ETL_WH         AUTO_SUSPEND=300s, bursty (~50s) → propose lower.
BI_WH          AUTO_SUSPEND=300s, idle pattern matches → no proposal.

Right-sizing candidates
-----------------------
MEMORY_HOG_WH  Small.  Many queries spill to remote storage → upsize.
SATURATED_WH   Medium.  Queries queue ~10s on average → upsize.
OVERKILL_WH    Large.  Queries finish <500ms, no spills → downsize.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

import duckdb


SAMPLE_QUERIES = [
    "SELECT COUNT(*) FROM orders WHERE order_date >= '2026-01-01'",
    "SELECT customer_id, SUM(total) FROM orders GROUP BY customer_id",
    "SELECT * FROM dim_products WHERE category = 'electronics'",
    "SELECT AVG(elapsed_ms) FROM query_history WHERE start_time >= current_date - 7",
    "MERGE INTO stg_events USING raw_events ON stg_events.id = raw_events.id",
    "SELECT date_trunc('day', ts), COUNT(*) FROM clicks GROUP BY 1 ORDER BY 1",
]


def seed_demo_data(
    conn: duckdb.DuckDBPyConnection,
    *,
    days: int = 21,
    seed: int = 42,
) -> dict[str, int]:
    """Clear raw.* and repopulate with synthetic data.  Returns counts by table."""
    rng = random.Random(seed)

    for tbl in ("raw.query_history", "raw.warehouse_events_history",
                "raw.warehouse_metering_history", "raw.warehouses"):
        conn.execute(f"DELETE FROM {tbl}")

    # ── auto_suspend candidates: (name, size, AUTO_SUSPEND, reactivation_mean, idle_mean) ──
    suspend_warehouses = [
        ("ANALYTICS_WH", "MEDIUM",  600, 120, 600),
        ("ETL_WH",       "LARGE",   300,  50, 300),
        ("BI_WH",        "SMALL",   300, 360, 300),
    ]

    for name, size, auto_susp, _, _ in suspend_warehouses:
        _insert_warehouse(conn, name, size, auto_susp)

    now = datetime.now().replace(microsecond=0)
    event_id = 0
    query_counter = 0

    for name, size, _, reactivation_mean, idle_mean in suspend_warehouses:
        event_id, query_counter = _seed_suspend_pattern(
            conn, rng, name, size, days, now,
            reactivation_mean, idle_mean,
            event_id, query_counter,
        )

    # ── right-sizing candidates ──
    # MEMORY_HOG_WH: every other query spills, ~10% to remote (Rule 1 fires).
    _insert_warehouse(conn, "MEMORY_HOG_WH", "SMALL", 60)
    query_counter = _seed_right_sizing_pattern(
        conn, rng, "MEMORY_HOG_WH", "SMALL", days, now, query_counter,
        n_queries_per_day=20,
        spill_local_prob=0.5, spill_remote_prob=0.10,
        elapsed_ms_range=(2_000, 30_000),
        queue_overload_ms_range=(0, 0),
    )

    # SATURATED_WH: queue overload averages ~10s — Rule 3 fires.
    _insert_warehouse(conn, "SATURATED_WH", "MEDIUM", 60)
    query_counter = _seed_right_sizing_pattern(
        conn, rng, "SATURATED_WH", "MEDIUM", days, now, query_counter,
        n_queries_per_day=15,
        spill_local_prob=0.0, spill_remote_prob=0.0,
        elapsed_ms_range=(1_000, 8_000),
        queue_overload_ms_range=(5_000, 15_000),
    )

    # OVERKILL_WH: trivial queries on a Large warehouse — Rule 4 fires (downsize).
    _insert_warehouse(conn, "OVERKILL_WH", "LARGE", 60)
    query_counter = _seed_right_sizing_pattern(
        conn, rng, "OVERKILL_WH", "LARGE", days, now, query_counter,
        n_queries_per_day=20,
        spill_local_prob=0.0, spill_remote_prob=0.0,
        elapsed_ms_range=(50, 500),
        queue_overload_ms_range=(0, 0),
    )

    counts: dict[str, int] = {}
    for tbl in ("raw.query_history", "raw.warehouse_events_history", "raw.warehouses"):
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    return counts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _insert_warehouse(
    conn: duckdb.DuckDBPyConnection, name: str, size: str, auto_suspend: int,
) -> None:
    conn.execute(
        """
        INSERT INTO raw.warehouses
          (name, size, min_cluster_count, max_cluster_count,
           auto_suspend_seconds, auto_resume, scaling_policy, state, comment)
        VALUES (?, ?, 1, 1, ?, TRUE, 'STANDARD', 'SUSPENDED', NULL)
        """,
        [name, size, auto_suspend],
    )


def _seed_suspend_pattern(
    conn: duckdb.DuckDBPyConnection,
    rng: random.Random,
    name: str,
    size: str,
    days: int,
    now: datetime,
    reactivation_mean: float,
    idle_mean: float,
    event_id: int,
    query_counter: int,
) -> tuple[int, int]:
    """Generate suspend/resume cycles + simple queries for the auto_suspend recommender."""
    t = now - timedelta(days=days)
    end = now
    while t < end:
        resume_ts = t
        event_id += 1
        conn.execute(
            """
            INSERT INTO raw.warehouse_events_history
              (event_id, timestamp, warehouse_id, warehouse_name,
               event_name, event_state, size)
            VALUES (?, ?, ?, ?, 'RESUME_WAREHOUSE', 'COMPLETED', ?)
            """,
            [event_id, resume_ts, name.lower() + "_id", name, size],
        )

        q_start = resume_ts + timedelta(seconds=rng.uniform(0.5, 5))
        last_q_end = q_start
        for _ in range(rng.randint(3, 15)):
            dur_ms = rng.randint(200, 8000)
            q_end = q_start + timedelta(milliseconds=dur_ms)
            query_counter += 1
            _insert_query(
                conn, rng, name, size, q_start, q_end, dur_ms,
                bytes_spilled_local=0, bytes_spilled_remote=0,
                queued_overload_ms=0, query_counter=query_counter,
            )
            last_q_end = q_end
            q_start = q_end + timedelta(milliseconds=rng.randint(50, 4000))

        idle = max(5, rng.gauss(idle_mean, idle_mean * 0.15))
        suspend_ts = last_q_end + timedelta(seconds=idle)
        event_id += 1
        conn.execute(
            """
            INSERT INTO raw.warehouse_events_history
              (event_id, timestamp, warehouse_id, warehouse_name,
               event_name, event_state, size)
            VALUES (?, ?, ?, ?, 'SUSPEND_WAREHOUSE', 'COMPLETED', ?)
            """,
            [event_id, suspend_ts, name.lower() + "_id", name, size],
        )
        reactivation = max(5.0, rng.gauss(reactivation_mean, reactivation_mean * 0.25))
        t = suspend_ts + timedelta(seconds=reactivation)
    return event_id, query_counter


def _seed_right_sizing_pattern(
    conn: duckdb.DuckDBPyConnection,
    rng: random.Random,
    name: str,
    size: str,
    days: int,
    now: datetime,
    query_counter: int,
    *,
    n_queries_per_day: int,
    spill_local_prob: float,
    spill_remote_prob: float,
    elapsed_ms_range: tuple[int, int],
    queue_overload_ms_range: tuple[int, int],
) -> int:
    """Generate query history exhibiting a right-sizing problem.

    No suspend/resume events — the right-sizer doesn't need them, and we don't
    want them to pollute the auto_suspend recommender's view of these warehouses.
    """
    t = now - timedelta(days=days)
    while t < now:
        for _ in range(n_queries_per_day):
            dur_ms = rng.randint(*elapsed_ms_range)
            q_start = t + timedelta(seconds=rng.randint(0, 86400))
            q_end = q_start + timedelta(milliseconds=dur_ms)
            spill_local = (
                rng.randint(50_000_000, 500_000_000)
                if rng.random() < spill_local_prob else 0
            )
            spill_remote = (
                rng.randint(100_000_000, 2_000_000_000)
                if rng.random() < spill_remote_prob else 0
            )
            queue_ms = (
                rng.randint(*queue_overload_ms_range)
                if queue_overload_ms_range[1] > 0 else 0
            )
            query_counter += 1
            _insert_query(
                conn, rng, name, size, q_start, q_end, dur_ms,
                bytes_spilled_local=spill_local,
                bytes_spilled_remote=spill_remote,
                queued_overload_ms=queue_ms,
                query_counter=query_counter,
            )
        t += timedelta(days=1)
    return query_counter


def _insert_query(
    conn: duckdb.DuckDBPyConnection,
    rng: random.Random,
    warehouse_name: str,
    warehouse_size: str,
    start_ts: datetime,
    end_ts: datetime,
    elapsed_ms: int,
    *,
    bytes_spilled_local: int,
    bytes_spilled_remote: int,
    queued_overload_ms: int,
    query_counter: int,
) -> None:
    qid = f"q_{warehouse_name}_{query_counter:07d}"
    qtext = rng.choice(SAMPLE_QUERIES)
    conn.execute(
        """
        INSERT INTO raw.query_history
          (query_id, query_text, query_type, execution_status,
           user_name, warehouse_name, warehouse_size,
           start_time, end_time, total_elapsed_ms,
           queued_overload_ms,
           bytes_spilled_to_local, bytes_spilled_to_remote,
           query_parameterized_hash)
        VALUES (?, ?, 'SELECT', 'SUCCESS', 'svc_etl', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            qid, qtext, warehouse_name, warehouse_size,
            start_ts, end_ts, elapsed_ms,
            queued_overload_ms,
            bytes_spilled_local, bytes_spilled_remote,
            f"ph_{hash(qtext) & 0xffff:04x}",
        ],
    )
