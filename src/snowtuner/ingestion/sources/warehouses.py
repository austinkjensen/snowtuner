"""Snapshot current warehouse config via ``SHOW WAREHOUSES``.

Full-refresh source (no watermark).

``SHOW WAREHOUSES`` column order varies by Snowflake edition and release, so we
map by column name (from ``cursor.description``), not by position.

Generation (Gen1 vs Gen2) isn't exposed by ``SHOW WAREHOUSES`` itself, so for
each warehouse we run ``SHOW PARAMETERS LIKE 'GENERATION' IN WAREHOUSE <name>``
as a follow-up.  It's a per-warehouse round-trip but warehouse counts are
typically in the tens, not thousands, so the overhead is negligible.  If the
parameter query fails (older Snowflake versions, no MONITOR privilege, etc.)
we leave generation NULL and downstream consumers degrade gracefully.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import duckdb

from snowtuner.ingestion.base import Source, SnowflakeClient

logger = logging.getLogger(__name__)


_COLUMNS = [
    "name", "size", "min_cluster_count", "max_cluster_count",
    "auto_suspend_seconds", "auto_resume", "scaling_policy", "state", "comment",
    "generation",
]


class WarehousesSource(Source):
    name = "warehouses"
    target_table = "raw.warehouses"
    watermark_column = None
    default_initial_lookback_days = None  # full refresh, no lookback concept

    def fetch(self, client: SnowflakeClient, since: datetime | None) -> list[dict[str, Any]]:
        cols, rows = client.execute_with_columns("SHOW WAREHOUSES")
        col_idx = {c: i for i, c in enumerate(cols)}

        def pick(row: tuple[Any, ...], *candidates: str) -> Any:
            for c in candidates:
                if c in col_idx:
                    return row[col_idx[c]]
            return None

        out: list[dict[str, Any]] = []
        for r in rows:
            auto_resume_raw = pick(r, "auto_resume")
            if isinstance(auto_resume_raw, bool) or auto_resume_raw is None:
                auto_resume = auto_resume_raw
            else:
                # Some Snowflake editions return "true"/"false" as strings.
                auto_resume = str(auto_resume_raw).lower() == "true"

            name = pick(r, "name")
            out.append({
                "name": name,
                "size": pick(r, "size"),
                "min_cluster_count": pick(r, "min_cluster_count"),
                "max_cluster_count": pick(r, "max_cluster_count"),
                "auto_suspend_seconds": pick(r, "auto_suspend"),
                "auto_resume": auto_resume,
                "scaling_policy": pick(r, "scaling_policy"),
                "state": pick(r, "state"),
                "comment": pick(r, "comment"),
                "generation": _fetch_generation(client, name) if name else None,
            })
        return out

    def upsert(self, conn: duckdb.DuckDBPyConnection, rows: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM raw.warehouses")
        if not rows:
            return
        col_list = ", ".join(_COLUMNS)
        placeholders = ", ".join(["?"] * len(_COLUMNS))
        for r in rows:
            conn.execute(
                f"INSERT INTO raw.warehouses ({col_list}) VALUES ({placeholders})",
                [r.get(c) for c in _COLUMNS],
            )


def _fetch_generation(client: SnowflakeClient, warehouse_name: str) -> str | None:
    """Return the GENERATION parameter for a warehouse, or None on failure.

    ``SHOW PARAMETERS LIKE 'GENERATION' IN WAREHOUSE <name>`` returns rows
    shaped ``(key, value, default, level, description, type)``.  We pull
    ``value``.  Snowflake's value is either ``'1'`` or ``'2'``; we preserve
    whatever Snowflake returns rather than normalizing.

    Failures (older Snowflake versions where the parameter doesn't exist,
    missing MONITOR privilege, transient connection issues) downgrade to
    None — the experiments recommender treats unknown generation as
    "skip, can't safely classify".
    """
    try:
        # Identifier must be embedded; Snowflake doesn't accept bound parameters
        # for object names.  We trust the warehouse_name came from SHOW WAREHOUSES
        # output, so injection isn't a concern, but quote-escape defensively.
        safe_name = warehouse_name.replace('"', '""')
        cols, rows = client.execute_with_columns(
            f'SHOW PARAMETERS LIKE \'GENERATION\' IN WAREHOUSE "{safe_name}"'
        )
        if not rows:
            return None
        col_idx = {c.lower(): i for i, c in enumerate(cols)}
        value_idx = col_idx.get("value")
        if value_idx is None:
            return None
        v = rows[0][value_idx]
        return str(v) if v is not None else None
    except Exception as e:
        logger.warning(
            "could not fetch generation for warehouse %r: %s",
            warehouse_name, e,
        )
        return None
