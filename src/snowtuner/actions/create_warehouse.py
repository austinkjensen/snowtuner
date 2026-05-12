"""CREATE WAREHOUSE action."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from snowtuner.actions.base import Action, ActionType, Issue


class CreateWarehouseConfig(BaseModel):
    warehouse_size: str = "X-SMALL"
    auto_suspend: int = 60
    auto_resume: bool = True
    min_cluster_count: int = 1
    max_cluster_count: int = 1
    scaling_policy: str = "STANDARD"
    initially_suspended: bool = True
    comment: str | None = None


class CreateWarehouse(Action):
    type: Literal[ActionType.CREATE_WAREHOUSE] = ActionType.CREATE_WAREHOUSE
    name: str
    config: CreateWarehouseConfig = Field(default_factory=CreateWarehouseConfig)

    def target_resource(self) -> str:
        return f"warehouse:{self.name.upper()}"

    def to_sql(self) -> str:
        c = self.config
        clauses = [
            f"WAREHOUSE_SIZE = '{c.warehouse_size}'",
            f"AUTO_SUSPEND = {c.auto_suspend}",
            f"AUTO_RESUME = {str(c.auto_resume).upper()}",
            f"MIN_CLUSTER_COUNT = {c.min_cluster_count}",
            f"MAX_CLUSTER_COUNT = {c.max_cluster_count}",
            f"SCALING_POLICY = '{c.scaling_policy}'",
            f"INITIALLY_SUSPENDED = {str(c.initially_suspended).upper()}",
        ]
        if c.comment:
            clauses.append(f"COMMENT = '{c.comment.replace(chr(39), chr(39)*2)}'")
        return f"CREATE WAREHOUSE {self.name.upper()} WITH {' '.join(clauses)};"

    def dry_run_preview(self) -> str:
        return f"CREATE WAREHOUSE {self.name} with:\n{self.config.model_dump_json(indent=2)}"

    def validate_against(self, context: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        existing = {w.upper() for w in (context.get("warehouses") or {})}
        if self.name.upper() in existing:
            issues.append(Issue(
                severity="error",
                message=f"Warehouse {self.name!r} already exists.",
            ))
        return issues
