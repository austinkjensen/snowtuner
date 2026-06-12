"""Demo runner: provision -> execute -> teardown, with progress persistence.

Coordinates the 6 cooked workloads against a real Snowflake account.  Each
warehouse runs in its own thread (with its own client / connection) so the
6 specs progress in parallel and the total wall time is dominated by the
slowest one (BURSTY's idle gaps, ~30 min).

State persistence lives in ``app.demo_runs`` so:
  - ``snowtuner demo status`` works after the seeding process dies.
  - ``snowtuner demo teardown`` can find leftover warehouses even if the
    user shut their laptop mid-run.

Cancellation: a single ``threading.Event`` is passed to every workload.
The CLI installs a SIGINT handler that sets the event, which propagates to
all workloads via their cooperative-cancellation checks.  Teardown still
runs on cancel - the alternative is leaking warehouses.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

import duckdb

from snowtuner.demo.warehouses import (
    DEMO_SPECS,
    DEMO_WAREHOUSE_PREFIX,
    DemoWarehouseSpec,
)
from snowtuner.demo.workloads import DEMO_WORKLOADS, WorkloadResult
from snowtuner.ingestion.snowflake_client import SnowflakeClient

logger = logging.getLogger(__name__)


# Standard Snowflake credit cost per "demo run" - rough order-of-magnitude
# estimate shown to the user before they hit y/n.  Real cost depends on
# edition and current credit price.  Numbers are based on each warehouse's
# size * estimated wall-clock active time.  Round-3 calibration: spill
# workloads moved to SF1000 (1.5B-key distincts + one 6B-key monster) so
# spill is guaranteed regardless of node memory - demonstrating real
# memory pressure costs real compute, by design:
#   MEMORY_HOG  XSMALL (1 cr/hr) * ~60 min active  ≈ 1.00 credits
#   LOCAL_SPILL SMALL  (2 cr/hr) * ~25 min active  ≈ 0.85 credits
#   SATURATED   SMALL  (2 cr/hr) * ~8 min active   ≈ 0.27 credits
#   OVERKILL    LARGE  (8 cr/hr) * ~3 min active   ≈ 0.40 credits
#   BURSTY      SMALL  (2 cr/hr) * ~5 min active   ≈ 0.17 credits (idle suspended)
#   HEALTHY     SMALL  (2 cr/hr) * ~2 min active   ≈ 0.07 credits
# Total ≈ 2.8 credits; round up for variance (the monster's runtime is
# the big unknown).  Recalibrate against WAREHOUSE_METERING_HISTORY
# after each dogfood run.
EST_CREDITS_PER_RUN = 3.5
EST_DOLLARS_PER_RUN = 10.50


@dataclass
class PreflightReport:
    """Result of the pre-flight grant check.

    If ``ok`` is False, ``message`` contains a copy-pasteable remediation
    SQL block for the operator to run as ACCOUNTADMIN.
    """
    ok: bool
    message: str


def preflight(client: SnowflakeClient) -> PreflightReport:
    """Verify the current role has the grants demo mode needs.

    Two requirements:
      1. CREATE WAREHOUSE ON ACCOUNT - we provision 6 warehouses.
      2. USAGE on SNOWFLAKE_SAMPLE_DATA - all workloads read TPC-H.

    We test each by attempting a no-op operation and catching the
    Snowflake-side AccessControlError.  A clean error message is far more
    helpful than letting the failure surface deep inside a CREATE
    WAREHOUSE call.
    """
    issues: list[str] = []

    # 1. CREATE WAREHOUSE: try creating + immediately dropping a sentinel.
    #    Using IF NOT EXISTS + INITIALLY_SUSPENDED so even a leak costs $0.
    sentinel = f"{DEMO_WAREHOUSE_PREFIX}PREFLIGHT_PROBE"
    try:
        client.execute(
            f"CREATE WAREHOUSE IF NOT EXISTS {sentinel} "
            f"WITH WAREHOUSE_SIZE='XSMALL' AUTO_SUSPEND=60 "
            f"INITIALLY_SUSPENDED=TRUE"
        )
        client.execute(f"DROP WAREHOUSE IF EXISTS {sentinel}")
    except Exception as e:
        if _is_access_error(e):
            issues.append(
                "Role lacks CREATE WAREHOUSE on the account.  Run as "
                "ACCOUNTADMIN:\n"
                "  GRANT CREATE WAREHOUSE ON ACCOUNT TO ROLE <snowtuner-role>;"
            )
        else:
            issues.append(f"CREATE WAREHOUSE probe failed unexpectedly: {e}")

    # 2. SNOWFLAKE_SAMPLE_DATA access: trivial select that needs USAGE on
    #    the database and SELECT on the table.  TPCH_SF1.NATION is the
    #    smallest TPC-H table - rows back in <100ms when accessible.
    try:
        client.execute(
            "SELECT COUNT(*) FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1.NATION"
        )
    except Exception as e:
        if _is_access_error(e) or "does not exist" in str(e).lower():
            issues.append(
                "Role can't read SNOWFLAKE_SAMPLE_DATA.  Run as ACCOUNTADMIN:\n"
                "  GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA\n"
                "    TO ROLE <snowtuner-role>;\n"
                "(SNOWFLAKE_SAMPLE_DATA is a free share that exists by default "
                "on every account; the grant just makes the snowtuner role "
                "able to read it.)"
            )
        else:
            issues.append(f"SNOWFLAKE_SAMPLE_DATA probe failed unexpectedly: {e}")

    # 3. TPCH_SF1000 specifically: the spill workloads need its
    #    billions-of-rows tables (ORDERS 1.5B, LINEITEM 6B).  Most
    #    accounts' sample-data share includes SF1000, but some older or
    #    partial mounts only carry SF1-SF100 - catch that here instead
    #    of 20 minutes into the run.  NATION is 25 rows; the probe is free.
    try:
        client.execute(
            "SELECT 1 FROM SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000.NATION LIMIT 1"
        )
    except Exception as e:
        if _is_access_error(e) or "does not exist" in str(e).lower():
            issues.append(
                "SNOWFLAKE_SAMPLE_DATA.TPCH_SF1000 not found - the demo's "
                "spill workloads need its billion-row tables.  Your "
                "sample-data share may be a partial mount.  Re-mount as "
                "ACCOUNTADMIN:\n"
                "  DROP DATABASE IF EXISTS SNOWFLAKE_SAMPLE_DATA;\n"
                "  CREATE DATABASE SNOWFLAKE_SAMPLE_DATA FROM SHARE "
                "SFC_SAMPLES.SAMPLE_DATA;\n"
                "  GRANT IMPORTED PRIVILEGES ON DATABASE SNOWFLAKE_SAMPLE_DATA\n"
                "    TO ROLE <snowtuner-role>;"
            )
        else:
            issues.append(f"TPCH_SF1000 probe failed unexpectedly: {e}")

    if issues:
        return PreflightReport(
            ok=False,
            message="\n\n".join(issues),
        )
    return PreflightReport(ok=True, message="all required grants present")


def _is_access_error(e: Exception) -> bool:
    """Snowflake error code 42501 = SQL access control error.

    Same heuristic used by ingestion/sources/warehouses.py for the
    GENERATION parameter probe.  Substring match because the Snowflake
    Python connector wraps the code into the exception message rather
    than exposing it as a structured attribute.
    """
    s = str(e)
    return "42501" in s or "access control" in s.lower()


def cost_summary() -> str:
    """Short human-readable cost preamble for ``snowtuner demo seed``.

    Intentionally explicit about the dollar number - users on a small
    Snowflake plan need to know this isn't free.  Pinned to standard
    edition pricing for the estimate; enterprise customers pay 1.5-2x.
    """
    return (
        f"Estimated cost: ~{EST_CREDITS_PER_RUN:.2f} credits "
        f"(~${EST_DOLLARS_PER_RUN:.2f} at $3/credit standard edition); "
        f"worst case roughly double if the deep-spill queries run long.\n"
        f"Workload runtime: ~45-70 minutes (MEMORY_HOG's deep-spill "
        f"queries are the long pole).\n"
        f"Then ACCOUNT_USAGE catches up after ~45 minutes; run "
        f"`snowtuner demo verify` at that point, then "
        f"`snowtuner sync && snowtuner run`."
    )


# ── Persistence helpers ───────────────────────────────────────────────────


def _insert_run(
    conn: duckdb.DuckDBPyConnection, warehouses: list[str],
) -> int:
    """Insert a new RUNNING row into app.demo_runs.  Returns the id."""
    conn.execute(
        """
        INSERT INTO app.demo_runs (status, warehouses, per_workload)
        VALUES ('RUNNING', ?, ?)
        """,
        [json.dumps(warehouses), json.dumps({})],
    )
    row = conn.execute(
        "SELECT id FROM app.demo_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return int(row[0])


def _update_workload(
    conn: duckdb.DuckDBPyConnection, run_id: int, result: WorkloadResult,
) -> None:
    """Merge a finished workload's result into the run's per_workload JSON.

    Done in one transaction to avoid lost updates if two workloads finish
    near-simultaneously.  We re-read the column under the lock, merge, and
    write back.
    """
    row = conn.execute(
        "SELECT per_workload FROM app.demo_runs WHERE id = ?", [run_id],
    ).fetchone()
    current = json.loads(row[0]) if row and row[0] else {}
    current[result.workload_key] = asdict(result)
    conn.execute(
        "UPDATE app.demo_runs SET per_workload = ? WHERE id = ?",
        [json.dumps(current), run_id],
    )


def _finalize_run(
    conn: duckdb.DuckDBPyConnection,
    run_id: int,
    *,
    status: str,
    notes: str | None = None,
) -> None:
    """Mark the run COMPLETED / FAILED / TORN_DOWN with a timestamp."""
    if status == "TORN_DOWN":
        conn.execute(
            "UPDATE app.demo_runs SET status = ?, torn_down_at = current_timestamp, "
            "notes = COALESCE(notes, '') || COALESCE(?, '') WHERE id = ?",
            [status, ("\n" + notes) if notes else "", run_id],
        )
    else:
        conn.execute(
            "UPDATE app.demo_runs SET status = ?, completed_at = current_timestamp, "
            "notes = COALESCE(?, notes) WHERE id = ?",
            [status, notes, run_id],
        )


# ── Provisioning ─────────────────────────────────────────────────────────


def render_create_demo_warehouse_sql(spec: DemoWarehouseSpec) -> str:
    """CREATE WAREHOUSE statement for one demo spec.

    INITIALLY_SUSPENDED=TRUE so the warehouse doesn't bill from creation
    until the first query resumes it.  COMMENT carries provenance so a
    human poking around in Snowsight understands what these are.
    """
    return (
        f"CREATE WAREHOUSE IF NOT EXISTS {spec.warehouse_name}\n"
        f"  WAREHOUSE_SIZE = '{spec.size}'\n"
        f"  AUTO_SUSPEND = {spec.auto_suspend_seconds}\n"
        f"  AUTO_RESUME = TRUE\n"
        f"  INITIALLY_SUSPENDED = TRUE\n"
        f"  COMMENT = 'snowtuner demo: {spec.workload_key}'"
    )


def _provision_one(client: SnowflakeClient, spec: DemoWarehouseSpec) -> None:
    """Provision one demo warehouse.  Raises on failure - the caller
    should fall through to teardown."""
    client.execute(render_create_demo_warehouse_sql(spec))


# ── Top-level orchestration ──────────────────────────────────────────────


def run_demo(
    *,
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
    specs: Iterable[DemoWarehouseSpec] = DEMO_SPECS,
    stop_event: threading.Event | None = None,
    skip_teardown: bool = False,
) -> int:
    """Provision, run all workloads in parallel, then tear down.

    Args:
        client:        the primary SnowflakeClient.  Cloned per warehouse
                       thread so each gets its own connection.
        conn:          DuckDB connection for app.demo_runs persistence.
                       MUST be the per-thread cursor pattern if called from
                       a server thread - the CLI passes the main connection.
        specs:         which demo warehouses to run.  Defaults to all 6;
                       tests pass a subset.
        stop_event:    cooperative-cancellation hook.  If set during
                       execution, in-flight workloads bail out and we move
                       to teardown.  Created internally if None.
        skip_teardown: leave warehouses up after the run completes.  Useful
                       when debugging on a real account; never set in
                       production.

    Returns the ``app.demo_runs.id`` of the run row so the CLI can show it.

    On exception during workload execution, we still try to tear down the
    warehouses - leaking them is worse than re-raising.
    """
    specs = list(specs)
    if stop_event is None:
        stop_event = threading.Event()

    warehouse_names = [s.warehouse_name for s in specs]
    run_id = _insert_run(conn, warehouse_names)
    logger.info("demo run %d started: %s", run_id, warehouse_names)

    provisioned: list[str] = []
    final_status = "COMPLETED"
    final_notes: str | None = None

    try:
        # ── Provision phase ──
        # Serial because CREATE WAREHOUSE is cheap (<1s each) and
        # error messages are clearer when not racing.
        for spec in specs:
            if stop_event.is_set():
                break
            try:
                _provision_one(client, spec)
                provisioned.append(spec.warehouse_name)
                logger.info("provisioned %s", spec.warehouse_name)
            except Exception as e:
                logger.error("failed to provision %s: %s", spec.warehouse_name, e)
                final_status = "FAILED"
                final_notes = f"provision failed for {spec.warehouse_name}: {e}"
                return run_id  # finally block tears down what we got

        # ── Execute phase ──
        # One thread per warehouse, each with its own cloned client so
        # connections don't share.  Workloads run for up to ~30 min; we
        # block here on the pool waiting for all to finish.
        def _run_one(spec: DemoWarehouseSpec) -> WorkloadResult | None:
            workload = DEMO_WORKLOADS.get(spec.workload_key)
            if workload is None:
                logger.error(
                    "spec %s has no workload for key=%r",
                    spec.short_name, spec.workload_key,
                )
                return None
            per_warehouse_client = client.clone()
            try:
                return workload.execute(
                    per_warehouse_client,
                    spec.warehouse_name,
                    stop_event=stop_event,
                )
            finally:
                per_warehouse_client.close()

        with ThreadPoolExecutor(max_workers=len(specs)) as pool:
            futures = {pool.submit(_run_one, s): s for s in specs}
            for fut in as_completed(futures):
                spec = futures[fut]
                try:
                    result = fut.result()
                except Exception as e:
                    logger.exception(
                        "workload %s crashed: %s", spec.workload_key, e,
                    )
                    # Stub a failed result so the operator can see what blew up.
                    result = WorkloadResult(
                        workload_key=spec.workload_key,
                        warehouse_name=spec.warehouse_name,
                        started_at=time.time(),
                        completed_at=time.time(),
                        last_error=f"workload crashed: {type(e).__name__}: {e}",
                    )
                if result is not None:
                    _update_workload(conn, run_id, result)

        if stop_event.is_set():
            final_status = "FAILED"
            final_notes = "cancelled by stop_event"

    finally:
        # ── Finalize execute state FIRST ──
        # Write COMPLETED / FAILED + completed_at before teardown so the
        # subsequent TORN_DOWN transition doesn't clobber the execute
        # status.  Without this ordering, teardown's _finalize_run sets
        # status=TORN_DOWN and then the second _finalize_run here would
        # overwrite it back to COMPLETED, losing the teardown signal.
        _finalize_run(conn, run_id, status=final_status, notes=final_notes)

        # ── Teardown phase ──
        # Always runs (unless --skip-teardown).  Leaking warehouses costs
        # the user real money; aggressively try DROP for every name we
        # provisioned, even if some fail.
        if not skip_teardown:
            torn_down, drop_errors = teardown_demo(
                client=client, conn=conn,
                names=provisioned, run_id=run_id,
            )
            if drop_errors:
                logger.warning(
                    "teardown partial: %d drop(s) failed", len(drop_errors),
                )
            logger.info("dropped %d demo warehouses", len(torn_down))

    return run_id


# ── Teardown ─────────────────────────────────────────────────────────────


def list_demo_warehouses(client: SnowflakeClient) -> list[str]:
    """Ask Snowflake for all warehouses with the demo prefix.

    Source of truth for teardown - even if app.demo_runs has stale rows,
    this finds the actual leftover warehouses.  Filter on the prefix so
    we never DROP a non-demo warehouse by mistake.
    """
    cols, rows = client.execute_with_columns(
        f"SHOW WAREHOUSES LIKE '{DEMO_WAREHOUSE_PREFIX}%'"
    )
    name_idx = {c.lower(): i for i, c in enumerate(cols)}.get("name")
    if name_idx is None:
        return []
    return [str(r[name_idx]) for r in rows]


def teardown_demo(
    *,
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
    names: Iterable[str] | None = None,
    run_id: int | None = None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Drop the named demo warehouses (or all SNOWTUNER_DEMO_* if names is None).

    Returns (dropped_names, errors) where errors is list[(name, msg)].

    Idempotent: DROP WAREHOUSE IF EXISTS never raises on a missing
    warehouse, so re-running teardown on a cleaned account is a no-op.

    Marks app.demo_runs.run_id as TORN_DOWN if a run_id is supplied.  When
    called via ``snowtuner demo teardown`` (no run_id), we also mark any
    RUNNING / COMPLETED / FAILED rows as TORN_DOWN since their warehouses
    just got dropped.
    """
    if names is None:
        names = list_demo_warehouses(client)
    names = [n for n in names if n.startswith(DEMO_WAREHOUSE_PREFIX)]

    dropped: list[str] = []
    errors: list[tuple[str, str]] = []
    for name in names:
        try:
            client.execute(f"DROP WAREHOUSE IF EXISTS {name}")
            dropped.append(name)
        except Exception as e:
            errors.append((name, str(e)))
            logger.warning("failed to drop %s: %s", name, e)

    if run_id is not None:
        _finalize_run(
            conn, run_id, status="TORN_DOWN",
            notes=f"dropped {len(dropped)} warehouses",
        )
    else:
        # Sweep teardown - mark any non-TORN_DOWN rows as torn down.
        conn.execute(
            "UPDATE app.demo_runs "
            "SET status = 'TORN_DOWN', torn_down_at = current_timestamp "
            "WHERE status != 'TORN_DOWN'"
        )

    return dropped, errors


# ── Status reporting ─────────────────────────────────────────────────────


@dataclass
class RunStatus:
    """Read-only snapshot for ``snowtuner demo status`` rendering."""
    run_id: int
    status: str
    started_at: str
    completed_at: str | None
    torn_down_at: str | None
    warehouses: list[str]
    per_workload: dict
    notes: str | None


# ── Post-hoc verification ────────────────────────────────────────────────


@dataclass
class VerifyResult:
    """One workload's PASS/FAIL after a `snowtuner demo verify` run."""
    workload_key: str
    warehouse_name: str
    expected: str
    observed: str
    verdict: str   # 'PASS: ...' or 'FAIL: ...'

    @property
    def is_pass(self) -> bool:
        return self.verdict.startswith("PASS")


def verify_demo(
    *,
    client: SnowflakeClient,
    conn: duckdb.DuckDBPyConnection,
) -> list[VerifyResult]:
    """Query ACCOUNT_USAGE for the last demo run and check each workload.

    For each demo warehouse from the latest ``app.demo_runs`` row, runs
    the appropriate ACCOUNT_USAGE query and compares the observed
    spill / queue / elapsed signals against what the workload was
    designed to produce.  Returns one ``VerifyResult`` per warehouse.

    Headless-gap fallback: ``app.demo_runs`` lives in the LOCAL DuckDB,
    so a seed run from a different host / user / DB file leaves this
    process with no run row (dogfood hit exactly this - verify said "No
    demo runs found" while the warehouses' telemetry sat in Snowflake).
    The telemetry is in ACCOUNT_USAGE regardless of where seed ran, so
    when no local run row exists we verify ALL demo warehouses over a
    24-hour window instead of bailing.

    ACCOUNT_USAGE lag: QUERY_HISTORY lags ~45 min historically (less in
    modern accounts), WAREHOUSE_EVENTS_HISTORY can lag hours.  A FAIL
    result with "no queries in ACCOUNT_USAGE" or "no events" means
    "retry later" - not necessarily a bug.
    """
    last = latest_status(conn)
    if last is not None:
        # Single-quoted timestamp literal; started_at comes from our own
        # DuckDB row, not user input.
        since_expr = f"TO_TIMESTAMP_NTZ('{last.started_at}')"
        run_warehouses = set(last.warehouses)
        specs = [s for s in DEMO_SPECS if s.warehouse_name in run_warehouses]
    else:
        since_expr = "DATEADD('hour', -24, CURRENT_TIMESTAMP())"
        specs = list(DEMO_SPECS)

    results: list[VerifyResult] = []
    for spec in specs:
        if spec.workload_key == "bursty":
            results.append(_verify_bursty(client, spec, since_expr))
        else:
            results.append(_verify_via_query_history(client, spec, since_expr))
    return results


def _verify_via_query_history(
    client: SnowflakeClient, spec: DemoWarehouseSpec, since_expr: str,
) -> VerifyResult:
    """Aggregate ACCOUNT_USAGE.QUERY_HISTORY for one warehouse and judge.

    ``since_expr`` is a SQL expression for the window start - either a
    TO_TIMESTAMP_NTZ literal from the local run row, or the DATEADD
    24-hour fallback when no run row exists.
    """
    try:
        # Identifier interpolation is safe here - warehouse_name comes from
        # DEMO_SPECS (compile-time constant) and since_expr is built by
        # verify_demo from our own DB or a constant, not user input.
        rows = client.execute(f"""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN bytes_spilled_to_local > 0 THEN 1 ELSE 0 END) AS n_local,
            SUM(CASE WHEN bytes_spilled_to_remote > 0 THEN 1 ELSE 0 END) AS n_remote,
            COALESCE(AVG(queued_overload_time), 0) AS avg_queue_ms,
            COALESCE(APPROX_PERCENTILE(total_elapsed_time, 0.99), 0) AS p99_ms,
            COALESCE(MAX(queued_overload_time), 0) AS max_queue_ms
        FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
        WHERE warehouse_name = '{spec.warehouse_name}'
          AND start_time >= {since_expr}
          AND execution_status = 'SUCCESS'
        """)
    except Exception as e:
        return VerifyResult(
            workload_key=spec.workload_key,
            warehouse_name=spec.warehouse_name,
            expected=spec.expected_finding,
            observed=f"query failed: {e}",
            verdict="FAIL: ACCOUNT_USAGE query errored",
        )

    if not rows:
        return VerifyResult(
            workload_key=spec.workload_key,
            warehouse_name=spec.warehouse_name,
            expected=spec.expected_finding,
            observed="no rows returned",
            verdict="FAIL: ACCOUNT_USAGE returned no aggregate",
        )

    n, n_local, n_remote, avg_queue_ms, p99_ms, max_queue_ms = rows[0]
    n = int(n or 0)
    n_local = int(n_local or 0)
    n_remote = int(n_remote or 0)
    avg_queue_ms = float(avg_queue_ms or 0)
    p99_ms = float(p99_ms or 0)
    max_queue_ms = float(max_queue_ms or 0)

    observed = (
        f"n={n}, local_spill={n_local}, remote_spill={n_remote}, "
        f"avg_queue={avg_queue_ms/1000:.2f}s, "
        f"max_queue={max_queue_ms/1000:.2f}s, "
        f"p99_elapsed={p99_ms/1000:.2f}s"
    )

    if n == 0:
        return VerifyResult(
            workload_key=spec.workload_key,
            warehouse_name=spec.warehouse_name,
            expected=spec.expected_finding,
            observed=observed,
            verdict=(
                "FAIL: 0 queries in ACCOUNT_USAGE - either workload "
                "didn't run, or ACCOUNT_USAGE hasn't caught up "
                "(typically ~45 min lag)"
            ),
        )

    # Per-workload judgment against the recommender's actual thresholds.
    # Match the constants in recommenders/builtins/rule_based_right_sizer.py
    # and auto_suspend_survival.py so a PASS here means the recommender
    # will fire too.
    #
    # The n >= 30 checks mirror MIN_QUERIES_FOR_READINESS: the right-sizer
    # skips any warehouse below it, so spill with too few queries is still
    # a FAIL.  Dogfood round 1 missed exactly this - 11 queries of perfect
    # spill produce zero recommendations.
    if spec.workload_key == "memory_hog":
        if n < 30:
            verdict = (
                f"FAIL: only {n} queries - below the right-sizer readiness "
                f"gate (need >=30); spill is irrelevant until n clears it"
            )
        elif n_remote > 0:
            verdict = f"PASS: {n_remote} remote spill (rule 1 -> upsize)"
        elif n_local > 0 and (n_local / n) >= 0.20:
            verdict = (
                f"PASS: {n_local}/{n} local spill = {n_local/n:.0%} "
                f"(rule 2 -> upsize)"
            )
        else:
            verdict = (
                f"FAIL: no spill (got {n_local} local, {n_remote} remote "
                f"on {n} queries; need any remote OR >=20% local)"
            )
    elif spec.workload_key == "local_spill":
        ratio = n_local / n if n > 0 else 0
        if n < 30:
            verdict = (
                f"FAIL: only {n} queries - below the right-sizer readiness "
                f"gate (need >=30); spill is irrelevant until n clears it"
            )
        elif ratio >= 0.20:
            verdict = (
                f"PASS: {n_local}/{n} local spill = {ratio:.0%} "
                f"(rule 2 -> upsize)"
            )
        else:
            verdict = (
                f"FAIL: {n_local}/{n} = {ratio:.0%} local spill "
                f"(need >=20%)"
            )
    elif spec.workload_key == "saturated":
        if avg_queue_ms >= 5000 and n >= 30:
            verdict = (
                f"PASS: avg queue {avg_queue_ms/1000:.1f}s on {n} queries "
                f"(rule 3 -> upsize)"
            )
        else:
            verdict = (
                f"FAIL: avg queue {avg_queue_ms/1000:.1f}s on {n} queries "
                f"(need >=5s and >=30 queries)"
            )
    elif spec.workload_key == "overkill":
        if n >= 100 and p99_ms <= 1000 and n_local == 0 and n_remote == 0 and avg_queue_ms < 1000:
            verdict = (
                f"PASS: p99 {p99_ms:.0f}ms on {n} queries, no spill/queue "
                f"(rule 4 -> downsize)"
            )
        else:
            verdict = (
                f"FAIL: doesn't meet rule 4 (n={n}>=100? p99={p99_ms:.0f}<=1000? "
                f"spill={n_local+n_remote}==0? queue={avg_queue_ms:.0f}<1000?)"
            )
    elif spec.workload_key == "healthy":
        # Control: should NOT trip any upsize/downsize rule.
        triggers = []
        if n_remote > 0:
            triggers.append("rule 1 (remote spill)")
        if n > 0 and (n_local / n) >= 0.20:
            triggers.append("rule 2 (local spill)")
        if avg_queue_ms >= 5000 and n >= 30:
            triggers.append("rule 3 (queue)")
        if n >= 100 and p99_ms <= 1000 and n_local == 0 and n_remote == 0 and avg_queue_ms < 1000:
            triggers.append("rule 4 (downsize)")
        if not triggers:
            verdict = f"PASS: control - no rule triggered on {n} queries"
        else:
            verdict = f"FAIL: control unexpectedly triggers {', '.join(triggers)}"
    else:
        verdict = f"FAIL: unknown workload_key {spec.workload_key!r}"

    return VerifyResult(
        workload_key=spec.workload_key,
        warehouse_name=spec.warehouse_name,
        expected=spec.expected_finding,
        observed=observed,
        verdict=verdict,
    )


def _verify_bursty(
    client: SnowflakeClient, spec: DemoWarehouseSpec, since_expr: str,
) -> VerifyResult:
    """The auto-suspend survival tuner models idle gaps from QUERY_HISTORY.

    Since the gap-based rework, the recommendation needs >=10 idle gaps
    >= 60s between busy periods - whether or not the warehouse actually
    suspended.  So that's what we verify, from QUERY_HISTORY (~45 min lag)
    instead of WAREHOUSE_EVENTS_HISTORY (hours).  Suspend/resume events
    are only an enrichment for the cold-start cost now.

    Gap detection mirrors the warehouse_idle_gaps feature transform:
    compute-bearing statements only, overlapping intervals merged via a
    running MAX(end_time), a gap wherever the next start_time clears it.
    """
    try:
        rows = client.execute(f"""
        WITH q AS (
            SELECT start_time,
                   MAX(end_time) OVER (
                       ORDER BY start_time, end_time
                       ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                   ) AS prev_max_end
            FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
            WHERE warehouse_name = '{spec.warehouse_name}'
              AND start_time >= {since_expr}
              AND execution_status = 'SUCCESS'
              AND COALESCE(execution_time, 0) > 0
        )
        SELECT COUNT(*) AS n_gaps,
               COALESCE(MEDIAN(DATEDIFF('second', prev_max_end, start_time)), 0)
                   AS median_gap_s
        FROM q
        WHERE prev_max_end IS NOT NULL
          AND start_time > prev_max_end
          AND DATEDIFF('second', prev_max_end, start_time) >= 60
        """)
    except Exception as e:
        return VerifyResult(
            workload_key=spec.workload_key,
            warehouse_name=spec.warehouse_name,
            expected=spec.expected_finding,
            observed=f"gap query failed: {e}",
            verdict="FAIL: QUERY_HISTORY gap query errored",
        )

    n_gaps, median_gap_s = (rows[0] if rows else (0, 0))
    n_gaps = int(n_gaps or 0)
    median_gap_s = float(median_gap_s or 0)
    observed = f"{n_gaps} idle gaps >= 60s, median {median_gap_s:.0f}s"

    if n_gaps >= 10:
        verdict = (
            f"PASS: {n_gaps} idle gaps (median {median_gap_s:.0f}s) - "
            f"auto-suspend survival tuner will fire"
        )
    elif n_gaps == 0:
        verdict = (
            "FAIL: 0 idle gaps - either the workload's burst/idle cycles "
            "didn't run, or QUERY_HISTORY hasn't caught up (~45 min lag)"
        )
    else:
        verdict = (
            f"FAIL: only {n_gaps} gaps (need >=10; may be lag - retry later)"
        )

    return VerifyResult(
        workload_key=spec.workload_key,
        warehouse_name=spec.warehouse_name,
        expected=spec.expected_finding,
        observed=observed,
        verdict=verdict,
    )


def latest_status(conn: duckdb.DuckDBPyConnection) -> RunStatus | None:
    """Return the most recent app.demo_runs row, or None if no runs exist."""
    row = conn.execute(
        """
        SELECT id, status, started_at, completed_at, torn_down_at,
               warehouses, per_workload, notes
        FROM app.demo_runs
        ORDER BY id DESC LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return RunStatus(
        run_id=int(row[0]),
        status=str(row[1]),
        started_at=str(row[2]),
        completed_at=str(row[3]) if row[3] is not None else None,
        torn_down_at=str(row[4]) if row[4] is not None else None,
        warehouses=json.loads(row[5]) if row[5] else [],
        per_workload=json.loads(row[6]) if row[6] else {},
        notes=str(row[7]) if row[7] else None,
    )
