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


# Per-process memo: which warehouses have already had their access-control
# error logged.  The Gen2/QAS probes hit three parameters per warehouse,
# and the typical access-control failure mode is "the role lacks MONITOR
# on this warehouse" — same root cause for all three.  Without dedup,
# every sync logs 3× warning lines per ungranted warehouse, drowning real
# issues.  This set caps the noise at one informative line per warehouse
# per process lifetime.  Cleared by tests via _reset_access_error_memo().
_access_error_memo: set[str] = set()


def _reset_access_error_memo() -> None:
    """Test hook — clears the per-process access-error dedup memo."""
    _access_error_memo.clear()


_COLUMNS = [
    "name", "size", "min_cluster_count", "max_cluster_count",
    "auto_suspend_seconds", "auto_resume", "scaling_policy", "state", "comment",
    "generation", "qas_state", "qas_max_scale_factor",
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
            # Per-warehouse parameter lookups in one batch: 3 round-trips
            # per warehouse but acceptable for tens of warehouses.  Each is
            # independently defensive — one failure doesn't poison the row.
            gen = _fetch_parameter(client, name, "GENERATION") if name else None
            qas_raw = (
                _fetch_parameter(client, name, "ENABLE_QUERY_ACCELERATION")
                if name else None
            )
            qas_state = _normalize_qas_state(qas_raw)
            qas_max_raw = (
                _fetch_parameter(client, name, "QUERY_ACCELERATION_MAX_SCALE_FACTOR")
                if name else None
            )
            try:
                qas_max = int(qas_max_raw) if qas_max_raw is not None else None
            except (TypeError, ValueError):
                qas_max = None

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
                "generation": gen,
                "qas_state": qas_state,
                "qas_max_scale_factor": qas_max,
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


def _fetch_parameter(
    client: SnowflakeClient, warehouse_name: str, param_name: str,
) -> str | None:
    """Return one warehouse-level parameter's value, or None on failure.

    ``SHOW PARAMETERS LIKE '<param>' IN WAREHOUSE <name>`` returns rows
    shaped ``(key, value, default, level, description, type)``.  We pull
    ``value`` and return as a string.

    Failures (older Snowflake versions where the parameter doesn't exist,
    edition restrictions on QAS-related params, missing MONITOR privilege,
    transient connection issues) downgrade to None — downstream consumers
    treat unknown values as "can't safely classify, skip".
    """
    try:
        # Identifier must be embedded; Snowflake doesn't accept bound parameters
        # for object names.  We trust the warehouse_name came from SHOW WAREHOUSES
        # output, so injection isn't a concern, but quote-escape defensively.
        safe_name = warehouse_name.replace('"', '""')
        cols, rows = client.execute_with_columns(
            f'SHOW PARAMETERS LIKE \'{param_name}\' IN WAREHOUSE "{safe_name}"'
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
        # SQL access control error (Snowflake error code 42501) is by far
        # the most common reason this fails — it just means the role lacks
        # MONITOR on the warehouse.  Three probes per warehouse × N
        # ungranted warehouses would spam the log; consolidate to one
        # actionable line per warehouse per process.  Non-permission errors
        # (network, edition restriction, etc.) still log per-probe so we
        # can see anything weird.
        err_str = str(e)
        if "42501" in err_str or "access control" in err_str.lower():
            if warehouse_name not in _access_error_memo:
                _access_error_memo.add(warehouse_name)
                logger.warning(
                    "warehouse %r: no MONITOR privilege — Gen2/QAS detection "
                    "skipped.  To enable, run as ACCOUNTADMIN: "
                    "GRANT MONITOR ON WAREHOUSE %s TO ROLE <snowtuner-role>",
                    warehouse_name, warehouse_name,
                )
            return None
        logger.warning(
            "could not fetch parameter %s for warehouse %r: %s",
            param_name, warehouse_name, e,
        )
        return None


def _normalize_qas_state(raw: str | None) -> str | None:
    """Normalize Snowflake's QAS boolean to our 'on'/'off' enum convention.

    ``ENABLE_QUERY_ACCELERATION`` returns 'true'/'false' as strings (or
    sometimes a Python bool depending on driver version).  We map to the
    lowercase 'on'/'off' that the QASState enum and the experiments
    framework use.
    """
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("true", "on", "1", "yes", "enabled"):
        return "on"
    if s in ("false", "off", "0", "no", "disabled"):
        return "off"
    return None
