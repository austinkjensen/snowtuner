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

    def target_warehouse_name(self) -> str | None:
        """Warehouse name this action affects, if any.  Used by autonomous-mode
        config matching.  Subclasses operating on warehouses should return the
        warehouse name; everything else returns None.  (Named with the ``_name``
        suffix to avoid colliding with Pydantic fields named ``target_warehouse``
        on subclasses such as ``CreateRoutingRule``.)"""
        return None

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

    def supports_autonomous_apply(self) -> bool:
        """Whether autonomous mode can apply this action type.  Default False;
        subclasses override when they have a tested apply path."""
        return False

    def autonomous_knobs(self) -> list[str]:
        """Per-knob identifiers this action affects, for autonomous-mode gating.

        For action types with multiple independently-controllable knobs (e.g.
        ``ALTER_WAREHOUSE`` distinguishes ``AUTO_SUSPEND`` from
        ``WAREHOUSE_SIZE``), subclasses return one entry per knob being
        changed.  Atomic action types return ``['*']`` — the catch-all that
        matches every knob in autonomous_config.

        The ``AutonomousRunner`` looks up an ``autonomous_config`` row for
        each knob and only proceeds if every knob is enabled.  Most
        recommendations touch a single knob, but the multi-element case is
        supported for future composite actions.
        """
        return ["*"]

    def apply(self, client: Any) -> str:
        """Execute the action against Snowflake.  Returns the SQL that ran.

        Raises NotImplementedError when the action doesn't support autonomous
        application — better to fail loudly than silently swallow the call.
        """
        raise NotImplementedError(
            f"Action type {self.type.value!r} does not support autonomous apply"
        )

    # ---- persistence ----
    def to_payload(self) -> dict[str, Any]:
        """Serialize to a dict suitable for JSON storage."""
        return self.model_dump(mode="json")
