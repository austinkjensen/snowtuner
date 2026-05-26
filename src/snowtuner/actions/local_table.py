"""Create-local-DuckDB-table action.

Used when we want to offload a recurring read-only query to a local DuckDB cache
(populated on a refresh schedule) instead of hitting Snowflake every time.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Literal


from snowtuner.actions.base import Action, ActionType, Issue


class RefreshPolicy(str, Enum):
    MANUAL = "manual"
    EVERY_MINUTE = "every_minute"
    EVERY_HOUR = "every_hour"
    EVERY_DAY = "every_day"


class CreateLocalDuckDBTable(Action):
    type: Literal[ActionType.CREATE_LOCAL_DUCKDB_TABLE] = ActionType.CREATE_LOCAL_DUCKDB_TABLE
    table_name: str  # schema-qualified inside our DuckDB, e.g. "cache.dim_users"
    source_query: str  # SQL to run against Snowflake to populate the cache
    refresh_policy: RefreshPolicy = RefreshPolicy.EVERY_HOUR

    def target_resource(self) -> str:
        return f"local_table:{self.table_name}"

    def to_sql(self) -> str:
        # The DuckDB-side DDL that would be run locally.  The Snowflake-side
        # source_query is surfaced via dry_run_preview.
        return (
            f"CREATE OR REPLACE TABLE {self.table_name} AS\n"
            f"SELECT * FROM snowflake_scan($$\n{self.source_query.strip()}\n$$);"
        )

    def dry_run_preview(self) -> str:
        return (
            f"Materialize local DuckDB table `{self.table_name}`\n"
            f"  refresh: {self.refresh_policy.value}\n"
            f"  source query ({len(self.source_query)} chars):\n"
            f"  {self.source_query.strip()[:400]}"
        )

    def validate_against(self, context: dict[str, Any]) -> list[Issue]:
        issues: list[Issue] = []
        if "." not in self.table_name:
            issues.append(Issue(
                severity="warning",
                message="table_name should be schema-qualified (e.g. 'cache.foo').",
            ))
        return issues
