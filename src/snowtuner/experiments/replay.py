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

This module is intentionally side-effect-only on Snowflake; it doesn't touch
DuckDB.  The engine writes the resulting ``ExperimentRun`` row.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Protocol

from snowtuner.experiments.model import ExperimentRun, RunStatus


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
    started_at = _utc_naive_now()
    replay_query_id: str | None = None
    error_message: str | None = None
    status = RunStatus.SUCCESS

    try:
        executor.execute(representative_sql)
        replay_query_id = executor.last_query_id()
    except Exception as e:
        status = RunStatus.FAILED
        error_message = f"{type(e).__name__}: {e}"

    completed_at = _utc_naive_now()

    metrics: dict = {}
    if status == RunStatus.SUCCESS and replay_query_id is not None:
        metrics = _fetch_query_metrics(
            metrics_executor or executor, replay_query_id,
        )

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

    QUERY_HISTORY is part of the SNOWFLAKE.ACCOUNT_USAGE schema for historical
    queries (45 min lag!) and ``INFORMATION_SCHEMA.QUERY_HISTORY`` for the
    last 7 days with sub-minute latency.  We use the INFORMATION_SCHEMA
    function form which exposes the just-completed query.
    """
    last_err: Exception | None = None
    for _attempt in range(_METRICS_POLL_MAX_TRIES):
        try:
            rows = executor.execute(
                f"""
                SELECT
                    total_elapsed_time,
                    queued_overload_time,
                    bytes_scanned,
                    bytes_spilled_to_local_storage,
                    bytes_spilled_to_remote_storage,
                    credits_used_cloud_services
                FROM TABLE(INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION(
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
                    "bytes_spilled_local": int(r[3]) if r[3] is not None else None,
                    "bytes_spilled_remote": int(r[4]) if r[4] is not None else None,
                    # credits_used_cloud_services covers cloud-services overhead, not
                    # warehouse credits — the engine reconciles those from metering
                    # history at finalize time.  Leaving as None here.
                    "credits_used_estimate": None,
                }
        except Exception as e:
            last_err = e
        time.sleep(_METRICS_POLL_INTERVAL_S)
    # Could not retrieve — return empty metrics (run still counts as success,
    # but the stats step will mark it under-instrumented).
    if last_err:
        # Surface the last error in the run row via a synthetic note; the
        # engine will treat empty metrics as "excluded from aggregation."
        return {"_fetch_error": f"{type(last_err).__name__}: {last_err}"}
    return {}


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
