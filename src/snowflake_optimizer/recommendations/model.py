"""Recommendation domain model."""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from snowflake_optimizer.actions.base import Action
from snowflake_optimizer.actions.registry import action_from_dict


class RecommendationStatus(str, Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    APPLIED = "APPLIED"
    ROLLED_BACK = "ROLLED_BACK"
    SUPERSEDED = "SUPERSEDED"


class EvidenceRef(BaseModel):
    """A pointer to data that supports this recommendation."""
    kind: str  # e.g. 'query_history', 'warehouse_idle_gaps', 'metering'
    description: str
    filters: dict[str, Any] = Field(default_factory=dict)
    metric: str | None = None
    value: float | None = None


class Impact(BaseModel):
    """Expected outcome if the recommendation is applied."""
    credits_delta_daily: float | None = None  # negative = savings
    p50_latency_delta_ms: float | None = None
    confidence: float = 0.0  # [0, 1]
    notes: str | None = None


class Recommendation(BaseModel):
    id: int | None = None
    generated_by: str
    action: Action
    rationale: str
    evidence: list[EvidenceRef] = Field(default_factory=list)
    expected_impact: Impact = Field(default_factory=Impact)
    status: RecommendationStatus = RecommendationStatus.PROPOSED
    created_at: datetime | None = None
    updated_at: datetime | None = None
    applied_at: datetime | None = None
    applied_sql: str | None = None
    rollback_sql: str | None = None
    superseded_by: int | None = None
    notes: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "Recommendation":
        """Hydrate from a DuckDB row dict."""
        import json

        payload = row["action_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        action = action_from_dict(payload)

        evidence_raw = row.get("evidence")
        if isinstance(evidence_raw, str):
            evidence_raw = json.loads(evidence_raw)
        evidence = [EvidenceRef.model_validate(e) for e in (evidence_raw or [])]

        impact_raw = row.get("expected_impact")
        if isinstance(impact_raw, str):
            impact_raw = json.loads(impact_raw)
        impact = Impact.model_validate(impact_raw) if impact_raw else Impact()

        return cls(
            id=row["id"],
            generated_by=row["generated_by"],
            action=action,
            rationale=row.get("rationale") or "",
            evidence=evidence,
            expected_impact=impact,
            status=RecommendationStatus(row["status"]),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            applied_at=row.get("applied_at"),
            applied_sql=row.get("applied_sql"),
            rollback_sql=row.get("rollback_sql"),
            superseded_by=row.get("superseded_by"),
            notes=row.get("notes"),
        )
