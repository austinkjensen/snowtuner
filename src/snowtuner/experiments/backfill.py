"""Post-hoc metric backfill for experiments whose live metric-fetch failed.

The replay.py ``_fetch_query_metrics`` path can come back empty for a
handful of reasons:

  * SQL compilation error on the metric query itself (e.g. the historical
    bug where ``INFORMATION_SCHEMA.QUERY_HISTORY_BY_SESSION`` failed to
    resolve under the SNOWTUNER_ROLE session because the session had no
    current database).
  * QUERY_HISTORY lag exceeded the in-session poll window.
  * Transient driver / network errors.

When that happens we still have the ``replay_query_id`` (Snowflake-assigned)
on every successful run, and Snowflake's ``SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY``
view records the metrics persistently (with a ~45-minute lag).  Backfill
fetches those metrics post-hoc, UPDATEs the run rows, then re-runs
``aggregate()`` and writes the report.

This module is the recovery path.  The live path in replay.py is now fixed
so future experiments shouldn't need this — but it stays as a safety net
for any class of similar future failure.
"""
from __future__ import annotations

import logging

from snowtuner.experiments.engine import SnowflakeExecutorAdapter
from snowtuner.experiments.model import (
    ExperimentKind,
    ExperimentStatus,
    RunStatus,
)
from snowtuner.experiments.stats import aggregate
from snowtuner.experiments.store import ExperimentStore

logger = logging.getLogger(__name__)


def backfill_metrics(
    *,
    store: ExperimentStore,
    snowflake_client,           # SnowflakeClient
    experiment_id: int,
) -> dict[str, int]:
    """Backfill missing metrics on a COMPLETED experiment + re-aggregate.

    Returns a small summary dict suitable for an HTTP response::

        {
          "rows_inspected": 30,
          "rows_updated": 30,
          "rows_unreachable": 0,
          "report_regenerated": True,
        }

    Refuses to run if the experiment isn't COMPLETED (running / accepted
    experiments are still moving; a successful future run will populate
    metrics live).
    """
    exp = store.get(experiment_id)
    if exp is None:
        raise ValueError(f"experiment {experiment_id} not found")
    if exp.status != ExperimentStatus.COMPLETED:
        raise ValueError(
            f"experiment {experiment_id} is {exp.status.value}; "
            f"backfill is only safe on COMPLETED experiments"
        )

    runs = store.runs_for(experiment_id)
    # Only SUCCESS runs with a replay_query_id but missing elapsed_ms are
    # candidates.  FAILED runs intentionally have no metrics; runs that
    # already have elapsed_ms are good.
    targets = [
        r for r in runs
        if r.status == RunStatus.SUCCESS
        and r.replay_query_id is not None
        and r.elapsed_ms is None
    ]
    if not targets:
        return {
            "rows_inspected": len(runs),
            "rows_updated": 0,
            "rows_unreachable": 0,
            "report_regenerated": False,
            "note": "nothing to backfill",
        }

    executor = SnowflakeExecutorAdapter(snowflake_client)
    query_ids = [r.replay_query_id for r in targets]

    # One bulk lookup is preferable to N round-trips.  ACCOUNT_USAGE.QUERY_HISTORY
    # is a shared view with the IDs we need, and it's account-scoped so no
    # current-database issue (unlike INFORMATION_SCHEMA.*).
    placeholders = ", ".join(["%s"] * len(query_ids))
    sql = f"""
        SELECT
            query_id,
            total_elapsed_time,
            queued_overload_time,
            bytes_scanned,
            bytes_spilled_to_local_storage,
            bytes_spilled_to_remote_storage
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE query_id IN ({placeholders})
    """
    rows = executor.execute(sql, query_ids)
    by_qid = {str(r[0]): r for r in rows}
    logger.info(
        "backfill: found %d/%d query_ids in ACCOUNT_USAGE.QUERY_HISTORY",
        len(by_qid), len(query_ids),
    )

    updated = 0
    unreachable = 0
    for run in targets:
        metric_row = by_qid.get(run.replay_query_id or "")
        if metric_row is None:
            unreachable += 1
            continue
        _, elapsed, queued, scanned, spill_local, spill_remote = metric_row
        store.update_run_metrics(
            experiment_id=experiment_id,
            arm_name=run.arm_name,
            rep_index=run.rep_index,
            sampled_query_id=run.sampled_query_id,
            elapsed_ms=int(elapsed) if elapsed is not None else None,
            queued_overload_ms=int(queued) if queued is not None else None,
            bytes_scanned=int(scanned) if scanned is not None else None,
            bytes_spilled_local=int(spill_local) if spill_local is not None else None,
            bytes_spilled_remote=int(spill_remote) if spill_remote is not None else None,
        )
        updated += 1

    # Re-aggregate from the now-populated run rows and persist the new report.
    fresh_runs = store.runs_for(experiment_id)
    control_arm_name = exp.proposed.control_arm_name
    if exp.proposed.kind == ExperimentKind.TUNING and control_arm_name is None:
        control_arm_name = next(
            (a.name for a in exp.proposed.arms if a.is_control), None,
        )
    non_control = [
        a.name for a in exp.proposed.arms if a.name != control_arm_name
    ]
    report = aggregate(
        experiment_id=experiment_id,
        runs=fresh_runs,
        control_arm_name=control_arm_name,
        non_control_arms=non_control,
        kind=exp.proposed.kind,
    )
    store.set_report(experiment_id, report)
    logger.info(
        "backfill: experiment %d re-aggregated; best_arm=%s",
        experiment_id, report.best_arm_name,
    )

    return {
        "rows_inspected": len(runs),
        "rows_updated": updated,
        "rows_unreachable": unreachable,
        "report_regenerated": True,
    }
