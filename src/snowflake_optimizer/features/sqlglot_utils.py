"""SQLGlot helpers — parameterized hashing + AST feature vectors.

Ported from query_watchdog/similarity.py and adapted for snowflake-optimizer.
"""
from __future__ import annotations

import hashlib
from collections import Counter

import sqlglot
from sqlglot import exp


def parameterized_hash(sql: str, dialect: str | None = "snowflake") -> str | None:
    """Return a 16-char hex hash of *sql* with every literal replaced by `?`.

    Returns None if the SQL can't be parsed.
    """
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return None
    for node in list(tree.find_all(exp.Literal)):
        node.replace(exp.Placeholder())
    canonical = tree.sql(dialect="duckdb")
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def extract_feature_vector(sql: str, dialect: str | None = "snowflake") -> dict[str, int]:
    """AST node-type frequency vector.  Returns {} on parse failure."""
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except Exception:
        return {}
    counts: dict[str, int] = Counter()
    for node in tree.walk():
        counts[type(node).__name__] += 1
    return dict(counts)
