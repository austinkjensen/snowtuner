"""Snowtuner demo mode - cooked workloads run against real Snowflake.

Unlike ``snowtuner seed`` (which pokes synthetic rows into the local DuckDB
``raw.*`` tables, bypassing Snowflake entirely), demo mode provisions a small
set of throwaway warehouses on the user's real Snowflake account and runs
intentionally-shaped query patterns against them.  The patterns are
engineered to trip specific recommender rules so a new user can see what
snowtuner finds, end-to-end, on their own account.

Demo mode is OPT-IN and OWNED.  All warehouses use the prefix
``SNOWTUNER_DEMO_`` and are created by the snowtuner role, which means
``snowtuner demo teardown`` can drop them unilaterally.  An AUTO_SUSPEND
ceiling of 120s on every demo warehouse caps the idle-credit blast radius
even if the user kills the process mid-run.

Important honesty:  cooked workloads are NOT representative of real
Snowflake usage.  The demo's job is to *prove the optimizer works*, not to
predict what it will find on your real account.  Real recommendations come
from ``snowtuner sync && snowtuner run`` against your actual workload.

Public surface:
    from snowtuner.demo import DEMO_SPECS, run_demo, teardown_demo

The CLI wrapper is in ``snowtuner.cli`` (``snowtuner demo seed | status |
teardown``).
"""
from __future__ import annotations

from snowtuner.demo.warehouses import (
    DEMO_SPECS,
    DEMO_WAREHOUSE_PREFIX,
    DemoWarehouseSpec,
)
from snowtuner.demo.workloads import (
    DEMO_WORKLOADS,
    DemoWorkload,
    WorkloadResult,
)

__all__ = [
    "DEMO_SPECS",
    "DEMO_WAREHOUSE_PREFIX",
    "DEMO_WORKLOADS",
    "DemoWarehouseSpec",
    "DemoWorkload",
    "WorkloadResult",
]
