"""Workload resolution for experiments (Phase 3).

A unified entry point that turns one of two user choices ::

  * "auto-sample from warehouse W"   (the historical default)
  * "use saved query group G"        (Phase 3 addition)

into a concrete list of ``SampledQuery`` objects ready for replay.

Resolution happens **at propose time** (not run time): the resulting query
IDs are frozen onto the ``ProposedExperiment`` so the user can preview the
workload before accepting, the cost estimator can budget against the
*actual* queries, and even dynamic groups become reproducible (snapshot
semantics).  See ``ProposedExperiment.sampled_query_ids`` /
``sample_warnings`` / ``workload_source``.

Safety filters mirror what ``StratifiedByFamily`` applies inside the
warehouse path — no ``CURRENT_TIMESTAMP``, no ``INFORMATION_SCHEMA``, only
``SELECT`` queries that succeeded.  Group members that fail those filters
are silently excluded from the resolved list; the count appears in the
``sample_warnings`` so the user can tell why their 50-query group only
produced 38 sampled queries.
"""
from __future__ import annotations

from dataclasses import dataclass

import duckdb

from snowtuner.experiments.cost_estimate import QueryStats
from snowtuner.experiments.sampling import (
    SampledQuery,
    StratifiedByFamily,
    _has_unsafe_text,
)
from snowtuner.query_groups.model import QueryGroup, QueryGroupKind
from snowtuner.query_groups.sql import build_filter_from_spec


@dataclass(frozen=True)
class ResolvedWorkload:
    """Output of ``resolve_workload`` — the frozen workload for an experiment."""
    sampled: list[SampledQuery]
    source: str             # 'auto' | f'group:{id}'
    warnings: list[str]


def resolve_workload(
    conn: duckdb.DuckDBPyConnection,
    *,
    workload_warehouse: str | None,
    query_group: QueryGroup | None,
    sample_size: int,
) -> ResolvedWorkload:
    """Resolve the workload for a proposed experiment.

    Exactly one of ``workload_warehouse`` / ``query_group`` must be supplied.
    When ``query_group`` is set, it takes precedence; ``workload_warehouse``
    is only used as the "where do queries come from" for the auto-sample
    fallback (the historical behavior).
    """
    if query_group is not None:
        return _resolve_from_group(conn, query_group, sample_size)
    if workload_warehouse:
        return _resolve_from_warehouse(conn, workload_warehouse, sample_size)
    raise ValueError(
        "resolve_workload needs either a query_group or a workload_warehouse"
    )


# ── auto-sample path ───────────────────────────────────────────────────


def _resolve_from_warehouse(
    conn: duckdb.DuckDBPyConnection,
    warehouse_name: str,
    sample_size: int,
) -> ResolvedWorkload:
    """Run the legacy ``StratifiedByFamily`` sampler against the warehouse
    and trim to ``sample_size``.  Surface a warning if fewer than
    ``sample_size`` eligible queries came back.
    """
    sampler = StratifiedByFamily()
    candidates = sampler.select(conn, warehouse_name)
    sampled = candidates[:sample_size]
    warnings: list[str] = []
    if len(sampled) < sample_size:
        warnings.append(
            f"requested {sample_size} queries but only {len(sampled)} "
            f"eligible queries available on warehouse {warehouse_name!r}; "
            f"running with {len(sampled)}"
        )
    return ResolvedWorkload(
        sampled=sampled,
        source="auto",
        warnings=warnings,
    )


# ── group path ─────────────────────────────────────────────────────────


def _resolve_from_group(
    conn: duckdb.DuckDBPyConnection,
    group: QueryGroup,
    sample_size: int,
) -> ResolvedWorkload:
    """Resolve a query group's members into the frozen sample list.

    For static groups: load by ``snapshot_query_ids``.
    For dynamic groups: re-evaluate ``filter_spec`` against ``raw.query_history``
    *as of propose time* — once resolved, the experiment's workload is fixed,
    regardless of how the group's underlying filter evolves later.

    Safety filters (no ``CURRENT_TIMESTAMP``, ``SELECT``-only, etc.) are
    applied in a second pass on the loaded rows; counts of excluded queries
    are surfaced as warnings.

    Sampling within the group: queries are ranked by ``total_elapsed_ms DESC``
    (highest-impact first) and trimmed to ``sample_size``.  This matches the
    "biggest cost contributors" rationale the auto-sampler uses; it also
    means the user's "save 100 slow queries as a group" workflow runs the
    most-impactful 30 (the default) at experiment time.
    """
    if group.kind == QueryGroupKind.STATIC:
        ids = group.snapshot_query_ids or []
        if not ids:
            return ResolvedWorkload(
                sampled=[],
                source=f"group:{group.id}",
                warnings=[f"static group {group.name!r} has no members"],
            )
        # Pull the rows for the snapshot IDs.  Filter for replay safety in
        # SQL (matches the warehouse sampler's SQL-level filter).
        placeholders = ", ".join(["?"] * len(ids))
        rows = conn.execute(
            f"""
            SELECT
                query_id, query_text, query_parameterized_hash,
                total_elapsed_ms, bytes_scanned
            FROM raw.query_history
            WHERE query_id IN ({placeholders})
              AND query_type = 'SELECT'
              AND execution_status = 'SUCCESS'
              AND query_parameterized_hash IS NOT NULL
              AND lower(query_text) NOT LIKE '%current_timestamp%'
              AND lower(query_text) NOT LIKE '%current_date%'
              AND lower(query_text) NOT LIKE '%now()%'
              AND lower(query_text) NOT LIKE '%information_schema%'
            ORDER BY total_elapsed_ms DESC NULLS LAST
            """,
            ids,
        ).fetchall()
    else:
        # Dynamic group: re-evaluate filter_spec.  The query joins the same
        # features table the /queries endpoint does so structural filters
        # work, and the safety filter is applied alongside.
        where_body, params = build_filter_from_spec(group.filter_spec)
        if where_body:
            where_sql = f"AND ({where_body})"
        else:
            where_sql = ""
        rows = conn.execute(
            f"""
            SELECT
                q.query_id, q.query_text, q.query_parameterized_hash,
                q.total_elapsed_ms, q.bytes_scanned
            FROM raw.query_history q
            LEFT JOIN features.query_sql_features f USING (query_id)
            WHERE q.query_type = 'SELECT'
              AND q.execution_status = 'SUCCESS'
              AND q.query_parameterized_hash IS NOT NULL
              AND lower(q.query_text) NOT LIKE '%current_timestamp%'
              AND lower(q.query_text) NOT LIKE '%current_date%'
              AND lower(q.query_text) NOT LIKE '%now()%'
              AND lower(q.query_text) NOT LIKE '%information_schema%'
              {where_sql}
            ORDER BY q.total_elapsed_ms DESC NULLS LAST
            """,
            params,
        ).fetchall()

    # Defense-in-depth: also apply the Python-side unsafe-text check.  This
    # catches stuff the SQL-level LIKE filters might miss (e.g. mixed-case
    # variants we missed).  Same conservative attitude as the warehouse
    # sampler.
    safe_rows = [r for r in rows if not _has_unsafe_text(r[1])]
    excluded = len(rows) - len(safe_rows)

    sampled = [
        SampledQuery(
            query_id=qid,
            parameterized_hash=phash,
            representative_sql=qtext,
            historical=QueryStats(
                query_id=qid,
                p50_elapsed_ms=float(elapsed or 0),
                mean_elapsed_ms=float(elapsed or 0),
                bytes_scanned=int(bytes_scanned) if bytes_scanned is not None else None,
            ),
        )
        for (qid, qtext, phash, elapsed, bytes_scanned) in safe_rows[:sample_size]
    ]

    warnings: list[str] = []
    if excluded:
        warnings.append(
            f"{excluded} query/queries excluded from group {group.name!r} "
            f"for replay safety (CURRENT_TIMESTAMP / INFORMATION_SCHEMA / etc.)"
        )
    if len(sampled) < sample_size:
        warnings.append(
            f"requested {sample_size} queries but group {group.name!r} "
            f"only has {len(sampled)} eligible members; running with {len(sampled)}"
        )

    return ResolvedWorkload(
        sampled=sampled,
        source=f"group:{group.id}",
        warnings=warnings,
    )
