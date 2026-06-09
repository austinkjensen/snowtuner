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
    """Forced full-materialized sort on XSMALL -> heavy spill.

    The trick is the WHERE clause on row-number: filtering on a slice in
    the MIDDLE of the sort order (not the top) defeats Snowflake's top-K
    optimization, forcing the optimizer to actually materialize the entire
    sort.  60M rows * ~16 bytes for the projected columns = ~1 GB of sort
    state, plus algorithmic overhead 2-3x, well past XSMALL's working
    memory.  Tested empirically against a clean account; produces local
    spill reliably and remote spill on accounts with smaller local-disk
    XSMALL configurations.

    Previous version (TPC-H Q1 GROUP BY l_returnflag) produced 4-row
    output, so the hash table held 4 partial aggregates and never
    spilled.  Don't revert.
    """
    key = "memory_hog"
    description = "Forced full sort on TPCH_SF10.LINEITEM, sized to spill"
    estimated_minutes = 10.0

    # Mid-range row filter: optimizer can't use top-K because the slice
    # isn't at the top of the sort.  Returns just 11 rows; the work is
    # entirely in the sort.
    _SQL = """
    SELECT l_orderkey, l_partkey, l_extendedprice, rn
    FROM (
      SELECT l_orderkey, l_partkey, l_extendedprice,
             ROW_NUMBER() OVER (ORDER BY l_extendedprice DESC, l_orderkey ASC) AS rn
      FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM
    )
    WHERE rn BETWEEN 30000000 AND 30000010
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
        # The default prepare_session() caps STATEMENT_TIMEOUT at 600s.
        # XSMALL on SF10 with forced full sort takes 4-7 minutes when it
        # has to spill, sometimes more on a slow account.  Override to
        # 25 min so a marginal account doesn't kill the query before we
        # see the spill - the surrounding workload runner enforces its
        # own outer-bound timing.
        executor = _new_executor(client, warehouse_name)
        try:
            executor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1500")
        except Exception as e:
            logger.warning(
                "memory_hog: failed to bump statement timeout: %s", e,
            )
        # Two reps so the recommender's "any remote spill" rule has a
        # second chance if the first run lands just under the threshold.
        for sql in [self._SQL.strip()] * 2:
            if stop_event.is_set():
                result.notes.append("stopped early")
                break
            result.queries_attempted += 1
            try:
                executor.execute(sql)
                result.queries_succeeded += 1
            except Exception as e:
                result.queries_failed += 1
                result.last_error = f"{type(e).__name__}: {e}"
                logger.warning("memory_hog query failed: %s", e)
        result.completed_at = time.time()
        return result


# ── workload 2: LOCAL_SPILL ───────────────────────────────────────────────


class LocalSpillWorkload(DemoWorkload):
    """SF10 forced-full-sort on SMALL -> ~30% of queries spill local.

    Same ROW_NUMBER + mid-range filter trick as MemoryHog (defeats top-K),
    but the slice and warehouse size are different: SF10 LINEITEM (60M
    rows, ~1 GB projected sort state) on SMALL (~4-8 GB memory) is tight
    enough that the heavy query spills LOCAL but won't blow past disk
    into REMOTE - which is exactly what rule 2 wants to see.

    Interleaved 3 heavy + 7 light = 30% heavy.  Recommender rule 2 fires
    at >=20% local-spill ratio, so 30% gives us headroom for variance.

    Previous version used SF1 with LIMIT 50000 - the LIMIT enables
    top-K optimization (priority queue of 50000 elements, ~5 MB working
    set) and nothing spilled.  The mid-range filter is the fix.
    """
    key = "local_spill"
    description = "SF10 forced-sort on SMALL, ~30% of queries spill local"
    estimated_minutes = 6.0

    # Same mid-range filter trick - defeats top-K.  SF10 instead of SF1
    # because SMALL is too big to spill on SF1.
    _HEAVY_SQL = """
    SELECT l_orderkey, l_partkey, l_extendedprice, rn
    FROM (
      SELECT l_orderkey, l_partkey, l_extendedprice,
             ROW_NUMBER() OVER (ORDER BY l_extendedprice DESC, l_orderkey ASC) AS rn
      FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM
    )
    WHERE rn BETWEEN 25000000 AND 25000010
    """

    # Cheap aggregate that fits in memory; no spill.
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
        # Bump timeout for heavy queries - same reasoning as MemoryHog.
        executor = _new_executor(client, warehouse_name)
        try:
            executor.execute("ALTER SESSION SET STATEMENT_TIMEOUT_IN_SECONDS = 1500")
        except Exception as e:
            logger.warning(
                "local_spill: failed to bump statement timeout: %s", e,
            )
        # 3 heavy + 7 light = 30% heavy.  Interleaved so warehouse memory
        # pressure doesn't get release time between heavies.
        queries = (
            [self._HEAVY_SQL.strip()] * 3
            + [self._LIGHT_SQL.strip()] * 7
        )
        for sql in queries:
            if stop_event.is_set():
                result.notes.append("stopped early")
                break
            result.queries_attempted += 1
            try:
                executor.execute(sql)
                result.queries_succeeded += 1
            except Exception as e:
                result.queries_failed += 1
                result.last_error = f"{type(e).__name__}: {e}"
                logger.warning("local_spill query failed: %s", e)
        result.completed_at = time.time()
        return result


# ── workload 3: SATURATED ─────────────────────────────────────────────────


class SaturatedWorkload(DemoWorkload):
    """60 concurrent SF10 aggregates on single-cluster SMALL -> heavy queue.

    Cost calculation:
      - Each query: full scan of SF10 LINEITEM (60M rows), ~8-12 s on SMALL.
      - SMALL single-cluster MAX_CONCURRENCY_LEVEL defaults to 8.
      - 60 queries / 8 concurrent = ~7.5 batches at 10s each.
      - Avg queue per query: ~30s (well past the 5s rule-3 threshold).
      - Peak queue: ~70s on the last-fired queries.

    Previous version used 40 queries of SF1 COUNT-DISTINCT (~200 ms each).
    With 200ms per query on 8 concurrent, even 40 in a burst clears in <2s
    of wall time and peak queue was 280ms - nowhere near 5s.  The fix is
    LONGER queries plus MORE of them; without both, modern Snowflake's
    concurrency model swallows the load.

    Memory note: 60 concurrent Python threads each with a Snowflake
    connection eats ~3 GB.  Fits in t3.medium's 4 GB + 2 GB swap.  If we
    hit OOM on a smaller instance, switch to execute_async on one
    connection (TODO #76 followup).
    """
    key = "saturated"
    description = "60 concurrent SF10 aggregates on SMALL -> 30s+ queueing"
    estimated_minutes = 6.0

    # Full table scan + 3-row group-by output.  Cheap output but the scan
    # itself takes 8-12s on SMALL, which is what we need for queue pile-up.
    _CONCURRENT_SQL = """
    SELECT
        l_returnflag,
        COUNT(*) AS n,
        AVG(l_extendedprice) AS avg_price,
        MAX(l_shipdate) AS max_ship
    FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF10.LINEITEM
    GROUP BY l_returnflag
    """

    _FAN_OUT = 60

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
