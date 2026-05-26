"""Pydantic I/O schemas for the HTTP API."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from snowtuner.recommendations.model import (
    EvidenceRef,
    Impact,
    Recommendation,
    RecommendationStatus,
)


class RecommenderInfo(BaseModel):
    name: str
    version: str
    action_type: str
    class_path: str
    required_feature_tables: list[str] = Field(default_factory=list)


class RunRequest(BaseModel):
    skip_sync: bool = True


class RunRecommenderReport(BaseModel):
    name: str
    is_ready: bool
    readiness_reason: str
    fit_completed: bool
    predictions_emitted: int = 0
    error: str | None = None


class RunResponse(BaseModel):
    feature_results: list[dict[str, Any]]
    recommender_results: list[RunRecommenderReport]


class RecommendationOut(BaseModel):
    id: int | None
    generated_by: str
    action_type: str
    target_resource: str | None
    preview: str
    sql: str
    rollback_sql: str | None = None
    rationale: str
    evidence: list[EvidenceRef]
    expected_impact: Impact
    status: RecommendationStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None
    applied_at: datetime | None = None

    @classmethod
    def from_model(cls, r: Recommendation) -> "RecommendationOut":
        rollback = None
        if hasattr(r.action, "rollback_sql"):
            rollback = r.action.rollback_sql()  # type: ignore[attr-defined]
        return cls(
            id=r.id,
            generated_by=r.generated_by,
            action_type=r.action.type.value,
            target_resource=r.action.target_resource(),
            preview=r.action.dry_run_preview(),
            sql=r.action.to_sql(),
            rollback_sql=rollback,
            rationale=r.rationale,
            evidence=r.evidence,
            expected_impact=r.expected_impact,
            status=r.status,
            created_at=r.created_at,
            updated_at=r.updated_at,
            applied_at=r.applied_at,
        )


class StatusUpdateRequest(BaseModel):
    note: str | None = None


class SeedRequest(BaseModel):
    days: int = 21
    seed: int = 42


# ── Autonomous mode ─────────────────────────────────────────────

class AutonomousConfigOut(BaseModel):
    action_type: str
    warehouse_name: str
    knob: str = "*"  # '*' = catch-all (every knob this action emits)
    enabled: bool
    confidence_threshold: float
    cooldown_hours: int
    max_rollbacks_per_week: int
    circuit_open_until: datetime | None = None
    updated_at: datetime | None = None


class AutonomousConfigUpsert(BaseModel):
    enabled: bool | None = None
    confidence_threshold: float | None = None
    cooldown_hours: int | None = None
    max_rollbacks_per_week: int | None = None


class AutonomousApplicationOut(BaseModel):
    id: int
    recommendation_id: int
    action_type: str
    warehouse_name: str | None
    applied_sql: str
    rollback_sql: str | None
    applied_at: datetime
    state: str
    error: str | None = None
    rolled_back_at: datetime | None = None
    rolled_back_sql: str | None = None
    rollback_error: str | None = None


# ── Warehouse + status views ────────────────────────────────────

class WarehouseSummaryOut(BaseModel):
    name: str
    size: str | None = None
    auto_suspend_seconds: int | None = None
    auto_resume: bool | None = None
    # Snowflake compute generation ('1' or '2').  Mirrored per-warehouse
    # via SHOW PARAMETERS at sync time.  None on older Snowflake versions
    # where the parameter isn't available.
    generation: str | None = None
    queries_in_window: int = 0
    suspend_resume_events: int = 0


class SourceFreshnessOut(BaseModel):
    name: str
    rows: int
    earliest: datetime | None = None
    latest: datetime | None = None
    last_synced_at: datetime | None = None


class StatusOut(BaseModel):
    sources: list[SourceFreshnessOut]
    warehouses: list[WarehouseSummaryOut]
    recommender_states: list[dict[str, Any]]
    recommendation_counts: dict[str, int]


# ── Schema drift (v0.2) ─────────────────────────────────────────

class SourceDriftOut(BaseModel):
    """Drift report for one ingestion source."""
    source_name: str
    source_view: str
    expected_columns: list[str] = Field(default_factory=list)
    actual_columns: list[str] = Field(default_factory=list)
    missing_from_snowflake: list[str] = Field(default_factory=list)
    extra_in_snowflake: list[str] = Field(default_factory=list)
    error: str | None = None
    is_actionable: bool = False    # missing columns will break sync


class DriftReportOut(BaseModel):
    sources: list[SourceDriftOut]
    any_actionable: bool


# ── AutomationLoop (v0.2) ───────────────────────────────────────

class StageOutcomeOut(BaseModel):
    name: str                    # 'sync' | 'features' | 'recommenders' | 'autonomous'
    started_at: datetime
    duration_seconds: float
    outcome: str                  # 'success' | 'failed' | 'skipped'
    error: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TickReportOut(BaseModel):
    started_at: datetime
    completed_at: datetime | None = None
    stages: list[StageOutcomeOut] = Field(default_factory=list)
    overall: str                  # 'running' | 'success' | 'failed' | 'skipped'
    skip_reason: str | None = None


class AutomationStatusOut(BaseModel):
    enabled: bool
    interval_seconds: int
    currently_running: bool
    next_run_at: datetime | None = None
    last_tick: TickReportOut | None = None


# ── Credentials view ────────────────────────────────────────────

class CredentialStatusOut(BaseModel):
    """Public-safe view of resolved credentials.  Never includes secrets."""
    configured: bool
    account: str | None = None
    user: str | None = None
    role: str | None = None
    warehouse: str | None = None
    auth_method: str | None = None
    source: str | None = None  # 'env' | 'keyring' | 'file'
    private_key_path: str | None = None  # path is fine; the file itself is 0600


class CredentialVerifyOut(BaseModel):
    ok: bool
    account: str | None = None
    user: str | None = None
    role: str | None = None
    warehouse: str | None = None
    region: str | None = None
    error: str | None = None


# ── Experiments (v0.2) ──────────────────────────────────────────

class RecipeInfo(BaseModel):
    """One row of GET /experiments/recipes."""
    name: str
    summary: str   # the recipe function's docstring summary


class ProposeExperimentRequest(BaseModel):
    """POST /experiments/propose body.

    The server samples historical query stats and looks up the warehouse
    config — the client only needs to say *which* recipe against *which*
    warehouse.

    Optional ``query_group_id`` (Phase 3) overrides the warehouse-based
    auto-sample with the group's members.  Static groups use their frozen
    snapshot; dynamic groups are re-evaluated at propose-time and the
    resolved IDs are then frozen onto the experiment proposal.
    """
    recipe_name: str
    target_warehouse: str
    query_group_id: int | None = None


class AbortExperimentRequest(BaseModel):
    """POST /experiments/{id}/abort body.  Reason is required so the audit
    trail is useful."""
    reason: str


class BenchmarkArmSpec(BaseModel):
    """One arm in a user-built benchmark experiment.

    Each arm fully specifies its config; the engine creates a test warehouse
    that *is* this config (no merge with a control's current state).  Fields
    left unset will be defaulted at engine time to sensible values
    (XSMALL, Gen1, QAS off) so the user doesn't have to specify everything.
    """
    name: str
    size: str | None = None              # e.g. 'XSMALL' through 'X6LARGE'
    generation: str | None = None        # '1' or '2'
    qas_state: str | None = None         # 'ON' or 'OFF'
    qas_max_scale_factor: int | None = None


class ProposeBenchmarkRequest(BaseModel):
    """POST /experiments/propose-benchmark body.

    Distinct endpoint from the recipe-based `propose` because the payload
    shape is fundamentally different: arms are absolute, not deltas; there's
    a workload source instead of a target warehouse.
    """
    hypothesis: str                       # plain-English statement of what we're testing
    workload_warehouse: str | None = None # where to sample queries from when no group is picked
    query_group_id: int | None = None     # Phase 3: pick a saved group instead of auto-sample
    arms: list[BenchmarkArmSpec]
    control_arm_name: str | None = None   # optional reference arm; None = pure Pareto comparison
    sample_size: int = 30
    reps_per_arm: int = 3


# ── Queries explorer (v0.2 slice 1) ─────────────────────────────

class QueryRow(BaseModel):
    """One row in the GET /queries list response — compact, list-view shape."""
    query_id: str
    query_text_preview: str   # truncated to ~200 chars for the list
    query_type: str | None
    execution_status: str | None
    user_name: str | None
    role_name: str | None
    warehouse_name: str | None
    warehouse_size: str | None
    start_time: datetime | None
    total_elapsed_ms: int | None
    bytes_scanned: int | None
    bytes_spilled_to_local: int | None
    bytes_spilled_to_remote: int | None
    queued_overload_ms: int | None
    query_parameterized_hash: str | None

    # sqlglot-extracted structural counts (from features.query_sql_features).
    # NULL when query_text was redacted or unparseable.
    joins_count: int | None = None
    tables_referenced_count: int | None = None
    ctes_count: int | None = None
    subqueries_count: int | None = None
    where_block_count: int | None = None
    where_predicate_count: int | None = None


class QueryDetail(BaseModel):
    """Full detail for GET /queries/{id} — everything we have in raw.query_history
    for one query, used by the side-sheet detail view."""
    query_id: str
    query_text: str
    query_type: str | None
    execution_status: str | None
    user_name: str | None
    role_name: str | None
    warehouse_name: str | None
    warehouse_size: str | None
    database_name: str | None
    schema_name: str | None
    start_time: datetime | None
    end_time: datetime | None
    total_elapsed_ms: int | None
    compilation_ms: int | None
    execution_ms: int | None
    queued_overload_ms: int | None
    queued_provisioning_ms: int | None
    bytes_scanned: int | None
    bytes_spilled_to_local: int | None
    bytes_spilled_to_remote: int | None
    query_parameterized_hash: str | None

    # sqlglot-extracted structural counts.
    joins_count: int | None = None
    tables_referenced_count: int | None = None
    ctes_count: int | None = None
    subqueries_count: int | None = None
    where_block_count: int | None = None
    where_predicate_count: int | None = None
    sql_features_parse_error: str | None = None

    # Semantic predicates (Phase 2).  Empty list means "parsed, none";
    # populated only when the query was parseable.  Tables are emitted both
    # fully-qualified (BUSINESS.SALES_OUTCOME) and short (SALES_OUTCOME).
    referenced_tables: list[str] = Field(default_factory=list)
    where_columns: list[str] = Field(default_factory=list)


class QueryFamily(BaseModel):
    """One row in GET /query-families — the parameterized-hash rollup view.

    A family is a set of queries with the same query_parameterized_hash —
    same SQL skeleton, different literal values.
    """
    query_parameterized_hash: str
    representative_query_id: str         # one query_id from this family for drilling in
    representative_sql: str              # truncated preview of one query's text
    occurrence_count: int
    mean_elapsed_ms: float | None
    p95_elapsed_ms: float | None
    total_elapsed_ms: int | None
    total_bytes_scanned: int | None
    n_spill_remote: int
    n_failed: int
    first_seen: datetime | None
    last_seen: datetime | None
    distinct_warehouses: int
    distinct_users: int


class QueryListResponse(BaseModel):
    """Wrapped response for GET /queries — includes pagination metadata."""
    rows: list[QueryRow]
    total: int                # total matching rows (for pagination UI)
    limit: int
    offset: int


class QueryFilterFacets(BaseModel):
    """GET /queries/facets — distinct values for the filter chips."""
    warehouses: list[str]
    users: list[str]
    roles: list[str]
    query_types: list[str]
    execution_statuses: list[str]
    # Semantic-predicate options (Phase 2).  Sorted by usage frequency
    # descending and capped server-side so the payload stays bounded on
    # large workloads.  ``referenced_tables`` includes both short and
    # fully-qualified forms (BUSINESS.SALES_OUTCOME and SALES_OUTCOME).
    referenced_tables: list[str] = Field(default_factory=list)
    where_columns: list[str] = Field(default_factory=list)


# ── Self-documentation (Docs tab) ───────────────────────────────

class CliParam(BaseModel):
    """One option or argument on a CLI command."""
    name: str
    kind: str           # 'option' | 'argument'
    type: str           # 'STRING' | 'INT' | 'BOOL' | 'CHOICE' | ...
    help: str = ""
    required: bool = False
    is_flag: bool = False
    default: str | None = None
    choices: list[str] | None = None    # populated when type == CHOICE
    multiple: bool = False


class CliCommand(BaseModel):
    """One node in the CLI tree.  Groups have ``subcommands`` populated."""
    name: str
    path: list[str]                     # ['snowtuner', 'autonomous', 'enable']
    help: str = ""
    short_help: str = ""
    is_group: bool = False
    params: list[CliParam] = Field(default_factory=list)
    subcommands: list["CliCommand"] = Field(default_factory=list)


CliCommand.model_rebuild()


class McpToolInfo(BaseModel):
    """One MCP tool registered on the admin server.

    ``parameters`` is the JSON Schema FastMCP generates from the tool
    function's signature; rendered as a collapsible JSON block in the UI.
    """
    name: str
    description: str = ""
    parameters: dict | None = None


# ── Query groups (slice 2) ──────────────────────────────────────

class CreateQueryGroupRequest(BaseModel):
    """POST /query-groups body.

    ``filter_spec`` carries the criteria; for static groups, we materialize
    the current member list at creation time and store it on the row.  The
    client can pass the same fields it'd otherwise use as URL filters on
    ``/queries`` — the explorer's "Save current filter as group" action just
    forwards what's in its current search-params shape, lightly normalized.
    """
    name: str
    description: str | None = None
    kind: str                                   # 'static' | 'dynamic'

    # Filter criteria.  Comma-separated strings or arrays — the endpoint
    # normalizes to the model's `list[str]` shape.  Numeric / bool / datetime
    # fields are passed directly.
    warehouse_name: list[str] | str | None = None
    user_name: list[str] | str | None = None
    role_name: list[str] | str | None = None
    query_type: list[str] | str | None = None
    execution_status: list[str] | str | None = None
    query_parameterized_hash: list[str] | str | None = None
    start_time_from: datetime | None = None
    start_time_to: datetime | None = None
    min_elapsed_ms: int | None = None
    max_elapsed_ms: int | None = None
    has_remote_spill: bool | None = None
    has_local_spill: bool | None = None
    has_queueing: bool | None = None
    search: str | None = None

    # Structural attributes (sqlglot-extracted; NULL on redacted queries).
    min_joins: int | None = None
    max_joins: int | None = None
    min_tables: int | None = None
    max_tables: int | None = None
    min_ctes: int | None = None
    max_ctes: int | None = None
    min_subqueries: int | None = None
    max_subqueries: int | None = None
    min_where_blocks: int | None = None
    max_where_blocks: int | None = None
    min_where_predicates: int | None = None
    max_where_predicates: int | None = None

    # Semantic predicates (Phase 2).  Comma-separated strings or arrays;
    # the endpoint normalizes to ``list[str]``.  ``include`` = "query must
    # touch ALL of these"; ``exclude`` = "query must touch NONE of these".
    referenced_tables_include: list[str] | str | None = None
    referenced_tables_exclude: list[str] | str | None = None
    where_columns_include: list[str] | str | None = None
    where_columns_exclude: list[str] | str | None = None
