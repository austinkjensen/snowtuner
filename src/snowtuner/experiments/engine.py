"""ExperimentEngine — runs an ACCEPTED ProposedExperiment to completion.

Top-level responsibilities:

  1. Sample fresh queries from the local DuckDB feature store.
  2. Provision a side-by-side test warehouse per arm (control included),
     so the production control warehouse is never touched.
  3. Replay each sampled query against each arm in alternation, capturing
     metrics.
  4. Track cumulative cost; abort if the hard cap is hit.
  5. Reconcile per-arm credit usage from WAREHOUSE_METERING_HISTORY.
  6. Compute paired-test stats with Bonferroni correction.
  7. Tear down the test warehouses.
  8. Persist the final report.

Crash recovery: provisioned warehouse names are persisted *before* the
CREATE WAREHOUSE so a janitor can clean them up after a crash.  The engine
exposes ``recover_orphaned_warehouses()`` to be called at startup.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import duckdb

from snowtuner.experiments.config_delta import WarehouseConfig
from snowtuner.experiments.model import (
    ExperimentRun,
    ExperimentStatus,
    RunStatus,
)
from snowtuner.experiments.provisioning import (
    ProvisionedArm,
    render_create_warehouse_sql,
    render_drop_warehouse_sql,
    test_warehouse_name,
)
from snowtuner.experiments.replay import prepare_session, replay_one
from snowtuner.experiments.sampling import SampledQuery, SamplingStrategy, StratifiedByFamily
from snowtuner.experiments.stats import aggregate
from snowtuner.experiments.store import ExperimentStore
from snowtuner.recommenders.sizes import credit_rate

logger = logging.getLogger(__name__)


@dataclass
class EngineConfig:
    """Knobs that control engine behavior.  Sensible defaults; rarely overridden."""

    # Multiplier applied to ``cost_estimate.high_credits`` to set the hard cap.
    # 1.0 means "abort when we hit the high-end estimate."
    cost_cap_multiplier: float = 1.0

    # If the leading-indicator cost estimate exceeds the cap, abort.  Otherwise
    # keep running and reconcile against metering history at the end.
    enforce_hard_cap: bool = True

    # Per-arm session statement timeout (Snowflake STATEMENT_TIMEOUT_IN_SECONDS).
    per_query_timeout_seconds: int = 600


class SnowflakeExecutorAdapter:
    """Wraps a ``SnowflakeClient`` into the ``SnowflakeExecutor`` protocol used
    by replay.py — adds ``last_query_id()`` since the client doesn't expose
    cursor state directly.
    """

    def __init__(self, client) -> None:
        self._client = client
        self._last_query_id: str | None = None

    def execute(self, sql: str, params: list | None = None) -> list[tuple]:
        # Reuse the SnowflakeClient's connect+cursor path, but keep the cursor
        # alive long enough to capture sfqid.
        conn = self._client._connect()  # noqa: SLF001 — using the lazy-connect path
        cur = conn.cursor()
        try:
            cur.execute(sql, params or [])
            try:
                rows = cur.fetchall()
            except Exception:
                # Some DDL/SET statements return no result set; treat as empty.
                rows = []
            self._last_query_id = getattr(cur, "sfqid", None)
            return rows
        finally:
            cur.close()

    def last_query_id(self) -> str | None:
        return self._last_query_id


class ExperimentEngine:
    """Drives a single experiment from ACCEPTED to COMPLETED (or ABORTED).

    Not thread-safe; instantiate one per run.  The DuckDB conn is the local
    snowtuner database (for reading raw.warehouses, raw.query_history, and
    writing app.experiments / app.experiment_runs).  The SnowflakeClient
    talks to Snowflake on behalf of the experiment_user role.
    """

    def __init__(
        self,
        duck_conn: duckdb.DuckDBPyConnection,
        snowflake_client,                       # SnowflakeClient
        *,
        config: EngineConfig | None = None,
        sampling_strategy: SamplingStrategy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    ) -> None:
        self.duck = duck_conn
        self.sf = snowflake_client
        self.cfg = config or EngineConfig()
        self.sampler = sampling_strategy or StratifiedByFamily()
        self.store = ExperimentStore(duck_conn)
        self._clock = clock

    # ── public entry points ─────────────────────────────────────────

    def run(self, experiment_id: int) -> None:
        """Run an ACCEPTED experiment to completion (or abort).

        Idempotency: callers must ensure this is invoked once per experiment.
        The store's single-experiment-at-a-time guard prevents concurrent runs,
        but a re-invocation on a COMPLETED experiment is a programmer error.
        """
        exp = self.store.get(experiment_id)
        if exp is None:
            raise ValueError(f"experiment {experiment_id} not found")
        if exp.status != ExperimentStatus.ACCEPTED:
            raise ValueError(
                f"experiment {experiment_id} is in status {exp.status.value!r}, "
                f"only ACCEPTED experiments can be run"
            )

        provisioned: list[ProvisionedArm] = []
        try:
            # Step 1: look up control config and sample queries.
            control_config = self._load_control_config(exp.proposed.target_warehouse)
            sampled = self._sample_queries(exp.proposed.target_warehouse, exp.proposed.sample_size)
            if not sampled:
                self.store.set_status(
                    experiment_id, ExperimentStatus.FAILED,
                    aborted_reason="no queries available to sample from query_history",
                )
                return

            # Step 2: persist test warehouse names BEFORE creating them, so a
            # crash mid-CREATE doesn't strand them.
            provisioned_names = [
                test_warehouse_name(experiment_id, arm.name)
                for arm in exp.proposed.arms
            ]
            self.store.set_test_warehouses(experiment_id, provisioned_names)

            # Step 3: provision test warehouses.
            for arm in exp.proposed.arms:
                merged = control_config.merge(arm.delta)
                name = test_warehouse_name(experiment_id, arm.name)
                sql = render_create_warehouse_sql(name, merged)
                logger.info("creating test warehouse: %s", name)
                self.sf.execute(sql)
                provisioned.append(ProvisionedArm(arm=arm, warehouse_name=name, config=merged))

            # Step 4: mark RUNNING and execute the replay loop.
            self.store.set_status(experiment_id, ExperimentStatus.RUNNING)
            self._run_replay_loop(
                experiment_id=experiment_id,
                provisioned=provisioned,
                sampled=sampled,
                reps_per_arm=exp.proposed.reps_per_arm,
                cost_cap_credits=(
                    exp.proposed.cost_estimate.high_credits * self.cfg.cost_cap_multiplier
                ),
            )

            # Step 5: build report from runs.
            runs = self.store.runs_for(experiment_id)
            control_arm = next(a for a in exp.proposed.arms if a.is_control)
            non_control = [a.name for a in exp.proposed.arms if not a.is_control]
            # Allocate credits over runs proportional to elapsed.
            self._allocate_credits_to_runs(runs, provisioned)

            report = aggregate(
                experiment_id=experiment_id,
                runs=runs,
                control_arm_name=control_arm.name,
                non_control_arms=non_control,
            )
            self.store.set_report(experiment_id, report)
            self.store.set_status(experiment_id, ExperimentStatus.COMPLETED)
        except Exception as e:
            logger.exception("experiment %s failed", experiment_id)
            self.store.set_status(
                experiment_id, ExperimentStatus.FAILED,
                aborted_reason=f"{type(e).__name__}: {e}",
            )
            # don't re-raise; we still want to clean up below
        finally:
            self._teardown(experiment_id, provisioned)

    def recover_orphaned_warehouses(self) -> list[str]:
        """At engine startup: drop any test warehouses left behind by a prior crash.

        Returns the list of warehouse names that were dropped.
        """
        dropped: list[str] = []
        for exp in self.store.needing_cleanup():
            for name in exp.test_warehouse_names:
                try:
                    self.sf.execute(render_drop_warehouse_sql(name))
                    dropped.append(name)
                except Exception:
                    logger.exception("failed to drop orphaned warehouse %s", name)
            self.store.mark_test_warehouses_cleaned(exp.id, True)
        return dropped

    # ── internals ──────────────────────────────────────────────────

    def _load_control_config(self, warehouse_name: str) -> WarehouseConfig:
        """Load the control warehouse's current config from raw.warehouses.

        v0.2 only reads the fields experiments care about.  Generation / QAS
        columns may not exist in older synced rows — defaults handle that.
        """
        row = self.duck.execute(
            """
            SELECT name, size, auto_suspend_seconds, auto_resume
            FROM raw.warehouses
            WHERE upper(name) = upper(?)
            """,
            [warehouse_name],
        ).fetchone()
        if not row:
            raise RuntimeError(
                f"control warehouse {warehouse_name!r} not found in raw.warehouses; "
                f"run a sync before accepting experiments"
            )
        return WarehouseConfig(
            name=row[0],
            size=row[1],
            auto_suspend_seconds=row[2],
            auto_resume=bool(row[3]) if row[3] is not None else None,
            # Generation and QAS aren't currently in raw.warehouses; they
            # default to None and the merge() picks up the arm's delta.
            generation=None,
            qas_state=None,
        )

    def _sample_queries(
        self, target_warehouse: str, sample_size: int,
    ) -> list[SampledQuery]:
        """Pick representative queries to replay.  Trims to ``sample_size``."""
        samples = self.sampler.select(self.duck, target_warehouse)
        return samples[:sample_size]

    def _run_replay_loop(
        self,
        *,
        experiment_id: int,
        provisioned: list[ProvisionedArm],
        sampled: list[SampledQuery],
        reps_per_arm: int,
        cost_cap_credits: float,
    ) -> None:
        """Alternation-scheduled replay.

        Schedule: for each (query, rep), run all arms back-to-back.  Within
        each pass, arms are alternated so warmup effects are symmetric.

        Cost guard: after each (query, rep) pass, compute a leading-indicator
        cost estimate (sum of elapsed × credit_rate / 3600000) and abort if
        it exceeds the cap.
        """
        executor = SnowflakeExecutorAdapter(self.sf)

        # Pre-prepare each arm's session context.  Session state is per-
        # Snowflake-connection, and we use one connection for the whole
        # experiment, so we re-prepare USE WAREHOUSE on every arm switch but
        # only need to set USE_CACHED_RESULT once.
        executor.execute("ALTER SESSION SET USE_CACHED_RESULT = FALSE")
        executor.execute(
            f"ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = {self.cfg.per_query_timeout_seconds}"
        )

        cumulative_credits = 0.0

        for rep_index in range(reps_per_arm):
            for sample in sampled:
                for prov in provisioned:
                    # Switch warehouse for this arm.
                    executor.execute(f"USE WAREHOUSE {prov.warehouse_name}")

                    run = replay_one(
                        executor=executor,
                        experiment_id=experiment_id,
                        arm_name=prov.arm.name,
                        rep_index=rep_index,
                        sampled_query_id=sample.query_id,
                        parameterized_hash=sample.parameterized_hash,
                        representative_sql=sample.representative_sql,
                    )
                    self.store.record_run(run)

                    # Leading-indicator credit estimate for this run.
                    if run.elapsed_ms is not None and prov.config.size is not None:
                        per_run_credits = (
                            run.elapsed_ms / 3_600_000.0
                            * credit_rate(prov.config.size)
                        )
                        cumulative_credits += per_run_credits

                # Cost-cap check after each (query, rep) pass.
                self.store.set_actual_cost(experiment_id, cumulative_credits)
                if self.cfg.enforce_hard_cap and cumulative_credits >= cost_cap_credits:
                    self.store.set_actual_cost(
                        experiment_id, cumulative_credits, cost_cap_hit=True,
                    )
                    self.store.set_status(
                        experiment_id, ExperimentStatus.ABORTED,
                        aborted_reason=(
                            f"cost cap of {cost_cap_credits:.2f} credits hit "
                            f"at query {sample.query_id} rep {rep_index}; "
                            f"cumulative leading-indicator: {cumulative_credits:.2f}"
                        ),
                    )
                    return

    def _allocate_credits_to_runs(
        self,
        runs: list[ExperimentRun],
        provisioned: list[ProvisionedArm],
    ) -> None:
        """Distribute per-arm metered credits over each successful run
        proportionally to elapsed time.

        Source of truth: leading indicator (elapsed × credit_rate). v0.2 omits
        the WAREHOUSE_METERING_HISTORY reconciliation step because the metering
        view lags ~10 minutes and a cleanup-on-completion engine doesn't want
        to wait. The stats step's per-query credit deltas use these allocations.
        """
        wh_size: dict[str, str | None] = {
            p.arm.name: p.config.size for p in provisioned
        }
        for r in runs:
            if r.status != RunStatus.SUCCESS or r.elapsed_ms is None:
                continue
            size = wh_size.get(r.arm_name)
            if size is None:
                continue
            r.credits_used_estimate = (
                r.elapsed_ms / 3_600_000.0 * credit_rate(size)
            )
            # Persist back to the run row so downstream views see it too.
            self.duck.execute(
                """
                UPDATE app.experiment_runs
                SET credits_used_estimate = ?
                WHERE experiment_id = ? AND arm_name = ? AND rep_index = ?
                  AND sampled_query_id = ?
                """,
                [
                    r.credits_used_estimate, r.experiment_id, r.arm_name,
                    r.rep_index, r.sampled_query_id,
                ],
            )

    def _teardown(
        self, experiment_id: int, provisioned: list[ProvisionedArm],
    ) -> None:
        """Drop test warehouses and mark them cleaned.

        Tolerates per-warehouse failures: the janitor will retry on the next
        startup via ``recover_orphaned_warehouses()``.
        """
        all_dropped = True
        for prov in provisioned:
            try:
                self.sf.execute(render_drop_warehouse_sql(prov.warehouse_name))
                logger.info("dropped test warehouse %s", prov.warehouse_name)
            except Exception:
                logger.exception(
                    "teardown: failed to drop %s; will retry on next startup",
                    prov.warehouse_name,
                )
                all_dropped = False
        if all_dropped:
            self.store.mark_test_warehouses_cleaned(experiment_id, True)
