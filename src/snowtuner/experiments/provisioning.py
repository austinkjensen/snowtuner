"""Side-by-side test warehouse provisioning and teardown.

Each arm of an experiment runs against its own throwaway warehouse so the
production control warehouse is never touched.  Names are deterministic and
namespaced so a crash-recovery sweep can find and clean them.

Naming convention
-----------------
``SNOWTUNER_EXP_{experiment_id}_{arm_name}``

Capped at Snowflake's 255-char identifier limit (we'd have to try very hard
to exceed it).  Uppercased to match Snowflake's default unquoted-identifier
folding so identity comparisons in QUERY_HISTORY join cleanly.
"""
from __future__ import annotations

from dataclasses import dataclass

from snowtuner.experiments.arm import Arm
from snowtuner.experiments.config_delta import WarehouseConfig


@dataclass(frozen=True)
class ProvisionedArm:
    """A successfully-created test warehouse bound to an arm."""
    arm: Arm
    warehouse_name: str           # the SNOWTUNER_EXP_* name
    config: WarehouseConfig       # the realized config (control merged with delta)


def test_warehouse_name(experiment_id: int, arm_name: str) -> str:
    """Deterministic, recoverable name for an arm's test warehouse.

    The experiment_id is the recovery anchor — a janitor can list all
    ``SNOWTUNER_EXP_{id}_*`` warehouses and drop them by experiment.
    """
    return f"SNOWTUNER_EXP_{experiment_id}_{arm_name}".upper()


def render_create_warehouse_sql(name: str, config: WarehouseConfig) -> str:
    """Build a CREATE WAREHOUSE statement from a merged config.

    Always uses INITIALLY_SUSPENDED = TRUE — the engine resumes the warehouse
    on first replay, so we don't pay for idle time between provisioning and
    the first query.

    AUTO_SUSPEND defaults to 60s if the control didn't specify one — we don't
    want test warehouses to linger after a crash.
    """
    clauses = config.to_create_warehouse_clauses()
    # Force-suspend on provisioning to avoid paying for idle.
    clauses["INITIALLY_SUSPENDED"] = True
    # Failsafe auto-suspend so crash-orphaned warehouses don't burn credits.
    clauses.setdefault("AUTO_SUSPEND", 60)
    clauses.setdefault("AUTO_RESUME", True)

    parts: list[str] = []
    for k, v in clauses.items():
        parts.append(_render_clause(k, v))
    body = "\n  ".join(parts)
    return f"CREATE WAREHOUSE {name}\n  {body}"


def _render_clause(key: str, value) -> str:
    if isinstance(value, bool):
        return f"{key} = {'TRUE' if value else 'FALSE'}"
    if isinstance(value, (int, float)):
        return f"{key} = {value}"
    # Strings: quote unless it's the WAREHOUSE_SIZE token (which is identifier-like
    # but Snowflake accepts both quoted and unquoted forms; quote for safety).
    return f"{key} = '{value}'"


def render_drop_warehouse_sql(name: str) -> str:
    """IF EXISTS so the janitor is idempotent."""
    return f"DROP WAREHOUSE IF EXISTS {name}"
