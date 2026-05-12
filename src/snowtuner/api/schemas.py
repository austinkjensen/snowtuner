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
    """
    recipe_name: str
    target_warehouse: str


class AbortExperimentRequest(BaseModel):
    """POST /experiments/{id}/abort body.  Reason is required so the audit
    trail is useful."""
    reason: str
