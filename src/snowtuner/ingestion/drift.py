"""Schema-drift detection for ingestion sources.

Each ``Source`` that mirrors a Snowflake system view declares the view it
pulls from (``source_view``) and the columns it expects to SELECT
(``expected_source_columns``).  At drift-check time we ask Snowflake what
columns the view ACTUALLY exposes today, diff the two sets, and surface
missing or extra columns.

Why this matters
----------------
Snowflake quietly adds columns to ACCOUNT_USAGE views as the product
evolves.  Today's QUERY_HISTORY has ~50 columns; six months from now it
may have 55.  Our sources mirror a stable subset, but two failure modes
can sneak in:

  * **Missing** — a column our source SELECTs no longer exists in the
    Snowflake view.  Sync will fail outright with a compile error.  We
    should catch this before the first user runs into it.
  * **Extra** — Snowflake added a column we don't mirror.  Sync succeeds
    but a recommender that could use the new column is leaving signal on
    the floor.

This module is **warn-only**: it reports drift, never auto-evolves the
schema.  The user decides whether to extend the source.  Auto-evolution
might land later behind a ``--auto-evolve`` flag.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from snowtuner.ingestion.base import Source, SnowflakeClient

logger = logging.getLogger(__name__)


@dataclass
class SourceDrift:
    """Drift report for one source."""
    source_name: str
    source_view: str
    expected_columns: list[str] = field(default_factory=list)
    actual_columns: list[str] = field(default_factory=list)
    missing_from_snowflake: list[str] = field(default_factory=list)
    extra_in_snowflake: list[str] = field(default_factory=list)
    error: str | None = None     # set when we couldn't query the view

    @property
    def has_drift(self) -> bool:
        """True if Snowflake's view doesn't match what the source expects."""
        return bool(self.missing_from_snowflake or self.extra_in_snowflake)

    @property
    def is_actionable(self) -> bool:
        """True if drift is severe enough that sync would fail.

        Missing columns break SELECT statements; extra columns are
        informational only.
        """
        return bool(self.missing_from_snowflake)


@dataclass
class DriftReport:
    """Aggregate drift across all sources."""
    sources: list[SourceDrift] = field(default_factory=list)

    @property
    def any_actionable(self) -> bool:
        return any(s.is_actionable for s in self.sources)

    @property
    def any_drift(self) -> bool:
        return any(s.has_drift for s in self.sources)


def check_drift(
    client: SnowflakeClient,
    sources: list[Source],
) -> DriftReport:
    """Query Snowflake for each source's view columns and diff against the
    source's expected list.

    Sources with ``source_view = None`` (e.g. ``SHOW WAREHOUSES``-backed
    sources) are skipped — they don't have a queryable INFORMATION_SCHEMA
    counterpart.
    """
    report = DriftReport()
    for source in sources:
        if not source.source_view:
            continue
        try:
            actual_cols = _fetch_view_columns(client, source.source_view)
        except Exception as e:
            logger.warning(
                "drift check failed for source %s (%s): %s",
                source.name, source.source_view, e,
            )
            report.sources.append(SourceDrift(
                source_name=source.name,
                source_view=source.source_view,
                expected_columns=list(source.expected_source_columns),
                error=f"{type(e).__name__}: {e}",
            ))
            continue

        expected_set = {c.lower() for c in source.expected_source_columns}
        actual_set = {c.lower() for c in actual_cols}
        missing = sorted(expected_set - actual_set)
        extra = sorted(actual_set - expected_set)

        if missing:
            logger.warning(
                "schema drift on %s: source expects %d columns that no "
                "longer exist in %s: %s",
                source.name, len(missing), source.source_view, missing,
            )
        if extra:
            logger.info(
                "schema drift on %s: Snowflake exposes %d columns the "
                "source doesn't mirror in %s: %s",
                source.name, len(extra), source.source_view, extra,
            )

        report.sources.append(SourceDrift(
            source_name=source.name,
            source_view=source.source_view,
            expected_columns=sorted(source.expected_source_columns),
            actual_columns=sorted(actual_cols),
            missing_from_snowflake=missing,
            extra_in_snowflake=extra,
        ))
    return report


def _fetch_view_columns(client: SnowflakeClient, view_fqn: str) -> list[str]:
    """Query INFORMATION_SCHEMA.COLUMNS for the given fully-qualified view.

    ``view_fqn`` is dotted: ``DB.SCHEMA.VIEW``.  We rely on the SNOWFLAKE
    database's INFORMATION_SCHEMA for ACCOUNT_USAGE views since
    ``SNOWFLAKE.ACCOUNT_USAGE.COLUMNS`` exists and is shared with every
    account.
    """
    parts = view_fqn.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"source_view must be 'DB.SCHEMA.VIEW' (got {view_fqn!r})"
        )
    db, schema, view = parts
    # ACCOUNT_USAGE.COLUMNS lives in the SNOWFLAKE database; every account
    # has access.  This works regardless of session current_database.
    rows = client.execute(
        f"""
        SELECT column_name
        FROM {db}.INFORMATION_SCHEMA.COLUMNS
        WHERE table_schema = '{schema}'
          AND table_name = '{view}'
        ORDER BY ordinal_position
        """
    )
    return [r[0] for r in rows]
