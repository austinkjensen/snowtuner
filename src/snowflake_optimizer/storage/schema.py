"""DuckDB schema definitions.

Organized in three logical layers:

  raw.*        Mirrors of Snowflake ACCOUNT_USAGE views + SHOW WAREHOUSES.
               Populated by ingestion Sources.

  features.*   Derived tables/views populated by FeatureTransforms.
               Recommenders read these.

  app.*        Application state: recommendations, training, routing, watermarks.

DuckDB uses schemas like namespaces.  We create all three on init.
"""
from __future__ import annotations

import duckdb

_SCHEMAS = ("raw", "features", "app")

_DDL = [
    # ── raw: mirrors Snowflake system views ────────────────────────
    """
    CREATE TABLE IF NOT EXISTS raw.query_history (
        query_id                 VARCHAR PRIMARY KEY,
        query_text               VARCHAR,
        query_type               VARCHAR,
        execution_status         VARCHAR,
        user_name                VARCHAR,
        role_name                VARCHAR,
        warehouse_name           VARCHAR,
        warehouse_size           VARCHAR,
        database_name            VARCHAR,
        schema_name              VARCHAR,
        start_time               TIMESTAMP,
        end_time                 TIMESTAMP,
        total_elapsed_ms         BIGINT,
        compilation_ms           BIGINT,
        execution_ms             BIGINT,
        queued_overload_ms       BIGINT,
        queued_provisioning_ms   BIGINT,
        bytes_scanned            BIGINT,
        bytes_spilled_to_local   BIGINT,
        bytes_spilled_to_remote  BIGINT,
        rows_produced            BIGINT,
        credits_used_cloud_services DOUBLE,
        query_hash               VARCHAR,
        query_parameterized_hash VARCHAR,
        error_message            VARCHAR,
        ingested_at              TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw.warehouse_metering_history (
        warehouse_id        VARCHAR,
        warehouse_name      VARCHAR,
        start_time          TIMESTAMP,
        end_time            TIMESTAMP,
        credits_used         DOUBLE,
        credits_used_compute DOUBLE,
        credits_used_cloud_services DOUBLE,
        ingested_at         TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (warehouse_name, start_time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw.warehouse_events_history (
        event_id            BIGINT PRIMARY KEY,
        timestamp           TIMESTAMP,
        warehouse_id        VARCHAR,
        warehouse_name      VARCHAR,
        event_name          VARCHAR,  -- RESUME_WAREHOUSE, SUSPEND_WAREHOUSE, RESIZE_WAREHOUSE, etc.
        event_reason        VARCHAR,
        event_state         VARCHAR,
        user_name           VARCHAR,
        role_name           VARCHAR,
        cluster_number      INTEGER,
        size                VARCHAR,  -- after the event (if relevant)
        ingested_at         TIMESTAMP DEFAULT current_timestamp
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw.warehouses (
        name                 VARCHAR PRIMARY KEY,
        size                 VARCHAR,
        min_cluster_count    INTEGER,
        max_cluster_count    INTEGER,
        auto_suspend_seconds INTEGER,
        auto_resume          BOOLEAN,
        scaling_policy       VARCHAR,
        state                VARCHAR,
        comment              VARCHAR,
        snapshot_at          TIMESTAMP DEFAULT current_timestamp
    )
    """,

    # ── features: derived tables populated by FeatureTransforms ────
    # Query family assignments (populated by the query_families transform)
    """
    CREATE TABLE IF NOT EXISTS features.query_families (
        parameterized_hash VARCHAR PRIMARY KEY,
        family_id          VARCHAR NOT NULL,
        representative_sql VARCHAR,
        updated_at         TIMESTAMP DEFAULT current_timestamp
    )
    """,
    # Per-warehouse active intervals — contiguous runs between RESUME and SUSPEND events.
    """
    CREATE TABLE IF NOT EXISTS features.warehouse_active_intervals (
        warehouse_name  VARCHAR,
        start_time      TIMESTAMP,
        end_time        TIMESTAMP,
        duration_sec    DOUBLE,
        PRIMARY KEY (warehouse_name, start_time)
    )
    """,
    # Per-warehouse idle gaps — the time between the last query on a warehouse
    # and the subsequent SUSPEND event.  Core input to auto_suspend tuning.
    """
    CREATE TABLE IF NOT EXISTS features.warehouse_idle_gaps (
        warehouse_name        VARCHAR,
        last_query_end_time   TIMESTAMP,
        suspend_time          TIMESTAMP,
        idle_seconds          DOUBLE,
        PRIMARY KEY (warehouse_name, last_query_end_time)
    )
    """,

    # ── app: application state ─────────────────────────────────────
    """
    CREATE SEQUENCE IF NOT EXISTS app.recommendations_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.recommendations (
        id               BIGINT PRIMARY KEY DEFAULT nextval('app.recommendations_seq'),
        generated_by     VARCHAR NOT NULL,  -- recommender name + version
        action_type      VARCHAR NOT NULL,
        target_resource  VARCHAR,           -- e.g. warehouse name
        action_payload   JSON NOT NULL,     -- full Action payload
        rationale        VARCHAR,
        evidence         JSON,              -- list of EvidenceRef
        expected_impact  JSON,              -- Impact object
        status           VARCHAR NOT NULL DEFAULT 'PROPOSED',
        apply_plan       JSON,              -- preview + rollback
        created_at       TIMESTAMP DEFAULT current_timestamp,
        updated_at       TIMESTAMP DEFAULT current_timestamp,
        applied_at       TIMESTAMP,
        applied_sql      VARCHAR,
        rollback_sql     VARCHAR,
        superseded_by    BIGINT,
        notes            VARCHAR
    )
    """,
    # Per-recommender training state — persisted so restarts don't reset progress.
    """
    CREATE TABLE IF NOT EXISTS app.training_state (
        recommender_name VARCHAR PRIMARY KEY,
        is_ready         BOOLEAN NOT NULL DEFAULT FALSE,
        readiness_report JSON,
        model_state      JSON,               -- opaque per-recommender state
        last_fit_at      TIMESTAMP,
        last_predict_at  TIMESTAMP,
        updated_at       TIMESTAMP DEFAULT current_timestamp
    )
    """,
    # Ingestion watermarks — highest start_time synced per source.
    """
    CREATE TABLE IF NOT EXISTS app.sync_watermarks (
        source_name   VARCHAR PRIMARY KEY,
        high_water    TIMESTAMP,
        last_sync_at  TIMESTAMP,
        rows_last_sync BIGINT
    )
    """,
    # Local routing rules — used by the query dispatcher in future phases.
    """
    CREATE SEQUENCE IF NOT EXISTS app.routing_rules_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.routing_rules (
        id              BIGINT PRIMARY KEY DEFAULT nextval('app.routing_rules_seq'),
        match_type      VARCHAR NOT NULL,  -- 'family' | 'user' | 'role' | 'regex'
        match_value     VARCHAR NOT NULL,
        target_warehouse VARCHAR NOT NULL,
        priority        INTEGER NOT NULL DEFAULT 100,
        enabled         BOOLEAN NOT NULL DEFAULT TRUE,
        source_recommendation_id BIGINT,
        created_at      TIMESTAMP DEFAULT current_timestamp
    )
    """,
]


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all schemas and tables if they don't exist. Safe to call repeatedly."""
    for s in _SCHEMAS:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    for stmt in _DDL:
        conn.execute(stmt)
