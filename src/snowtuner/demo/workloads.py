"""Query patterns that the 6 demo warehouses run.

Each workload is engineered to trip a specific recommender rule by
generating queries with known characteristics (spill, queue, latency,
suspend cycles).  All queries read from ``SNOWFLAKE_SAMPLE_DATA`` so the
user doesn't need to load any data of their own - the demo bootstrap just
needs IMPORTED PRIVILEGES on that database.

A workload is anything implementing ``DemoWorkload.execute()``.  Workloads
get a ``SnowflakeClient`` (for spawning additional connections - the
saturated workload needs many) and the target warehouse name (already
created and resumable by the runner).  They return a ``WorkloadResult``
summarizing what happened so the runner can persist it.

Workloads MUST be cooperative with ``stop_event``: check it between
queries / between bursts and bail out cleanly.  This lets ``snowtuner demo
teardown`` (or Ctrl-C) cut a long workload short.
"""
from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from snowtuner.experiments.replay import prepare_session
from snowtuner.ingestion.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)


@dataclass
class WorkloadResult:
    """What a single workload run produced.

    Persisted into ``app.demo_runs.per_workload`` so ``snowtuner demo
    status`` can show progress and so failures are visible after the fact.
    """
    workload_key: str
    warehouse_name: str
    queries_attempted: int = 0
    queries_succeeded: int = 0
    queries_failed: int = 0
    started_at: float = 0.0  # epoch seconds
    completed_at: float | None = None
    last_error: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float | None:
        if self.completed_at is None:
            return None
        return self.completed_at - self.started_at


class DemoWorkload(ABC):
    """Abstract base for one warehouse's query pattern."""

    #: Stable key matching ``DemoWarehouseSpec.workload_key``.
    key: str = ""
    #: Human-readable one-liner for ``snowtuner demo seed`` output.
    description: str = ""
    #: Rough wall-clock budget for the runner's "this will take X min" UX.
    #: Sum across all workloads (running in parallel) gives the runner's
    #: estimate.  Real time can vary by ~30% on busy accounts.
    estimated_minutes: float = 1.0

    @abstractmethod
    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        """Run the workload against ``warehouse_name`` and return the result.

        Implementations:
          - SHOULD honor ``stop_event.is_set()`` between queries / bursts.
          - SHOULD log per-query failures via ``logger.warning`` and record
            them on ``result.last_error``, but keep going so we surface as
            much data as possible.
          - MUST NOT raise on individual query failures - only on bootstrap
            failures (no connection, no warehouse, no SNOWFLAKE_SAMPLE_DATA).
            The runner treats workload exceptions as fatal for that
            warehouse and moves on to teardown.
        """


# ── shared helpers ────────────────────────────────────────────────────────


def _new_executor(client: SnowflakeClient, warehouse_name: str):
    """Spin up a fresh connection-backed executor pinned to ``warehouse_name``.

    We reuse the ``SnowflakeExecutorAdapter`` shape from the experiment
    engine - it captures sfqid which we don't actually use here, but the
    interface match means we get ``prepare_session()`` for free.

    NOTE on threading: the adapter calls ``client._connect()`` which returns
    a shared lazy connection.  Two threads calling this with the SAME
    client will share that connection and serialize behind one cursor.
    Concurrent workloads (SaturatedWorkload) must pass ``client.clone()``
    so each thread gets its own connection.
    """
    from snowtuner.experiments.engine import SnowflakeExecutorAdapter

    executor = SnowflakeExecutorAdapter(client)
    prepare_session(executor, warehouse_name)
    return executor


def _run_serial(
    *,
    client: SnowflakeClient,
    warehouse_name: str,
    queries: list[str],
    stop_event: threading.Event,
    result: WorkloadResult,
    inter_query_sleep_seconds: float = 0.0,
) -> None:
    """Run a list of queries in order, one connection, recording results.

    Common path for memory_hog / local_spill / overkill / healthy.  Each
    failure is recorded but doesn't halt the loop - the recommender just
    needs *enough* successful queries with the target characteristics.
    """
    executor = _new_executor(client, warehouse_name)
    for sql in queries:
        if stop_event.is_set():
            result.notes.append(
                f"stopped early after {result.queries_attempted}/{len(queries)} queries"
            )
            return
        result.queries_attempted += 1
        try:
            executor.execute(sql)
            result.queries_succeeded += 1
        except Exception as e:
            result.queries_failed += 1
            result.last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "demo workload %r: query %d failed: %s",
                result.workload_key, result.queries_attempted, e,
            )
        if inter_query_sleep_seconds > 0 and not stop_event.is_set():
            time.sleep(inter_query_sleep_seconds)


# ── workload 1: MEMORY_HOG ────────────────────────────────────────────────


class _SpillWorkloadBase(DemoWorkload):
    """Shared machinery for the two spill workloads (memory_hog, local_spill).

    Design constraints discovered the hard way (2026-06-08 dogfood, two
    failed rounds - read before "improving" this):

    1. The right-sizer SKIPS warehouses with fewer than 30 SUCCESS queries
       in the window (MIN_QUERIES_FOR_READINESS).  Session-setup statements
       (USE WAREHOUSE / ALTER SESSION) count toward n, but only ~5 of them.
       So every spill workload pads with light queries to clear the gate.

    2. Rule 2 needs >=20% of queries spilling.  With N_HEAVY=8 heavy
       spillers, N_LIGHT=20 lights, and ~6 setup statements, the ratio is
       8/34 = 24% - headroom for one heavy failing (7/33 = 21%).  Failed
       queries don't count in n at all (execution_status filter), so a
       timeout hurts the sample size, not the ratio.

    3. The spill primitive is exact COUNT(DISTINCT col_a, col_b) over a
       high-cardinality composite key.  Unlike ORDER BY (top-K shortcut)
       or low-cardinality GROUP BY (tiny hash table), exact distinct has
       no shortcut: the hash table must hold every distinct key.  SF100
       LINEITEM has ~600M near-unique (l_orderkey, l_partkey) pairs ->
       15-20 GB of hash state, beyond any current XSMALL/SMALL node
       memory.  Round-1 mistakes: TPC-H Q1 (4-row output, nothing to
       spill) and ROW_NUMBER mid-range filter (bounded heap, ~600 MB).

    Heavies run through a small thread pool so wall time stays in the
    ~15-25 min range instead of N_HEAVY * per-query minutes.  Concurrent
    heavies also share node memory, which makes spill MORE likely, and
    any queueing they cause is harmless (Rule 2 is checked before Rule 3).
    """

    _HEAVY_SQL = """
    SELECT COUNT(DISTINCT l_orderkey, l_partkey) AS n_pairs
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF100.LINEITEM
    """

    _LIGHT_SQL = """
    SELECT l_returnflag, COUNT(*) AS n
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
    GROUP BY l_returnflag
    """

    _N_HEAVY = 8
    _N_LIGHT = 20
    _HEAVY_CONCURRENCY = 4

    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        result = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
        )

        # Lights first: cheap (~1-2s each), get the warehouse resumed and
        # the n>=30 readiness gate cleared even if every heavy times out.
        _run_serial(
            client=client, warehouse_name=warehouse_name,
            queries=[self._LIGHT_SQL.strip()] * self._N_LIGHT,
            stop_event=stop_event, result=result,
        )

        # Heavies through a small pool, each on its own cloned connection.
        # Per-clone session needs its own timeout bump: a spilled 600M-key
        # distinct on XSMALL runs 5-15 min, past prepare_session's 600s cap.
        def _one_heavy(_i: int) -> tuple[bool, str | None]:
            if stop_event.is_set():
                return False, "stopped"
            per_thread = client.clone()
            try:
                ex = _new_executor(per_thread, warehouse_name)
                ex.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1500")
                ex.execute(self._HEAVY_SQL.strip())
                return True, None
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
            finally:
                per_thread.close()

        with ThreadPoolExecutor(max_workers=self._HEAVY_CONCURRENCY) as pool:
            futures = [pool.submit(_one_heavy, i) for i in range(self._N_HEAVY)]
            for fut in as_completed(futures):
                result.queries_attempted += 1
                ok, err = fut.result()
                if ok:
                    result.queries_succeeded += 1
                else:
                    result.queries_failed += 1
                    if err and err != "stopped":
                        result.last_error = err
                        logger.warning(
                            "%s heavy query failed: %s", self.key, err,
                        )

        result.completed_at = time.time()
        return result


class MemoryHogWorkload(_SpillWorkloadBase):
    """600M-key exact distinct on XSMALL -> every heavy query spills hard.

    Demonstrates right-sizer Rule 2 (sustained local spill -> upsize).
    Rule 1 (remote spill) was the original target but is not demoable at
    sane cost: remote spill requires exhausting the node's local SSD
    (hundreds of GB of spill), which means a multi-hour query.  If a
    heavy does push into remote on a constrained account, Rule 1 fires
    instead - same upsize recommendation, stronger evidence.
    """
    key = "memory_hog"
    description = "8x 600M-key exact distinct on XSMALL - sustained local spill"
    estimated_minutes = 25.0


# ── workload 2: LOCAL_SPILL ───────────────────────────────────────────────


class LocalSpillWorkload(_SpillWorkloadBase):
    """Same 600M-key distinct, but on SMALL -> intermittent local spill.

    SMALL has roughly twice XSMALL's memory and spreads the hash state
    across more capacity, so the same heavy query spills less deeply -
    the "workload routinely runs out of memory but limps through" pattern
    Rule 2 describes, vs MEMORY_HOG's every-query-drowns variant.  Faster
    per-heavy than MEMORY_HOG (~3-6 min vs 5-15) for the same reason.
    """
    key = "local_spill"
    description = "8x 600M-key exact distinct on SMALL - intermittent spill"
    estimated_minutes = 15.0


# ── workload 3: SATURATED ─────────────────────────────────────────────────


class SaturatedWorkload(DemoWorkload):
    """80 concurrent CPU-bound distincts on single-cluster SMALL -> queue.

    Round-2 lesson (dogfood 2026-06-08): scan-bound queries do NOT queue.
    The 60x SF10 GROUP BY l_returnflag version hit the local data cache
    after the first batch (narrow columns compress to ~0.25 GB), queries
    dropped to ~2-3s, and Snowflake's resource-based admission ran far
    more than 8 of them concurrently - max queue landed at 4.76s, just
    under the 5s threshold.

    Exact COUNT(DISTINCT l_orderkey) over 60M rows is CPU-bound: ~15M
    distinct keys must be hashed and deduped regardless of cache state,
    so per-query cost stays ~10-20s AND each query holds real memory,
    which keeps effective concurrency near the nominal 8.  80 queries
    deep, the tail waits through ~9 batches: avg queue 40-60s, far past
    the rule-3 threshold (avg >=5s, n>=30).

    Memory note: 80 concurrent Python threads each holding a Snowflake
    connection costs ~3-4 GB on the client host.  Fits t3.medium's 4 GB
    + 2 GB swap.  If a smaller host OOMs, switch to execute_async on a
    handful of connections.
    """
    key = "saturated"
    description = "80 concurrent CPU-bound distincts on SMALL -> 40s+ queueing"
    estimated_minutes = 8.0

    # Cache-resistant: the cost is hashing 60M values into ~15M distinct
    # keys, not reading bytes.  Output is 1 row.
    _CONCURRENT_SQL = """
    SELECT COUNT(DISTINCT l_orderkey) AS n_orders
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM
    """

    _FAN_OUT = 80

    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        result = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
        )

        def _one(_i: int) -> tuple[bool, str | None]:
            if stop_event.is_set():
                return False, "stopped"
            # Clone the client so this thread gets its own connection.  All
            # 40 tasks sharing the input client would serialize on one
            # cursor, defeating the whole point of the saturated pattern.
            per_thread_client = client.clone()
            try:
                ex = _new_executor(per_thread_client, warehouse_name)
                ex.execute(self._CONCURRENT_SQL.strip())
                return True, None
            except Exception as e:
                return False, f"{type(e).__name__}: {e}"
            finally:
                per_thread_client.close()

        # Worker count == fan-out so all 40 fire ~simultaneously.  Snowflake
        # connection setup takes 100-300ms - acceptable burst for a demo.
        with ThreadPoolExecutor(max_workers=self._FAN_OUT) as pool:
            futures = [pool.submit(_one, i) for i in range(self._FAN_OUT)]
            for fut in as_completed(futures):
                result.queries_attempted += 1
                ok, err = fut.result()
                if ok:
                    result.queries_succeeded += 1
                else:
                    result.queries_failed += 1
                    if err:
                        result.last_error = err

        result.completed_at = time.time()
        return result


# ── workload 4: OVERKILL ──────────────────────────────────────────────────


class OverkillWorkload(DemoWorkload):
    """120 trivial queries on LARGE -> p99 <= 1s, no spill, no queueing.

    Hits the right-sizer's downsize rule:
        n >= 100 AND p99 <= 1s AND no spills AND no queueing -> -1 size.
    LARGE running a query that finishes in 100ms is the textbook
    "overprovisioned" pattern.
    """
    key = "overkill"
    description = "120 trivial queries on LARGE -> downsize candidate"
    estimated_minutes = 3.0

    _SQL = "SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION"
    _N_QUERIES = 120

    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        result = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
        )
        _run_serial(
            client=client, warehouse_name=warehouse_name,
            queries=[self._SQL] * self._N_QUERIES,
            stop_event=stop_event, result=result,
        )
        result.completed_at = time.time()
        return result


# ── workload 5: BURSTY ────────────────────────────────────────────────────


class BurstyWorkload(DemoWorkload):
    """10 cycles of (5-query burst + 150s idle).

    AUTO_SUSPEND=120 means the warehouse suspends ~30s into each idle gap.
    Each cycle: burst (~25s) + idle (~150s) = ~3 min.  10 cycles produces
    >=10 reactivation gaps - enough for the survival recommender's
    MIN_CYCLES_PER_WAREHOUSE=10 threshold.

    The recommender then proposes lowering AUTO_SUSPEND from 120 to ~60
    (delta of 60s, safely past MIN_DELTA_SECONDS=30).
    """
    key = "bursty"
    description = (
        "10 burst-then-idle cycles, AUTO_SUSPEND=120 -> recommend lower"
    )
    estimated_minutes = 32.0  # 10 cycles * 3.2 min

    _SQL = "SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS"
    _QUERIES_PER_BURST = 5
    _N_CYCLES = 10
    _IDLE_SECONDS = 150.0  # > AUTO_SUSPEND=120 so warehouse actually suspends

    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        result = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
        )
        for cycle in range(self._N_CYCLES):
            if stop_event.is_set():
                result.notes.append(f"stopped after cycle {cycle}/{self._N_CYCLES}")
                break
            # Fresh executor per cycle - the warehouse suspended during the
            # idle gap, and re-using a stale connection just adds a confusing
            # "your warehouse was suspended" round-trip.
            executor = _new_executor(client, warehouse_name)
            for _ in range(self._QUERIES_PER_BURST):
                if stop_event.is_set():
                    break
                result.queries_attempted += 1
                try:
                    executor.execute(self._SQL)
                    result.queries_succeeded += 1
                except Exception as e:
                    result.queries_failed += 1
                    result.last_error = f"{type(e).__name__}: {e}"
                    logger.warning(
                        "bursty cycle %d: query failed: %s", cycle, e,
                    )
            # Sleep through the idle gap, but in small slices so stop_event
            # can interrupt promptly.
            slept = 0.0
            while slept < self._IDLE_SECONDS and not stop_event.is_set():
                time.sleep(min(2.0, self._IDLE_SECONDS - slept))
                slept += 2.0
        result.completed_at = time.time()
        return result


# ── workload 6: HEALTHY (control) ─────────────────────────────────────────


class HealthyWorkload(DemoWorkload):
    """Steady SF1 workload sized appropriately for SMALL.  No expected finding.

    The control case.  Proves the optimizer doesn't fabricate a
    recommendation when the warehouse is sized right.
    """
    key = "healthy"
    description = "Steady SF1 queries, SMALL sized appropriately - no rec"
    estimated_minutes = 3.0

    _SQLS = [
        "SELECT l_returnflag, COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM "
        "GROUP BY l_returnflag",
        "SELECT n_regionkey, COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION "
        "GROUP BY n_regionkey",
        "SELECT o_orderstatus, COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.ORDERS "
        "GROUP BY o_orderstatus",
        "SELECT COUNT(DISTINCT c_nationkey) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.CUSTOMER",
        "SELECT MAX(l_shipdate), MIN(l_shipdate) "
        "FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM",
    ]
    _N_LOOPS = 10

    def execute(
        self,
        client: SnowflakeClient,
        warehouse_name: str,
        *,
        stop_event: threading.Event,
    ) -> WorkloadResult:
        result = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
        )
        queries = self._SQLS * self._N_LOOPS  # 50 queries total
        _run_serial(
            client=client, warehouse_name=warehouse_name,
            queries=queries, stop_event=stop_event, result=result,
            inter_query_sleep_seconds=0.5,  # gentle pacing, no queueing
        )
        result.completed_at = time.time()
        return result


# ── registry ──────────────────────────────────────────────────────────────


DEMO_WORKLOADS: dict[str, DemoWorkload] = {
    w.key: w for w in [
        MemoryHogWorkload(),
        LocalSpillWorkload(),
        SaturatedWorkload(),
        OverkillWorkload(),
        BurstyWorkload(),
        HealthyWorkload(),
    ]
}
