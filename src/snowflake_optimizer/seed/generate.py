"""Generate plausible fake Snowflake telemetry into the raw.* tables.

Produces three fictional warehouses with different idle patterns so the
auto_suspend_tuner emits distinct recommendations for each:

  ANALYTICS_WH   — currently AUTO_SUSPEND=600, but idles only ~90s before resume.
                    Recommender should propose lowering.
  ETL_WH         — currently AUTO_SUSPEND=60, but gets re-resumed within ~30s
                    often. Recommender may propose raising.
  BI_WH          — currently AUTO_SUSPEND=300, matches actual ~300s idle pattern.
                    Recommender should NOT fire (within MIN_DELTA_SECONDS).
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

    # Clear
    for tbl in ("raw.query_history", "raw.warehouse_events_history",
                "raw.warehouse_metering_history", "raw.warehouses"):
        conn.execute(f"DELETE FROM {tbl}")

    # --- warehouses ---
    # Each row: (name, size, current_auto_suspend_s, reactivation_mean_s,
    #           idle_before_suspend_mean_s).  The idle-before-suspend mean
    #           should track current_auto_suspend since real Snowflake
    #           triggers the suspend automatically at that threshold.
    warehouses = [
        ("ANALYTICS_WH", "MEDIUM",  600, 120, 600),  # over-provisioned → recommend lower
        ("ETL_WH",       "LARGE",   300,  50, 300),  # bursty → recommend lower
        ("BI_WH",        "SMALL",   300, 360, 300),  # well-tuned → no proposal
    ]
    for name, size, auto_susp, _, _ in warehouses:
        conn.execute(
            """
            INSERT INTO raw.warehouses
              (name, size, min_cluster_count, max_cluster_count,
               auto_suspend_seconds, auto_resume, scaling_policy, state, comment)
            VALUES (?, ?, 1, 1, ?, TRUE, 'STANDARD', 'SUSPENDED', NULL)
            """,
            [name, size, auto_susp],
        )

    # --- events + queries ---
    now = datetime.now().replace(microsecond=0)
    event_id = 0
    query_counter = 0

    for name, size, _, reactivation_mean, idle_mean in warehouses:
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
            n_queries = rng.randint(3, 15)
            last_q_end = q_start
            for _ in range(n_queries):
                dur_ms = rng.randint(200, 8000)
                q_end = q_start + timedelta(milliseconds=dur_ms)
                query_counter += 1
                qid = f"q_{name}_{query_counter:07d}"
                qtext = rng.choice(SAMPLE_QUERIES)
                conn.execute(
                    """
                    INSERT INTO raw.query_history
                      (query_id, query_text, query_type, execution_status,
                       user_name, warehouse_name, warehouse_size,
                       start_time, end_time, total_elapsed_ms,
                       query_parameterized_hash)
                    VALUES (?, ?, 'SELECT', 'SUCCESS', 'svc_etl', ?, ?, ?, ?, ?, ?)
                    """,
                    [qid, qtext, name, size, q_start, q_end, dur_ms,
                     f"ph_{hash(qtext) & 0xffff:04x}"],
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

    counts: dict[str, int] = {}
    for tbl in ("raw.query_history", "raw.warehouse_events_history", "raw.warehouses"):
        counts[tbl] = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    return counts
