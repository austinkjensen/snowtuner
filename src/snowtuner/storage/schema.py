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
        -- Snowflake compute generation ('1' or '2').  Mirrored per-warehouse
        -- via `SHOW PARAMETERS LIKE 'GENERATION' IN WAREHOUSE <name>` (the
        -- top-level SHOW WAREHOUSES doesn't expose it as of the versions
        -- we've tested).  NULL when the parameter query fails or the
        -- warehouse doesn't expose it.
        generation                 VARCHAR,
        -- Query Acceleration Service state ('on'/'off') and max scale
        -- factor (0 = serverless cap not set).  Mirrored via SHOW
        -- PARAMETERS LIKE 'ENABLE_QUERY_ACCELERATION' / 'QUERY_ACCELERATION_MAX_SCALE_FACTOR'.
        -- NULL when unavailable (older Snowflake versions, edition
        -- restrictions, or transient lookup failure).
        qas_state                  VARCHAR,
        qas_max_scale_factor       INTEGER,
        snapshot_at                TIMESTAMP DEFAULT current_timestamp
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
    # ── Audit event stream ────────────────────────────────────────
    # The single chronological feed of "what happened across the system at
    # time T?".  Append-only.  Not the canonical state-of-record for any
    # domain entity — those live in their own tables (recommendations,
    # experiments, autonomous_applications, sync_watermarks).  This table
    # is the cross-cutting timeline you query to answer "what changed
    # between 9am and 10am?" without joining 5 tables.
    #
    # Scope: state-changing operator actions, AutomationLoop tick
    # transitions, sync completion (per source), experiment lifecycle,
    # autonomous applies, errors.  Read-only API calls are NOT logged.
    #
    # Reset behavior: archived to ~/.snowtuner/audit-archive/events-*.json
    # before deletion, then wiped (events reference IDs that get
    # renumbered on reset, so preserving in-place would create dangling
    # references).
    """
    CREATE SEQUENCE IF NOT EXISTS app.events_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.events (
        id          BIGINT PRIMARY KEY DEFAULT nextval('app.events_seq'),
        timestamp   TIMESTAMP NOT NULL DEFAULT current_timestamp,
        actor       VARCHAR NOT NULL,    -- 'user' | 'automation' | 'engine' |
                                         -- 'sync' | 'recommender:<name>' | 'autonomous'
        action      VARCHAR NOT NULL,    -- dotted-namespace verb, e.g.
                                         -- 'recommendation.accept',
                                         -- 'experiment.run',
                                         -- 'sync.source.success',
                                         -- 'automation.tick.complete'
        subject     VARCHAR,             -- target_resource: warehouse name,
                                         -- experiment id, recommendation id,
                                         -- source name, etc.
        outcome     VARCHAR NOT NULL,    -- 'success' | 'failed' | 'skipped' | 'started'
        payload     JSON,                -- structured details (stage outcomes,
                                         -- knob changes, error context, etc.)
        error       VARCHAR              -- short error message when outcome='failed'
    )
    """,
    # Time-ordered scans are the dominant query pattern (the UI's activity
    # feed, the API's GET /events).  An index on timestamp keeps "last 100
    # events" fast even when the table grows large.
    """
    CREATE INDEX IF NOT EXISTS events_timestamp_idx
      ON app.events(timestamp DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_action_idx
      ON app.events(action, timestamp DESC)
    """,
    """
    CREATE INDEX IF NOT EXISTS events_actor_idx
      ON app.events(actor, timestamp DESC)
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
    # ── Experiments ───────────────────────────────────────────────
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
    # ── SQL feature extraction ────────────────────────────────────
    # Per-query AST-derived counts, computed by QuerySqlFeaturesTransform.
    # Joined into /queries responses and used to filter queries / define
    # groups by structural attributes.  All counts NULL when the source
    # query_text was redacted or unparseable; parse_error records the reason.
    """
    CREATE TABLE IF NOT EXISTS features.query_sql_features (
        query_id                VARCHAR PRIMARY KEY,
        joins_count             INTEGER,
        tables_referenced_count INTEGER,
        ctes_count              INTEGER,
        subqueries_count        INTEGER,
        where_block_count       INTEGER,
        where_predicate_count   INTEGER,
        parse_error             VARCHAR,
        computed_at             TIMESTAMP NOT NULL DEFAULT current_timestamp
    )
    """,
    # ── Semantic predicates ───────────────────────────────────────
    # Two side tables that record which tables a query reads from and
    # which columns it filters on (any Column node anywhere in any WHERE
    # subtree).  Populated by the same QuerySqlFeaturesTransform parse pass
    # that fills query_sql_features.  Both are list-valued (one row per
    # (query_id, ref)) so we can index them and answer "queries that touch
    # table X but don't filter on column Y" via EXISTS / NOT EXISTS.
    #
    # For schema-qualified references (FROM business.sales_outcome), we
    # emit BOTH 'BUSINESS.SALES_OUTCOME' and 'SALES_OUTCOME' rows so a
    # filter on either short or fully-qualified name matches.  All names
    # are stored UPPERCASE (Snowflake's default identifier folding).
    """
    CREATE TABLE IF NOT EXISTS features.query_referenced_tables (
        query_id   VARCHAR NOT NULL,
        table_ref  VARCHAR NOT NULL,
        PRIMARY KEY (query_id, table_ref)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS features.query_where_columns (
        query_id   VARCHAR NOT NULL,
        column_ref VARCHAR NOT NULL,
        PRIMARY KEY (query_id, column_ref)
    )
    """,
    # ── Query groups ──────────────────────────────────────────────
    # Saved sets of queries the user can apply as a workload filter or feed
    # into an experiment.  Two kinds:
    #   - static  : snapshot at creation; immutable membership.  filter_spec
    #               is preserved for provenance but not re-evaluated.
    #   - dynamic : live-evaluated against raw.query_history on every read.
    # Groups are immutable in this slice (no edit-in-place).  Versioning is
    # a future concern.
    """
    CREATE SEQUENCE IF NOT EXISTS app.query_groups_seq
    """,
    """
    CREATE TABLE IF NOT EXISTS app.query_groups (
        id                  BIGINT PRIMARY KEY
                            DEFAULT nextval('app.query_groups_seq'),
        name                VARCHAR NOT NULL,
        description         VARCHAR,
        kind                VARCHAR NOT NULL,                 -- 'static' | 'dynamic'
        filter_spec         JSON NOT NULL,                    -- QueryFilterSpec
        snapshot_query_ids  JSON,                              -- static only; JSON array of query_ids
        snapshot_at         TIMESTAMP,                         -- when the static snapshot was taken
        created_at          TIMESTAMP NOT NULL DEFAULT current_timestamp,
        created_by          VARCHAR NOT NULL DEFAULT 'user'
    )
    """,
]


def init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Create all schemas and tables if they don't exist. Safe to call repeatedly.

    Pre-release policy: no in-place migrations.  The DDL below is the canonical
    shape; if a user's on-disk database has an older schema, the right move is
    ``snowtuner reset`` (which wipes the local DuckDB file and re-initializes
    from these DDLs) followed by ``snowtuner sync`` to repopulate ``raw.*``.

    This keeps the codebase honest while we're still iterating on shapes
    weekly.  Once we cut v1.0 we'll add a real migration framework; until
    then, ``raw.*`` is fully repopulatable from Snowflake and ``app.*`` state
    is recoverable from history (recommendations, experiments) or
    regeneratable by re-running the orchestrator.
    """
    for s in _SCHEMAS:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {s}")
    for stmt in _DDL:
        conn.execute(stmt)
