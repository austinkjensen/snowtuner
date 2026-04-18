"""Registry for polymorphic Action deserialization from stored JSON payloads."""
from __future__ import annotations

from typing import Any

from snowflake_optimizer.actions.base import Action, ActionType
from snowflake_optimizer.actions.alter_warehouse import AlterWarehouse
from snowflake_optimizer.actions.create_warehouse import CreateWarehouse
from snowflake_optimizer.actions.routing_rule import CreateRoutingRule
from snowflake_optimizer.actions.local_table import CreateLocalDuckDBTable


ACTION_TYPES: dict[ActionType, type[Action]] = {
    ActionType.ALTER_WAREHOUSE: AlterWarehouse,
    ActionType.CREATE_WAREHOUSE: CreateWarehouse,
    ActionType.CREATE_ROUTING_RULE: CreateRoutingRule,
    ActionType.CREATE_LOCAL_DUCKDB_TABLE: CreateLocalDuckDBTable,
}


def action_from_dict(data: dict[str, Any]) -> Action:
    """Dispatch to the correct Action subclass based on the `type` discriminator."""
    raw_type = data.get("type")
    if raw_type is None:
        raise ValueError("Action payload missing 'type' discriminator")
    try:
        action_type = ActionType(raw_type)
    except ValueError as e:
        raise ValueError(f"Unknown action type: {raw_type!r}") from e
    cls = ACTION_TYPES[action_type]
    return cls.model_validate(data)
