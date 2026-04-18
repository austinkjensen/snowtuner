"""Create-routing-rule action.

A routing rule is a local record (in app.routing_rules) that a future query
dispatcher will consult to prepend `USE WAREHOUSE <target>` to matching queries.
The 'SQL' rendered here is therefore what Snowflake itself would see *when a
matching query runs*, plus the DuckDB insert that installs the rule.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel

from snowflake_optimizer.actions.base import Action, ActionType, Issue


class RoutingMatchType(str, Enum):
    FAMILY = "family"
    USER = "user"
    ROLE = "role"
    REGEX = "regex"


class RoutingMatch(BaseModel):
    match_type: RoutingMatchType
    match_value: str


class CreateRoutingRule(Action):
    type: Literal[ActionType.CREATE_ROUTING_RULE] = ActionType.CREATE_ROUTING_RULE
    match: RoutingMatch
    target_warehouse: str
    priority: int = 100

    def target_resource(self) -> str:
        return f"routing:{self.match.match_type.value}:{self.match.match_value}"

    def to_sql(self) -> str:
        # What a routed query will end up running (illustrative, not executed).
        return f"USE WAREHOUSE {self.target_warehouse.upper()};  -- for queries matching {self.match.match_type.value}={self.match.match_value!r}"

    def dry_run_preview(self) -> str:
        return (
            f"Install routing rule (priority {self.priority}):\n"
            f"  WHEN {self.match.match_type.value} = {self.match.match_value!r}\n"
            f"  ROUTE TO warehouse {self.target_warehouse!r}"
        )

    def validate_against(self, context: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        warehouses = context.get("warehouses") or {}
        if warehouses and self.target_warehouse.upper() not in {w.upper() for w in warehouses}:
            issues.append(Issue(
                severity="error",
                message=f"Target warehouse {self.target_warehouse!r} does not exist.",
            ))
        return issues
