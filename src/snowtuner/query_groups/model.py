"""Query group domain models."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class QueryGroupKind(str, Enum):
    """Whether the group's members are frozen or re-evaluated on every read."""
    STATIC = "static"
    DYNAMIC = "dynamic"


class QueryFilterSpec(BaseModel):
    """The criteria that define a saved group of queries.

    The fields mirror what the queries explorer exposes as URL filters,
    so the round-trip ``URL params → QueryFilterSpec → URL params`` is
    lossless and "save current filter as group" is mechanical.

    Multi-value categorical filters use list semantics (``IN`` clause); each
    single-value or numeric field is a scalar predicate.  ``None`` everywhere
    means "no filter on this dimension."
    """
    # Categorical IN-filters
    warehouse_name: list[str] | None = None
    user_name: list[str] | None = None
    role_name: list[str] | None = None
    query_type: list[str] | None = None
    execution_status: list[str] | None = None
    query_parameterized_hash: list[str] | None = None

    # Time range
    start_time_from: datetime | None = None
    start_time_to: datetime | None = None

    # Numeric ranges
    min_elapsed_ms: int | None = None
    max_elapsed_ms: int | None = None

    # Performance toggles
    has_remote_spill: bool | None = None
    has_local_spill: bool | None = None
    has_queueing: bool | None = None

    # Free-text substring search over query_text
    search: str | None = None

    # Structural attributes — extracted by sqlglot into
    # features.query_sql_features.  NULL when query_text was redacted /
    # unparseable; a filter on min_* excludes nulls (since "min >= 1" can't
    # be satisfied by NULL).
    min_joins: int | None = None
    max_joins: int | None = None
    min_tables: int | None = None
    max_tables: int | None = None
    min_ctes: int | None = None
    max_ctes: int | None = None
    min_subqueries: int | None = None
    max_subqueries: int | None = None
    min_where_blocks: int | None = None
    max_where_blocks: int | None = None
    min_where_predicates: int | None = None
    max_where_predicates: int | None = None

    # ── Semantic predicates (Phase 2) ───────────────────────────────
    # All values are uppercased on the storage side, so case-insensitive
    # filtering happens automatically when the API also uppercases what
    # the user typed.  ``include`` semantics = "query must touch ALL of
    # these"; ``exclude`` = "query must touch NONE of these".  This
    # matches how a user thinks about set-membership for multi-valued
    # attributes (a query has many tables/columns, not one).
    referenced_tables_include: list[str] | None = None
    referenced_tables_exclude: list[str] | None = None
    where_columns_include: list[str] | None = None
    where_columns_exclude: list[str] | None = None


class QueryGroup(BaseModel):
    """A persisted query group — the row shape returned by the API.

    For static groups, ``snapshot_query_ids`` is the frozen membership;
    ``snapshot_at`` records when that snapshot was taken.  For dynamic
    groups, both are None and members are computed on demand from
    ``filter_spec``.
    """
    id: int
    name: str
    description: str | None = None
    kind: QueryGroupKind
    filter_spec: QueryFilterSpec

    # Static only.
    snapshot_query_ids: list[str] | None = None
    snapshot_at: datetime | None = None

    created_at: datetime
    created_by: str = "user"

    # Convenience: how many queries are in this group right now.  Populated
    # by the API endpoint, not stored on the row.
    member_count: int | None = Field(default=None)
