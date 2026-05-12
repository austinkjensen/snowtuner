"""ALTER WAREHOUSE action — our primary tuning lever for v1."""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from snowtuner.actions.base import Action, ActionType, Issue


class WarehouseKnob(str, Enum):
    """The subset of ALTER WAREHOUSE knobs we support tuning."""
    WAREHOUSE_SIZE = "WAREHOUSE_SIZE"
    MIN_CLUSTER_COUNT = "MIN_CLUSTER_COUNT"
    MAX_CLUSTER_COUNT = "MAX_CLUSTER_COUNT"
    SCALING_POLICY = "SCALING_POLICY"
    AUTO_SUSPEND = "AUTO_SUSPEND"
    AUTO_RESUME = "AUTO_RESUME"
    # New knobs introduced for v0.2 experiments — same ALTER WAREHOUSE surface,
    # used by recipes' derived recommendations when an arm wins.
    GENERATION = "GENERATION"
    ENABLE_QUERY_ACCELERATION = "ENABLE_QUERY_ACCELERATION"
    QUERY_ACCELERATION_MAX_SCALE_FACTOR = "QUERY_ACCELERATION_MAX_SCALE_FACTOR"


class KnobChange(BaseModel):
    knob: WarehouseKnob
    current_value: Any | None = None
    proposed_value: Any


class AlterWarehouse(Action):
    type: Literal[ActionType.ALTER_WAREHOUSE] = ActionType.ALTER_WAREHOUSE
    warehouse_name: str
    changes: list[KnobChange] = Field(..., min_length=1)

    def target_resource(self) -> str:
        # Include the knob set so recommendations changing AUTO_SUSPEND on a
        # warehouse don't conflict with recommendations changing WAREHOUSE_SIZE
        # on the same warehouse — they're independent decisions.
        knobs = ",".join(sorted(c.knob.value for c in self.changes))
        return f"warehouse:{self.warehouse_name.upper()}:{knobs}"

    def target_warehouse_name(self) -> str:
        return self.warehouse_name.upper()

    def supports_autonomous_apply(self) -> bool:
        return True

    def autonomous_knobs(self) -> list[str]:
        # One entry per knob being changed.  If autonomous_config has rows for
        # every one of these (or a '*' catch-all), the runner will apply.
        return [c.knob.value for c in self.changes]

    def apply(self, client: Any) -> str:
        sql = self.to_sql()
        client.execute(sql)
        return sql

    def to_sql(self) -> str:
        body = ", ".join(_render_knob(c.knob, c.proposed_value) for c in self.changes)
        return f"ALTER WAREHOUSE {_quote_ident(self.warehouse_name)} SET {body};"

    def rollback_sql(self) -> str | None:
        rollback_parts = [
            _render_knob(c.knob, c.current_value)
            for c in self.changes
            if c.current_value is not None
        ]
        if not rollback_parts:
            return None
        body = ", ".join(rollback_parts)
        return f"ALTER WAREHOUSE {_quote_ident(self.warehouse_name)} SET {body};"

    def dry_run_preview(self) -> str:
        lines = [f"ALTER WAREHOUSE {self.warehouse_name}:"]
        for c in self.changes:
            cur = "<unset>" if c.current_value is None else str(c.current_value)
            lines.append(f"  {c.knob.value}: {cur}  →  {c.proposed_value}")
        return "\n".join(lines)

    def validate_against(self, context: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        warehouses = context.get("warehouses") or {}
        if warehouses and self.warehouse_name.upper() not in {w.upper() for w in warehouses}:
            issues.append(Issue(
                severity="error",
                message=f"Warehouse {self.warehouse_name!r} does not exist in snapshot.",
            ))
        for c in self.changes:
            if c.knob == WarehouseKnob.AUTO_SUSPEND:
                v = c.proposed_value
                if not isinstance(v, int) or v < 0:
                    issues.append(Issue(
                        severity="error",
                        message=f"AUTO_SUSPEND must be a non-negative integer (got {v!r}).",
                    ))
                elif v < 60:
                    issues.append(Issue(
                        severity="warning",
                        message=f"AUTO_SUSPEND={v}s is below Snowflake's recommended 60s floor.",
                    ))
        return issues

    @model_validator(mode="after")
    def _nonempty_changes(self) -> "AlterWarehouse":
        knobs = [c.knob for c in self.changes]
        if len(knobs) != len(set(knobs)):
            raise ValueError("Duplicate knob in AlterWarehouse.changes")
        return self


def _quote_ident(name: str) -> str:
    if name.isidentifier() and name.isascii():
        return name.upper()
    escaped = name.replace('"', '""')
    return f'"{escaped}"'


def _render_knob(knob: WarehouseKnob, value: Any) -> str:
    if knob == WarehouseKnob.AUTO_RESUME:
        return f"AUTO_RESUME = {str(bool(value)).upper()}"
    if knob == WarehouseKnob.WAREHOUSE_SIZE:
        return f"WAREHOUSE_SIZE = '{value}'"
    if knob == WarehouseKnob.SCALING_POLICY:
        return f"SCALING_POLICY = '{value}'"
    if knob == WarehouseKnob.GENERATION:
        return f"GENERATION = '{value}'"
    if knob == WarehouseKnob.ENABLE_QUERY_ACCELERATION:
        return f"ENABLE_QUERY_ACCELERATION = {str(bool(value)).upper()}"
    if knob == WarehouseKnob.QUERY_ACCELERATION_MAX_SCALE_FACTOR:
        return f"QUERY_ACCELERATION_MAX_SCALE_FACTOR = {int(value)}"
    return f"{knob.value} = {int(value)}"
