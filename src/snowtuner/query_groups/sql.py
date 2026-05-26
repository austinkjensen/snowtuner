"""SQL building for ``QueryFilterSpec`` → DuckDB WHERE clauses.

Extracted from ``snowtuner.api.app`` so non-API callers (notably the
experiments workload resolver) can share the same canonical filter
translation without pulling in FastAPI.

Column references in the generated WHERE clauses are qualified to:
  * ``q.*`` — ``raw.query_history`` (the always-aliased base table)
  * ``f.*`` — ``features.query_sql_features`` (joined LEFT in callers)

Callers are responsible for that ``LEFT JOIN`` if they use structural
filters; semantic-predicate sub-selects reference the two side tables
directly by full name (``features.query_referenced_tables`` etc.).
"""
from __future__ import annotations

from typing import Any

from snowtuner.query_groups.model import QueryFilterSpec


def build_filter_from_spec(spec: QueryFilterSpec) -> tuple[str, list[Any]]:
    """Build a SQL WHERE-clause body + bind params from a ``QueryFilterSpec``.

    Returns ``(where_body, params)`` where ``where_body`` is an empty string
    when no filters are set.  Compose with a leading ``WHERE`` only if
    non-empty.
    """
    clauses: list[str] = []
    params: list[Any] = []

    def _in_clause(col: str, values: list[str] | None) -> None:
        if not values:
            return
        placeholders = ", ".join(["?"] * len(values))
        clauses.append(f"{col} IN ({placeholders})")
        params.extend(values)

    _in_clause("q.warehouse_name", spec.warehouse_name)
    _in_clause("q.user_name", spec.user_name)
    _in_clause("q.role_name", spec.role_name)
    _in_clause("q.query_type", spec.query_type)
    _in_clause("q.execution_status", spec.execution_status)
    _in_clause("q.query_parameterized_hash", spec.query_parameterized_hash)

    if spec.start_time_from:
        clauses.append("q.start_time >= ?")
        params.append(spec.start_time_from)
    if spec.start_time_to:
        clauses.append("q.start_time <= ?")
        params.append(spec.start_time_to)
    if spec.min_elapsed_ms is not None:
        clauses.append("q.total_elapsed_ms >= ?")
        params.append(spec.min_elapsed_ms)
    if spec.max_elapsed_ms is not None:
        clauses.append("q.total_elapsed_ms <= ?")
        params.append(spec.max_elapsed_ms)
    if spec.has_remote_spill is True:
        clauses.append("q.bytes_spilled_to_remote > 0")
    elif spec.has_remote_spill is False:
        clauses.append("(q.bytes_spilled_to_remote IS NULL OR q.bytes_spilled_to_remote = 0)")
    if spec.has_local_spill is True:
        clauses.append("q.bytes_spilled_to_local > 0")
    elif spec.has_local_spill is False:
        clauses.append("(q.bytes_spilled_to_local IS NULL OR q.bytes_spilled_to_local = 0)")
    if spec.has_queueing is True:
        clauses.append("q.queued_overload_ms > 0")
    elif spec.has_queueing is False:
        clauses.append("(q.queued_overload_ms IS NULL OR q.queued_overload_ms = 0)")
    if spec.search:
        clauses.append("lower(q.query_text) LIKE ?")
        params.append(f"%{spec.search.lower()}%")

    # Structural filters (Phase 1) — joined from features.query_sql_features.
    # ``f.col >= ?`` naturally excludes rows where the feature is NULL
    # (redacted/unparseable queries), which is what we want.
    def _structural_range(col: str, lo: int | None, hi: int | None) -> None:
        if lo is not None:
            clauses.append(f"f.{col} >= ?")
            params.append(lo)
        if hi is not None:
            clauses.append(f"f.{col} <= ?")
            params.append(hi)

    _structural_range("joins_count", spec.min_joins, spec.max_joins)
    _structural_range("tables_referenced_count", spec.min_tables, spec.max_tables)
    _structural_range("ctes_count", spec.min_ctes, spec.max_ctes)
    _structural_range("subqueries_count", spec.min_subqueries, spec.max_subqueries)
    _structural_range("where_block_count", spec.min_where_blocks, spec.max_where_blocks)
    _structural_range(
        "where_predicate_count",
        spec.min_where_predicates, spec.max_where_predicates,
    )

    # ── Semantic predicates (Phase 2) ─────────────────────────────
    # include = AND (must touch all of these); exclude = AND-NOT (must touch
    # none).  Each value gets its own EXISTS / NOT EXISTS subquery so the
    # bind parameter list stays flat.  Values are uppercased here so user
    # input matches the storage convention.
    def _semantic_include(table: str, col: str, values: list[str] | None) -> None:
        if not values:
            return
        for v in values:
            clauses.append(
                f"EXISTS (SELECT 1 FROM features.{table} x "
                f"WHERE x.query_id = q.query_id AND x.{col} = ?)"
            )
            params.append(v.upper())

    def _semantic_exclude(table: str, col: str, values: list[str] | None) -> None:
        if not values:
            return
        for v in values:
            clauses.append(
                f"NOT EXISTS (SELECT 1 FROM features.{table} x "
                f"WHERE x.query_id = q.query_id AND x.{col} = ?)"
            )
            params.append(v.upper())

    _semantic_include(
        "query_referenced_tables", "table_ref", spec.referenced_tables_include,
    )
    _semantic_exclude(
        "query_referenced_tables", "table_ref", spec.referenced_tables_exclude,
    )
    _semantic_include(
        "query_where_columns", "column_ref", spec.where_columns_include,
    )
    _semantic_exclude(
        "query_where_columns", "column_ref", spec.where_columns_exclude,
    )

    return " AND ".join(clauses), params
