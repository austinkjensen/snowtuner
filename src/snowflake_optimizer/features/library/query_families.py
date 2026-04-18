"""Assign each distinct parameterized SQL shape a stable family_id.

For v1 a "family" is simply one parameterized_hash.  Later we'll replace this
transform with one that does structural (cosine-similarity) clustering, but
the downstream interface (a table with family_id) stays the same.
"""
from __future__ import annotations

import duckdb

from snowflake_optimizer.features.base import FeatureTransform
from snowflake_optimizer.features.sqlglot_utils import parameterized_hash


class QueryFamiliesTransform(FeatureTransform):
    name = "query_families"
    inputs = {"raw.query_history"}
    outputs = {"features.query_families"}

    def run(self, conn: duckdb.DuckDBPyConnection) -> None:
        # 1) Find all parameterized_hashes that are not yet assigned a family.
        #    Prefer the hash Snowflake already provides; else compute locally
        #    via SQLGlot on the first query_text we see.
        rows = conn.execute(
            """
            SELECT parameterized_hash, query_text
            FROM (
                SELECT
                    COALESCE(query_parameterized_hash, query_hash) AS parameterized_hash,
                    FIRST(query_text) AS query_text,
                    ROW_NUMBER() OVER (
                        PARTITION BY COALESCE(query_parameterized_hash, query_hash)
                        ORDER BY start_time DESC
                    ) AS rn
                FROM raw.query_history
                WHERE COALESCE(query_parameterized_hash, query_hash) IS NOT NULL
                GROUP BY COALESCE(query_parameterized_hash, query_hash), query_text, start_time
            )
            WHERE rn = 1
            """
        ).fetchall()

        for phash, qtext in rows:
            if not phash:
                phash = parameterized_hash(qtext or "")
                if not phash:
                    continue
            conn.execute(
                """
                INSERT INTO features.query_families (parameterized_hash, family_id, representative_sql)
                VALUES (?, ?, ?)
                ON CONFLICT (parameterized_hash) DO UPDATE
                  SET family_id = excluded.family_id,
                      representative_sql = excluded.representative_sql,
                      updated_at = now()
                """,
                [phash, phash, qtext],
            )
