"""Query replay primitives — execute one sampled query against one arm
and capture the metrics QUERY_HISTORY exposes.

Design notes
------------
- Result cache is disabled at session level (``USE_CACHED_RESULT = FALSE``)
  so two arms running the same query don't get a free cached answer.
- Warehouse local disk cache is **not** flushed between reps — flushing it
  requires suspending+resuming, which adds 30-60s per query.  The cost is
  symmetric across arms (same query order in alternation), so paired
  comparison still works; absolute latency numbers will run slightly faster
  than a cold-cache production query would.
- Metrics are fetched from QUERY_HISTORY via a short retry loop because
  QUERY_HISTORY can lag the query completion by a couple seconds.
- The table function is qualified as ``SNOWFLAKE.INFORMATION_SCHEMA.*``
  rather than bare ``INFORMATION_SCHEMA.*`` because ``INFORMATION_SCHEMA``
  is database-scoped: an unqualified call fails with "Invalid identifier"
  when the session has no current database set, which is exactly the case
  for the SNOWTUNER_ROLE session the engine runs under.  Qualifying to
  ``SNOWFLAKE.INFORMATION_SCHEMA`` makes the call work regardless of the
  session's USE DATABASE state — and SNOWFLAKE is guaranteed to exist on
  every Snowflake account.

This module is intentionally side-effect-only on Snowflake; it doesn't touch
DuckDB.  The engine writes the resulting ``ExperimentRun`` row.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol

from snowtuner.experiments.model import ExperimentRun, RunStatus
from snowtuner.storage.db import naive_utcnow

logger = logging.getLogger(__name__)


class SnowflakeExecutor(Protocol):
    """Minimal contract: execute SQL, capture last query_id."""

    def execute(
        self, sql: str, params: list | None = None,
    ) -> list[tuple]: ...

    def last_query_id(self) -> str | None: ...


# How long to wait for QUERY_HISTORY to catch up before giving up.
_METRICS_POLL_MAX_TRIES = 6
_METRICS_POLL_INTERVAL_S = 2.0


def prepare_session(executor: SnowflakeExecutor, warehouse_name: str) -> None:
    """One-time session setup for an arm: pin the warehouse, disable result cache."""
    executor.execute(f"USE WAREHOUSE {warehouse_name}")
    executor.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
    # Defensive: a long-running query shouldn't block all reps.
    executor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 600")


def replay_one(
    *,
    executor: SnowflakeExecutor,
    experiment_id: int,
    arm_name: str,
    rep_index: int,
    sampled_query_id: str,
    parameterized_hash: str | None,
    representative_sql: str,
    metrics_executor: SnowflakeExecutor | None = None,
) -> ExperimentRun:
    """Run a single replay and return the populated ExperimentRun.

    ``metrics_executor`` (optional) is a separate executor used to query
    QUERY_HISTORY.  The arm's own executor is busy with the replay session
    and may also lack ACCOUNT_USAGE grants; the engine wires a metrics
    executor on the monitoring role.  If omitted, falls back to the arm's
    executor (works for SNOWTUNER_EXP_SVC granted IMPORTED PRIVILEGES on
    SNOWFLAKE).
    """
    started_at = naive_utcnow()
    replay_query_id: str | None = None
    error_message: str | None = None
    status = RunStatus.SUCCESS

    try:
        executor.execute(representative_sql)
        replay_query_id = executor.last_query_id()
    except Exception as e:
        status = RunStatus.FAILED
        error_message = f"{type(e).__name__}: {e}"

    completed_at = naive_utcnow()

    metrics: dict = {}
    if status == RunStatus.SUCCESS and replay_query_id is not None:
        metrics = _fetch_query_metrics(
            metrics_executor or executor, replay_query_id,
        )
        # If the fetch failed, attach a synthetic error_message so the run
        # row carries the diagnostic forward — aggregate() will still mark
        # the run as excluded (no elapsed_ms) but at least the UI / runs
        # endpoint surfaces *why*.
        fetch_err = metrics.pop("_fetch_error", None)
        if fetch_err and error_message is None:
            error_message = f"metric-fetch failed: {fetch_err}"

    return ExperimentRun(
        experiment_id=experiment_id,
        arm_name=arm_name,
        rep_index=rep_index,
        sampled_query_id=sampled_query_id,
        parameterized_hash=parameterized_hash,
        replay_query_id=replay_query_id,
        elapsed_ms=metrics.get("elapsed_ms"),
        queued_overload_ms=metrics.get("queued_overload_ms"),
        bytes_scanned=metrics.get("bytes_scanned"),
        bytes_spilled_local=metrics.get("bytes_spilled_local"),
        bytes_spilled_remote=metrics.get("bytes_spilled_remote"),
        credits_used_estimate=metrics.get("credits_used_estimate"),
        status=status,
        error_message=error_message,
        started_at=started_at,
        completed_at=completed_at,
    )


def _fetch_query_metrics(
    executor: SnowflakeExecutor, query_id: str,
) -> dict:
    """Poll QUERY_HISTORY for the just-run query's metrics.

    Uses ``SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(...)`` (the
    fully-qualified call — see module docstring for why we don't use the
    bare ``INFORMATION_SCHEMA`` form).  Sub-minute latency for the current
    session; for cross-session backfill use ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``
    instead (~45-minute lag but persistent).
    """
    last_err: Exception | None = None
    for _attempt in range(_METRICS_POLL_MAX_TRIES):
        try:
            # NOTE: ``INFORMATION_SCHEMA.QUERY_HISTORY*`` table functions
            # do NOT expose ``BYTES_SPILLED_TO_LOCAL/REMOTE_STORAGE`` —
            # those columns are only on ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``
            # (the persistent shared view with ~45-minute lag).  We leave
            # spill stats as None in the live path; the backfill helper
            # (``experiments/backfill.py``) recovers them from ACCOUNT_USAGE
            # after the fact, which is sufficient for reporting.
            rows = executor.execute(
                f"""
                SELECT
                    total_elapsed_time,
                    queued_overload_time,
                    bytes_scanned,
                    credits_used_cloud_services
                FROM TABLE(SNOWFLAKE.INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(
                    RESULT_LIMIT => 1000
                ))
                WHERE query_id = '{query_id}'
                LIMIT 1
                """
            )
            if rows:
                r = rows[0]
                return {
                    "elapsed_ms": int(r[0]) if r[0] is not None else None,
                    "queued_overload_ms": int(r[1]) if r[1] is not None else None,
                    "bytes_scanned": int(r[2]) if r[2] is not None else None,
                    # Spill stats unavailable in INFORMATION_SCHEMA — backfill
                    # fills these in if the user runs it post-completion.
                    "bytes_spilled_local": None,
                    "bytes_spilled_remote": None,
                    # credits_used_cloud_services covers cloud-services overhead, not
                    # warehouse credits — the engine reconciles those from metering
                    # history at finalize time.  Leaving as None here.
                    "credits_used_estimate": None,
                }
        except Exception as e:
            last_err = e
            logger.warning(
                "metric fetch failed for query_id=%s (attempt %d/%d): %s",
                query_id, _attempt + 1, _METRICS_POLL_MAX_TRIES, e,
            )
        time.sleep(_METRICS_POLL_INTERVAL_S)
    # Could not retrieve — return empty metrics (run still counts as success,
    # but the stats step will mark it under-instrumented).
    if last_err:
        # Surface the last error to the caller; ``replay_one`` copies it
        # onto the ExperimentRun.error_message field so the diagnostic is
        # visible in /experiments/{id}/runs.
        return {"_fetch_error": f"{type(last_err).__name__}: {last_err}"}
    # No exception but no rows either — usually means QUERY_HISTORY hasn't
    # caught up.  Still surface that.
    return {"_fetch_error": "no row in QUERY_HISTORY_BY_SESSION after poll window"}
