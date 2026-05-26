"""Extract structural + semantic attributes from each query's SQL via sqlglot.

For every query in ``raw.query_history`` (with non-redacted text), parse the
SQL once and record:

  * scalar counts in ``features.query_sql_features``:
      - ``joins_count``             — literal JOIN keyword count
      - ``tables_referenced_count`` — distinct table identifiers, excluding CTE names
      - ``ctes_count``              — number of WITH CTE definitions
      - ``subqueries_count``        — number of correlated/uncorrelated subqueries
      - ``where_block_count``       — number of WHERE clauses (incl. inside subqueries)
      - ``where_predicate_count``   — total leaf predicates across all WHERE blocks
  * list-valued semantic predicates:
      - ``features.query_referenced_tables`` — one row per (query_id, table_ref).
        For schema-qualified references (``FROM business.sales_outcome``) we
        emit BOTH ``BUSINESS.SALES_OUTCOME`` AND ``SALES_OUTCOME`` so a filter
        on either form matches.  Unqualified references emit just the table
        name.  CTE names are excluded.
      - ``features.query_where_columns`` — one row per (query_id, column_ref).
        Broad scope: every ``exp.Column`` node anywhere in any WHERE subtree,
        which includes columns wrapped in function calls (``DATE_TRUNC(...,
        close_timestamp)``) and columns referenced inside subqueries.

All names are stored UPPERCASE (Snowflake's default identifier folding).

The transform is **incremental**: queries that already have a row in
``features.query_sql_features`` are skipped on subsequent runs, including
queries that previously failed parsing (their parse_error is recorded so we
don't retry them every run).  To force a re-extraction, ``DELETE`` the rows
from ``features.query_sql_features`` (the two side tables get rebuilt
together with the scalar row).

Redacted queries (NULL or empty query_text) get a row with all counts NULL
and ``parse_error = 'redacted'``.  This marks them processed so the
incremental check doesn't re-scan them every run.
"""
from __future__ import annotations

import logging
from typing import Any

import duckdb
import sqlglot
from sqlglot import exp

from snowtuner.features.base import FeatureTransform

logger = logging.getLogger(__name__)


# Parsing every query in a 100K+ row history can take a few minutes the first
# time.  The transform processes everything per run; subsequent runs only do
# the delta (new queries since the last sync).
class QuerySqlFeaturesTransform(FeatureTransform):
    name = "query_sql_features"
    inputs = {"raw.query_history"}
    outputs = {
        "features.query_sql_features",
        "features.query_referenced_tables",
        "features.query_where_columns",
    }

    def run(self, conn: duckdb.DuckDBPyConnection) -> None:
        rows = conn.execute(
            """
            SELECT q.query_id, q.query_text
            FROM raw.query_history q
            LEFT JOIN features.query_sql_features f USING (query_id)
            WHERE f.query_id IS NULL
            """
        ).fetchall()
        if not rows:
            return

        total = len(rows)
        logged_progress_at = 0
        for i, (query_id, query_text) in enumerate(rows):
            features, table_refs, where_cols = _extract_features(query_text)
            conn.execute(
                """
                INSERT INTO features.query_sql_features
                  (query_id, joins_count, tables_referenced_count, ctes_count,
                   subqueries_count, where_block_count, where_predicate_count,
                   parse_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    query_id,
                    features["joins_count"],
                    features["tables_referenced_count"],
                    features["ctes_count"],
                    features["subqueries_count"],
                    features["where_block_count"],
                    features["where_predicate_count"],
                    features["parse_error"],
                ],
            )
            for table_ref in table_refs:
                conn.execute(
                    "INSERT OR IGNORE INTO features.query_referenced_tables "
                    "(query_id, table_ref) VALUES (?, ?)",
                    [query_id, table_ref],
                )
            for column_ref in where_cols:
                conn.execute(
                    "INSERT OR IGNORE INTO features.query_where_columns "
                    "(query_id, column_ref) VALUES (?, ?)",
                    [query_id, column_ref],
                )
            # Log progress every 1000 rows for big backfills so the user
            # can tell the run isn't hung.
            if total >= 1000 and (i + 1) - logged_progress_at >= 1000:
                logger.info(
                    "query_sql_features: parsed %d / %d queries", i + 1, total,
                )
                logged_progress_at = i + 1


# ── extraction ───────────────────────────────────────────────────────────


def _extract_features(
    query_text: str | None,
) -> tuple[dict[str, Any], set[str], set[str]]:
    """Parse one query and return (scalar features, table_refs, where_columns).

    On parse failure or redacted text, returns the empty-features sentinel
    plus empty sets.
    """
    empty = {
        "joins_count": None,
        "tables_referenced_count": None,
        "ctes_count": None,
        "subqueries_count": None,
        "where_block_count": None,
        "where_predicate_count": None,
        "parse_error": None,
    }
    if not query_text or not query_text.strip():
        return {**empty, "parse_error": "redacted"}, set(), set()

    try:
        tree = sqlglot.parse_one(query_text, dialect="snowflake")
    except Exception as e:
        # Truncate to keep the column from blowing up on giant error messages.
        return {**empty, "parse_error": f"{type(e).__name__}: {str(e)[:200]}"}, set(), set()

    if tree is None:
        return {**empty, "parse_error": "empty AST"}, set(), set()

    # CTE names are referenced as tables in the main query body — collect them
    # first so we can exclude them from the "real" table count and from the
    # query_referenced_tables side table.
    cte_names = {cte.alias_or_name for cte in tree.find_all(exp.CTE)}
    table_names = {t.name for t in tree.find_all(exp.Table)} - cte_names

    table_refs = _table_refs(tree, cte_names)
    where_columns = _where_columns(tree)

    features = {
        "joins_count": len(list(tree.find_all(exp.Join))),
        "tables_referenced_count": len(table_names),
        "ctes_count": len(cte_names),
        "subqueries_count": len(list(tree.find_all(exp.Subquery))),
        "where_block_count": len(list(tree.find_all(exp.Where))),
        "where_predicate_count": _count_predicates(tree),
        "parse_error": None,
    }
    return features, table_refs, where_columns


def _table_refs(tree, cte_names: set[str]) -> set[str]:
    """Extract the set of table refs a query reads from.

    For ``FROM business.sales_outcome`` we emit both ``BUSINESS.SALES_OUTCOME``
    and ``SALES_OUTCOME`` so the user can filter on either form (per the
    Phase 2 design discussion).  For unqualified references we emit just the
    bare table name.  CTE names are excluded — they're not real tables.
    """
    refs: set[str] = set()
    for t in tree.find_all(exp.Table):
        name = t.name
        if not name or name in cte_names:
            continue
        bare = name.upper()
        refs.add(bare)
        # sqlglot stores the schema as ``db`` (database is ``catalog``).
        # ``t.db`` returns the unquoted name string or empty string.
        schema = t.db
        if schema:
            refs.add(f"{schema.upper()}.{bare}")
    return refs


def _where_columns(tree) -> set[str]:
    """Extract every column name appearing anywhere in any WHERE subtree.

    Broad semantics, per Phase 2 design: walks the entire WHERE expression
    including columns inside function calls (``DATE_TRUNC('day',
    close_timestamp)``), CASE branches, and subqueries — anything that's
    syntactically an ``exp.Column`` node under a ``exp.Where``.
    """
    cols: set[str] = set()
    for where in tree.find_all(exp.Where):
        for col in where.find_all(exp.Column):
            if col.name:
                cols.add(col.name.upper())
    return cols


def _count_predicates(tree) -> int:
    """Count leaf predicates across every WHERE block in the query.

    A WHERE clause is a Boolean expression tree.  We walk it and count the
    non-And/Or nodes — each such node is one "predicate" (e.g. ``a = 1``,
    ``b IS NULL``, ``c IN (...)``, ``NOT EXISTS (...)``).  This gives a
    more useful filter knob than ``where_block_count`` alone, which only
    reflects how many ``WHERE`` keywords appear.
    """
    total = 0
    for where in tree.find_all(exp.Where):
        total += _count_leaves(where.this)
    return total


def _count_leaves(node) -> int:
    if node is None:
        return 0
    if isinstance(node, (exp.And, exp.Or)):
        return _count_leaves(node.left) + _count_leaves(node.right)
    return 1
