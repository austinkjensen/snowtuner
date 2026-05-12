"""Sampling strategies for picking representative queries to replay.

The default strategy (``StratifiedByFamily``) picks the top-N most cost-impactful
query families and a recent representative query from each.  Filters guard
against unsafe-to-replay queries (writes, side-effecting calls, time-dependent
results, schema-snapshot reads).

Sampling is one of the trust-load-bearing parts of experiments — bad samples
produce bad reports.  Errs on the conservative side: skip a query rather than
include one we're not sure is safe.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import duckdb

from snowtuner.experiments.cost_estimate import QueryStats


@dataclass(frozen=True)
class SampledQuery:
    """One query selected for replay."""
    query_id: str               # the historical query_id from QUERY_HISTORY
    parameterized_hash: str     # which family it came from
    representative_sql: str     # what we'll actually replay
    historical: QueryStats      # for cost estimation


class SamplingStrategy(ABC):
    @abstractmethod
    def select(
        self, conn: duckdb.DuckDBPyConnection, warehouse_name: str,
    ) -> list[SampledQuery]:
        ...


class StratifiedByFamily(SamplingStrategy):
    """Pick top-N families by (frequency × mean elapsed), then one representative
    query per family from the last ``lookback_days`` days.

    Filters (all enforced at SQL level):
      - query_type = 'SELECT'
      - execution_status = 'SUCCESS'
      - query_text not LIKE certain patterns (CURRENT_TIMESTAMP/NOW(),
        INFORMATION_SCHEMA reads) — coarse but sufficient for v0.2.0
      - parameterized_hash IS NOT NULL (skip un-fingerprintable queries)
    """

    def __init__(
        self,
        *,
        families_count: int = 6,
        queries_per_family: int = 5,
        lookback_days: int = 7,
    ):
        self.families_count = families_count
        self.queries_per_family = queries_per_family
        self.lookback_days = lookback_days

    @property
    def target_count(self) -> int:
        return self.families_count * self.queries_per_family

    def select(
        self, conn: duckdb.DuckDBPyConnection, warehouse_name: str,
    ) -> list[SampledQuery]:
        # Step 1: rank families by (frequency × mean elapsed) descending.
        families = self._fetch_families(conn, warehouse_name)
        # Step 2: pick representative queries from each family.
        out: list[SampledQuery] = []
        for phash, freq, mean_elapsed_ms, _impact in families:
            row_set = conn.execute(
                f"""
                SELECT
                    query_id,
                    query_text,
                    total_elapsed_ms,
                    bytes_scanned
                FROM raw.query_history
                WHERE warehouse_name = ?
                  AND query_parameterized_hash = ?
                  AND query_type = 'SELECT'
                  AND execution_status = 'SUCCESS'
                  AND start_time >= now() - INTERVAL {self.lookback_days} DAYS
                ORDER BY start_time DESC
                LIMIT {self.queries_per_family}
                """,
                [warehouse_name, phash],
            ).fetchall()
            for query_id, query_text, elapsed_ms, bytes_scanned in row_set:
                if _has_unsafe_text(query_text):
                    continue
                out.append(SampledQuery(
                    query_id=query_id,
                    parameterized_hash=phash,
                    representative_sql=query_text,
                    historical=QueryStats(
                        query_id=query_id,
                        p50_elapsed_ms=float(elapsed_ms or 0),
                        mean_elapsed_ms=float(mean_elapsed_ms or 0),
                        bytes_scanned=int(bytes_scanned) if bytes_scanned is not None else None,
                    ),
                ))
        return out

    # -- internals --

    def _fetch_families(
        self, conn: duckdb.DuckDBPyConnection, warehouse_name: str,
    ) -> list[tuple]:
        """Same shape as the inlined query above, but with the unsafe-text
        filter applied at SQL level via NOT EXISTS-style logic.  We can't call
        Python from DuckDB's filter, so the filter is here:"""
        return conn.execute(
            f"""
            WITH safe_queries AS (
                SELECT *
                FROM raw.query_history
                WHERE warehouse_name = ?
                  AND query_type = 'SELECT'
                  AND execution_status = 'SUCCESS'
                  AND query_parameterized_hash IS NOT NULL
                  AND start_time >= now() - INTERVAL {self.lookback_days} DAYS
                  AND lower(query_text) NOT LIKE '%current_timestamp%'
                  AND lower(query_text) NOT LIKE '%current_date%'
                  AND lower(query_text) NOT LIKE '%now()%'
                  AND lower(query_text) NOT LIKE '%information_schema%'
            )
            SELECT
                query_parameterized_hash,
                COUNT(*) AS freq,
                AVG(total_elapsed_ms) AS mean_elapsed_ms,
                COUNT(*) * AVG(total_elapsed_ms) AS impact
            FROM safe_queries
            GROUP BY query_parameterized_hash
            ORDER BY impact DESC
            LIMIT {self.families_count}
            """,
            [warehouse_name],
        ).fetchall()


# ── Unsafe-query-text checks (defense-in-depth alongside SQL-level filter) ──

_UNSAFE_TEXT_FRAGMENTS = (
    "current_timestamp",
    "current_date",
    "current_time",
    " now(",
    "information_schema",
    "sysdate",
    "localtimestamp",
)


def _has_unsafe_text(query_text: str) -> bool:
    """Conservative substring check.  Errs on excluding queries that mention
    these patterns even in a comment — the cost of a false positive is "we
    don't replay this query," which is fine.
    """
    if not query_text:
        return True
    lowered = query_text.lower()
    return any(frag in lowered for frag in _UNSAFE_TEXT_FRAGMENTS)
