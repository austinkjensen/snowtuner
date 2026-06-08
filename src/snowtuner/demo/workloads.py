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


class MemoryHogWorkload(DemoWorkload):
    """Heavy TPC-H aggregations on XSMALL -> guaranteed remote spill.

    ``TPCH_SF10.LINEITEM`` has ~60M rows.  A full GROUP BY + ORDER BY on
    XSMALL (~2 GB memory) can't fit the intermediate hash table in RAM, so
    Snowflake spills to local disk and then to remote storage.  Run twice
    so the recommender sees ``n_remote >= 1`` reliably.
    """
    key = "memory_hog"
    description = "Heavy aggregate on TPCH_SF10.LINEITEM, sized to spill remote"
    estimated_minutes = 8.0

    _SQL = """
    SELECT
        l_returnflag,
        l_linestatus,
        SUM(l_quantity)        AS sum_qty,
        SUM(l_extendedprice)   AS sum_base_price,
        SUM(l_extendedprice * (1 - l_discount)) AS sum_disc_price,
        AVG(l_quantity)        AS avg_qty,
        AVG(l_extendedprice)   AS avg_price,
        COUNT(*)               AS count_order
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM
    GROUP BY l_returnflag, l_linestatus
    ORDER BY l_returnflag, l_linestatus
    """

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
            queries=[self._SQL.strip()] * 2,
            stop_event=stop_event, result=result,
        )
        result.completed_at = time.time()
        return result


# ── workload 2: LOCAL_SPILL ───────────────────────────────────────────────


class LocalSpillWorkload(DemoWorkload):
    """Medium TPC-H sorts on SMALL -> >=20% queries spill local, not remote.

    SF1 LINEITEM is ~6M rows; SMALL warehouse has ~4 GB memory.  A windowed
    ORDER BY pushes about 1 GB through the sort buffer - tight enough that
    a fraction spill to local but none to remote.  Interleave with cheap
    aggregates so the spill ratio lands at ~30% (above the 20% threshold).
    """
    key = "local_spill"
    description = "Mixed SF1 sorts on SMALL, ~30% of queries spill to local"
    estimated_minutes = 4.0

    _HEAVY_SQL = """
    SELECT
        l_partkey,
        l_extendedprice,
        ROW_NUMBER() OVER (PARTITION BY l_suppkey ORDER BY l_extendedprice DESC) AS rn
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
    ORDER BY l_extendedprice DESC
    LIMIT 50000
    """

    _LIGHT_SQL = """
    SELECT l_returnflag, COUNT(*) AS n
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
    GROUP BY l_returnflag
    """

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
        # 3 heavy + 7 light = 30% heavy.  Heavy queries spill; light don't.
        # Interleaved so warehouse memory pressure doesn't get release time
        # between heavies.
        queries = (
            [self._HEAVY_SQL.strip()] * 3
            + [self._LIGHT_SQL.strip()] * 7
        )
        _run_serial(
            client=client, warehouse_name=warehouse_name,
            queries=queries, stop_event=stop_event, result=result,
        )
        result.completed_at = time.time()
        return result


# ── workload 3: SATURATED ─────────────────────────────────────────────────


class SaturatedWorkload(DemoWorkload):
    """40 concurrent COUNT-DISTINCT queries on a single-cluster SMALL.

    A SMALL warehouse runs ~8 queries concurrently per cluster.  Firing 40
    at once parks ~32 in the queue, producing avg_queue_overload >> 5s.
    Each task uses its own connection (Snowflake cursors are one-statement-
    at-a-time) - thread pool kept small enough that the laptop / EC2 host
    isn't strained.
    """
    key = "saturated"
    description = "40 concurrent SF1 queries on single-cluster SMALL -> queue"
    estimated_minutes = 5.0

    _CONCURRENT_SQL = """
    SELECT COUNT(DISTINCT l_partkey), COUNT(DISTINCT l_suppkey)
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.LINEITEM
    """

    _FAN_OUT = 40

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
