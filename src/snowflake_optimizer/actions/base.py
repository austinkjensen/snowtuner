"""Base types for actions.

An Action is a typed description of something the optimizer proposes the user do.
Each Action subclass owns its own SQL rendering, dry-run preview, and validation
so that recommenders never emit raw SQL directly.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionType(str, Enum):
    ALTER_WAREHOUSE = "ALTER_WAREHOUSE"
    CREATE_WAREHOUSE = "CREATE_WAREHOUSE"
    CREATE_ROUTING_RULE = "CREATE_ROUTING_RULE"
    CREATE_LOCAL_DUCKDB_TABLE = "CREATE_LOCAL_DUCKDB_TABLE"


class Issue(BaseModel):
    """A validation issue raised by Action.validate()."""
    severity: str  # 'error' | 'warning' | 'info'
    message: str


class ApplyPlan(BaseModel):
    """Preview of what will change + how to roll back."""
    preview: str
    rollback_sql: str | None = None
    rollback_description: str | None = None


class Action(BaseModel):
    """Base class for all actions.  Subclasses set a literal `type` discriminator."""
    type: ActionType = Field(..., description="Discriminator for polymorphic actions")

    # ---- each subclass must implement these ----
    def target_resource(self) -> str | None:
        """Name/identifier of the resource this action targets (warehouse name, rule id, etc.).
        Used for deduping overlapping proposals."""
        raise NotImplementedError

    def to_sql(self) -> str:
        """Render the exact SQL the user would run to apply this action."""
        raise NotImplementedError

    def dry_run_preview(self) -> str:
        """Human-readable diff-style preview of what this action changes."""
        raise NotImplementedError

    def validate_against(self, context: dict[str, Any]) -> list[Issue]:
        """Validate this action in context (e.g. referenced warehouse exists).
        Default is no-op; subclasses override as needed."""
        return []

    # ---- persistence ----
    def to_payload(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON storage."""
        return self.model_dump(mode="json")
