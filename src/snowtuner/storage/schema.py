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
        event_id         BIGINT PRIMARY KEY,  -- synthetic; sha256-derived from natural key
        timestamp        TIMESTAMP,
        warehouse_id     VARCHAR,
        warehouse_name   VARCHAR,
        cluster_number   INTEGER,  -- nullable; NULL for warehouse-level events
        event_name       VARCHAR,  -- RESUME_WAREHOUSE, SUSPEND_WAREHOUSE, RESIZE_WAREHOUSE, etc.
        event_reason     VARCHAR,
        event_state      VARCHAR,
        user_name        VARCHAR,
        role_name        VARCHAR,
        query_id         VARCHAR,
        size             VARCHAR,  -- after the event, if relevant
        cluster_count    INTEGER,
        ingested_at      TIMESTAMP DEFAULT current_timestamp
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
    # ── Autonomous mode ───────────────────────────────────────────
    # Per (action_type, warehouse_name) opt-in for autonomous apply.
    # Per-(action_type, warehouse_name, knob) autonomous-mode config.
    #
    # ``knob`` lets us gate granularly inside an action type — e.g. enable
    # autonomous AUTO_SUSPEND tuning on a warehouse but keep WAREHOUSE_SIZE
    # changes advisory.  Use ``'*'`` for "applies to every knob this action
    # produces" (matches the warehouse_name='*' catch-all convention).
    # Likewise warehouse_name='*' = catch-all for that action_type.
    # We use the literal '*' rather than NULL because DuckDB PRIMARY KEY
    # columns disallow NULL.
    """
    CREATE TABLE IF NOT EXISTS app.autonomous_config (
        action_type            VARCHAR NOT NULL,
        warehouse_name         VARCHAR NOT NULL,  -- '*' = catch-all
        knob                   VARCHAR NOT NULL DEFAULT '*',  -- '*' = every knob
        enabled                BOOLEAN NOT NULL DEFAULT FALSE,
        confidence_threshold   DOUBLE NOT NULL DEFAULT 0.85,
        cooldown_hours         INTEGER NOT NULL DEFAULT 24,
        max_rollbacks_per_week INTEGER NOT NULL DEFAULT 2,
        circuit_open_until     TIMESTAMP,        -- NULL when circuit is closed
        updated_at             TIMESTAMP DEFAULT current_timestamp,
        PRIMARY KEY (action_type, warehouse_name, knob)
    )
    """,
    """
    CREATE SEQUENCE IF NOT EXISTS app.autonomous_applications_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.autonomous_applications (
        id                BIGINT PRIMARY KEY
                          DEFAULT nextval('app.autonomous_applications_seq'),
        recommendation_id BIGINT NOT NULL,
        action_type       VARCHAR NOT NULL,
        warehouse_name    VARCHAR,
        applied_sql       VARCHAR NOT NULL,
        rollback_sql      VARCHAR,
        applied_at        TIMESTAMP NOT NULL DEFAULT current_timestamp,
        state             VARCHAR NOT NULL DEFAULT 'APPLIED',  -- APPLIED | ROLLED_BACK | FAILED
        error             VARCHAR,
        rolled_back_at    TIMESTAMP,
        rolled_back_sql   VARCHAR,
        rollback_error    VARCHAR
    )
    """,
    # ── v0.2 experiments ──────────────────────────────────────────
    # The full ProposedExperiment is stored as a single JSON blob (`spec`)
    # so the engine can reproduce the run from the row alone, and so we don't
    # have to evolve the schema each time the spec gains a field.  Reports
    # are likewise stored as JSON.  Per-(arm, query, rep) observations live in
    # the separate experiment_runs table because they're queryable for the
    # statistical aggregation step.
    """
    CREATE SEQUENCE IF NOT EXISTS app.experiments_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.experiments (
        id                          BIGINT PRIMARY KEY
                                    DEFAULT nextval('app.experiments_seq'),
        kind                        VARCHAR NOT NULL DEFAULT 'tuning',  -- tuning | benchmark
        recipe_name                 VARCHAR NOT NULL,
        target_warehouse            VARCHAR,                            -- nullable: benchmark may have none
        workload_warehouse          VARCHAR,                            -- benchmark workload source; falls back to target_warehouse
        hypothesis                  VARCHAR,
        proposed_by                 VARCHAR NOT NULL,
        status                      VARCHAR NOT NULL DEFAULT 'PROPOSED',
        spec                        JSON NOT NULL,
        cost_estimate               JSON NOT NULL,
        proposed_at                 TIMESTAMP NOT NULL DEFAULT current_timestamp,
        accepted_at                 TIMESTAMP,
        started_at                  TIMESTAMP,
        completed_at                TIMESTAMP,
        aborted_reason              VARCHAR,
        actual_cost_credits         DOUBLE,
        cost_cap_hit                BOOLEAN DEFAULT FALSE,
        report                      JSON,
        derived_recommendation_id   BIGINT,
        test_warehouses             JSON,
        test_warehouses_cleaned     BOOLEAN DEFAULT FALSE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app.experiment_runs (
        experiment_id           BIGINT NOT NULL,
        arm_name                VARCHAR NOT NULL,
        rep_index               INTEGER NOT NULL,
        sampled_query_id        VARCHAR NOT NULL,
        parameterized_hash      VARCHAR,
        replay_query_id         VARCHAR,
        elapsed_ms              BIGINT,
        queued_overload_ms      BIGINT,
        bytes_scanned           BIGINT,
        bytes_spilled_local     BIGINT,
        bytes_spilled_remote    BIGINT,
        credits_used_estimate   DOUBLE,
        status                  VARCHAR NOT NULL,  -- success | failed | excluded
        error_message           VARCHAR,
        started_at              TIMESTAMP,
        completed_at            TIMESTAMP,
        PRIMARY KEY (experiment_id, arm_name, rep_index, sampled_query_id)
    )
    """,
]


def _pre_create_migrations(conn: duckdb.DuckDBPyConnection) -> None:
    """Forward-only schema migrations applied before CREATE TABLE IF NOT EXISTS DDLs.

    We don't support upgrading from a released version yet, so each migration
    here just drops a table when its old shape is detected and lets the DDL
    below re-create it with the new shape.  This is acceptable pre-release
    because raw.* is fully repopulatable from a sync.
    """
    # Migration 1: drop raw.warehouse_events_history if its shape doesn't
    # match the current design (synthetic event_id PK + cluster_count column).
    # We've gone through a couple of schema iterations on this table:
    #   v1: event_id BIGINT PK pulled from Snowflake (Snowflake doesn't expose it)
    #   v2: composite PK over (warehouse_id, timestamp, event_name, cluster_number)
    #   v3 (current): synthetic event_id BIGINT PK we hash ourselves
    # If the existing table is missing either marker (event_id or cluster_count),
    # it's a pre-v3 shape and we drop+recreate.
    cols_rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'raw' AND table_name = 'warehouse_events_history'
        """
    ).fetchall()
    if cols_rows:
        col_names = {c[0] for c in cols_rows}
        if "event_id" not in col_names or "cluster_count" not in col_names:
            conn.execute("DROP TABLE raw.warehouse_events_history")
            conn.execute(
                "DELETE FROM app.sync_watermarks WHERE source_name = 'warehouse_events_history'"
            )

    # Migration 2: app.autonomous_config gained a `knob` column for per-knob
    # granularity (so e.g. ALTER_WAREHOUSE/AUTO_SUSPEND can be autonomous
    # without auto-applying WAREHOUSE_SIZE on the same warehouse).  Existing
    # rows are migrated to ``knob = '*'`` (catch-all), preserving prior behavior.
    cfg_cols = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'autonomous_config'
        ORDER BY ordinal_position
        """
    ).fetchall()
    if cfg_cols:
        cfg_col_names = [c[0] for c in cfg_cols]
        if "knob" not in cfg_col_names:
            saved = conn.execute(
                "SELECT * FROM app.autonomous_config"
            ).fetchall()
            conn.execute("DROP TABLE app.autonomous_config")
            # The CREATE in _DDL below will rebuild with the new shape.  We
            # pre-create here so the row replays land in the new shape; the
            # IF NOT EXISTS in the DDL step then becomes a no-op.
            conn.execute(
                """
                CREATE TABLE app.autonomous_config (
                    action_type            VARCHAR NOT NULL,
                    warehouse_name         VARCHAR NOT NULL,
                    knob                   VARCHAR NOT NULL DEFAULT '*',
                    enabled                BOOLEAN NOT NULL DEFAULT FALSE,
                    confidence_threshold   DOUBLE NOT NULL DEFAULT 0.85,
                    cooldown_hours         INTEGER NOT NULL DEFAULT 24,
                    max_rollbacks_per_week INTEGER NOT NULL DEFAULT 2,
                    circuit_open_until     TIMESTAMP,
                    updated_at             TIMESTAMP DEFAULT current_timestamp,
                    PRIMARY KEY (action_type, warehouse_name, knob)
                )
                """
            )
            for row in saved:
                d = dict(zip(cfg_col_names, row))
                conn.execute(
                    """
                    INSERT INTO app.autonomous_config
                      (action_type, warehouse_name, knob, enabled,
                       confidence_threshold, cooldown_hours,
                       max_rollbacks_per_week, circuit_open_until, updated_at)
                    VALUES (?, ?, '*', ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        d["action_type"],
                        d["warehouse_name"],
                        d.get("enabled", False),
                        d.get("confidence_threshold", 0.85),
                        d.get("cooldown_hours", 24),
                        d.get("max_rollbacks_per_week", 2),
                        d.get("circuit_open_until"),
                        d.get("updated_at"),
                    ],
                )

    # Migration 3: app.experiments gained kind + workload_warehouse columns
    # for benchmark-kind experiments.  Default kind = 'tuning' so existing
    # rows keep their semantics; target_warehouse becomes nullable.  These
    # are additive (ADD COLUMN + ALTER COLUMN DROP NOT NULL), so the
    # existing data is preserved.
    exp_cols_rows = conn.execute(
        """
        SELECT column_name FROM information_schema.columns
        WHERE table_schema = 'app' AND table_name = 'experiments'
        """
    ).fetchall()
    if exp_cols_rows:
        exp_col_names = {c[0] for c in exp_cols_rows}
        if "kind" not in exp_col_names:
            conn.execute(
                "ALTER TABLE app.experiments ADD COLUMN kind VARCHAR NOT NULL DEFAULT 'tuning'"
            )
        if "workload_warehouse" not in exp_col_names:
            conn.execute(
                "ALTER TABLE app.experiments ADD COLUMN workload_warehouse VARCHAR"
            )
        # Drop NOT NULL on target_warehouse — benchmark experiments may omit it.
        # DuckDB's ALTER TABLE syntax: ALTER COLUMN ... DROP NOT NULL.
        try:
            conn.execute(
                "ALTER TABLE app.experiments ALTER COLUMN target_warehouse DROP NOT NULL"
            )
        except Exception:
            # Already nullable, or DuckDB version doesn't support this syntax.
            # Either way it's not catastrophic — INSERTs from the store still
            # work; the constraint is just stricter than we'd like for benchmark.
            pass


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all schemas and tables if they don't exist. Safe to call repeatedly."""
    for s in _SCHEMAS:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    # Migrations run before DDLs so the DDLs can recreate dropped tables.
    try:
        _pre_create_migrations(conn)
    except duckdb.CatalogException:
        # First-ever init: app.sync_watermarks may not exist yet.  Safe to skip.
        pass
    for stmt in _DDL:
        conn.execute(stmt)
