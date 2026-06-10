"""Unit tests for the demo runner's orchestration logic.

These tests don't talk to Snowflake.  A ``FakeClient`` stands in for
``SnowflakeClient``: records every SQL call, optionally raises configured
errors, and supports ``clone()``.  Workload classes are also stubbed so we
test the runner's wiring (provision -> execute -> persist -> teardown)
without depending on TPC-H or thread timing.
"""
from __future__ import annotations

import threading
import time
from typing import Any

import duckdb
import pytest

from snowtuner.demo import warehouses as demo_warehouses
from snowtuner.demo.runner import (
    _finalize_run,
    _insert_run,
    _update_workload,
    cost_summary,
    latest_status,
    list_demo_warehouses,
    preflight,
    render_create_demo_warehouse_sql,
    run_demo,
    teardown_demo,
)
from snowtuner.demo.warehouses import DemoWarehouseSpec
from snowtuner.demo.workloads import DemoWorkload, WorkloadResult


# ── Fakes ─────────────────────────────────────────────────────────────────


class FakeClient:
    """Stand-in for SnowflakeClient.  Records SQL; optionally raises.

    ``raise_for_substring`` makes any execute() containing the substring
    raise the given exception.  Used to simulate access-control failures.
    """

    def __init__(
        self,
        *,
        raise_for_substring: dict[str, Exception] | None = None,
        show_warehouse_rows: list[tuple[Any, ...]] | None = None,
        rows_for_substring: dict[str, list[tuple[Any, ...]]] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.raise_for_substring = raise_for_substring or {}
        self.show_warehouse_rows = show_warehouse_rows or []
        # First substring match wins.  Useful for verify tests where the
        # ACCOUNT_USAGE query needs to return specific rows per warehouse.
        self.rows_for_substring = rows_for_substring or {}
        self.credentials = "fake-creds"  # what clone() copies

    def execute(self, sql: str, params: list | None = None) -> list[tuple]:
        self.calls.append(sql)
        for substr, exc in self.raise_for_substring.items():
            if substr.lower() in sql.lower():
                raise exc
        for substr, rows in self.rows_for_substring.items():
            if substr.lower() in sql.lower():
                return rows
        return []

    def execute_with_columns(self, sql: str, params: list | None = None):
        self.calls.append(sql)
        if "SHOW WAREHOUSES" in sql.upper():
            return (["name", "size"], list(self.show_warehouse_rows))
        return ([], [])

    def close(self) -> None:
        pass

    def clone(self) -> "FakeClient":
        # Share the same calls list so the test can see clone-side calls too.
        c = FakeClient(
            raise_for_substring=self.raise_for_substring,
            show_warehouse_rows=self.show_warehouse_rows,
            rows_for_substring=self.rows_for_substring,
        )
        c.calls = self.calls
        return c


class _StubWorkload(DemoWorkload):
    """Workload that succeeds with N reported queries after a tiny delay."""
    key = "stub"
    description = "stub workload"
    estimated_minutes = 0.01

    def __init__(self, *, n_queries: int = 3, fail: bool = False) -> None:
        self.n_queries = n_queries
        self.fail = fail

    def execute(self, client, warehouse_name, *, stop_event):
        if self.fail:
            raise RuntimeError("workload boom")
        r = WorkloadResult(
            workload_key=self.key,
            warehouse_name=warehouse_name,
            started_at=time.time(),
            queries_attempted=self.n_queries,
            queries_succeeded=self.n_queries,
        )
        r.completed_at = time.time()
        return r


# ── render_create_demo_warehouse_sql ──────────────────────────────────────


class TestRenderCreate:
    def test_basic_shape(self):
        spec = DemoWarehouseSpec(
            short_name="WIDGET_WH",
            size="SMALL",
            auto_suspend_seconds=90,
            workload_key="stub",
            expected_finding="x",
        )
        sql = render_create_demo_warehouse_sql(spec)
        # Spot-check the load-bearing bits.
        assert "SNOWTUNER_DEMO_WIDGET_WH" in sql
        assert "WAREHOUSE_SIZE = 'SMALL'" in sql
        assert "AUTO_SUSPEND = 90" in sql
        assert "INITIALLY_SUSPENDED = TRUE" in sql
        assert "IF NOT EXISTS" in sql

    def test_no_em_dashes(self):
        """Demo provisioning SQL is operator-facing.  Use plain dashes."""
        spec = DemoWarehouseSpec(
            short_name="X", size="SMALL", auto_suspend_seconds=60,
            workload_key="stub", expected_finding="x",
        )
        assert "—" not in render_create_demo_warehouse_sql(spec)


# ── preflight ────────────────────────────────────────────────────────────


class TestPreflight:
    def test_ok_when_no_errors(self):
        report = preflight(FakeClient())  # type: ignore[arg-type]
        assert report.ok is True

    def test_reports_missing_create_warehouse(self):
        client = FakeClient(raise_for_substring={
            "CREATE WAREHOUSE": Exception(
                "SQL access control error: 42501 insufficient privileges"
            ),
        })
        report = preflight(client)  # type: ignore[arg-type]
        assert report.ok is False
        assert "CREATE WAREHOUSE ON ACCOUNT" in report.message

    def test_reports_missing_sample_data(self):
        client = FakeClient(raise_for_substring={
            "SNOWFLAKE_SAMPLE_DATA": Exception(
                "SQL access control error: 42501 insufficient privileges"
            ),
        })
        report = preflight(client)  # type: ignore[arg-type]
        assert report.ok is False
        assert "SNOWFLAKE_SAMPLE_DATA" in report.message
        assert "IMPORTED PRIVILEGES" in report.message

    def test_unrelated_error_surfaces_verbatim(self):
        client = FakeClient(raise_for_substring={
            "CREATE WAREHOUSE": Exception("network timeout"),
        })
        report = preflight(client)  # type: ignore[arg-type]
        assert report.ok is False
        # Don't claim "missing grant" when the real error is something else.
        assert "network timeout" in report.message


# ── DB persistence helpers ────────────────────────────────────────────────


class TestPersistence:
    def test_insert_and_finalize_completed(self, duck: duckdb.DuckDBPyConnection):
        run_id = _insert_run(duck, ["SNOWTUNER_DEMO_A"])
        assert run_id >= 1
        _finalize_run(duck, run_id, status="COMPLETED")
        row = duck.execute(
            "SELECT status, completed_at FROM app.demo_runs WHERE id = ?",
            [run_id],
        ).fetchone()
        assert row[0] == "COMPLETED"
        assert row[1] is not None

    def test_update_workload_merges(self, duck: duckdb.DuckDBPyConnection):
        run_id = _insert_run(duck, ["SNOWTUNER_DEMO_A", "SNOWTUNER_DEMO_B"])
        r1 = WorkloadResult(
            workload_key="memory_hog",
            warehouse_name="SNOWTUNER_DEMO_A",
            queries_succeeded=2,
        )
        r2 = WorkloadResult(
            workload_key="overkill",
            warehouse_name="SNOWTUNER_DEMO_B",
            queries_succeeded=100,
        )
        _update_workload(duck, run_id, r1)
        _update_workload(duck, run_id, r2)

        import json
        row = duck.execute(
            "SELECT per_workload FROM app.demo_runs WHERE id = ?", [run_id],
        ).fetchone()
        merged = json.loads(row[0])
        assert set(merged.keys()) == {"memory_hog", "overkill"}
        assert merged["memory_hog"]["queries_succeeded"] == 2
        assert merged["overkill"]["queries_succeeded"] == 100


# ── teardown_demo ────────────────────────────────────────────────────────


class TestTeardown:
    def test_drops_explicit_names(self, duck: duckdb.DuckDBPyConnection):
        client = FakeClient()
        run_id = _insert_run(duck, ["SNOWTUNER_DEMO_X", "SNOWTUNER_DEMO_Y"])
        dropped, errors = teardown_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck,
            names=["SNOWTUNER_DEMO_X", "SNOWTUNER_DEMO_Y"],
            run_id=run_id,
        )
        assert dropped == ["SNOWTUNER_DEMO_X", "SNOWTUNER_DEMO_Y"]
        assert errors == []
        assert any("DROP WAREHOUSE IF EXISTS SNOWTUNER_DEMO_X" in c for c in client.calls)

        # Row marked TORN_DOWN.
        row = duck.execute(
            "SELECT status, torn_down_at FROM app.demo_runs WHERE id = ?",
            [run_id],
        ).fetchone()
        assert row[0] == "TORN_DOWN"
        assert row[1] is not None

    def test_refuses_non_demo_prefix(self, duck: duckdb.DuckDBPyConnection):
        """Critical safety: must NEVER drop a non-SNOWTUNER_DEMO_ warehouse,
        even if the caller passes a name that doesn't have the prefix.
        Production warehouses must be safe from teardown."""
        client = FakeClient()
        dropped, errors = teardown_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck,
            names=["PROD_ETL_WH", "ANALYTICS_WH"],
            run_id=None,
        )
        assert dropped == []
        # No DROP WAREHOUSE call should have been issued.
        assert not any("DROP WAREHOUSE" in c for c in client.calls)

    def test_sweep_uses_show_warehouses(self, duck: duckdb.DuckDBPyConnection):
        """Calling teardown_demo without names triggers a SHOW WAREHOUSES sweep."""
        client = FakeClient(show_warehouse_rows=[
            ("SNOWTUNER_DEMO_A", "XSMALL"),
            ("SNOWTUNER_DEMO_B", "SMALL"),
        ])
        dropped, errors = teardown_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck,
            names=None,
            run_id=None,
        )
        assert set(dropped) == {"SNOWTUNER_DEMO_A", "SNOWTUNER_DEMO_B"}
        assert any("SHOW WAREHOUSES" in c for c in client.calls)

    def test_idempotent_on_empty(self, duck: duckdb.DuckDBPyConnection):
        client = FakeClient(show_warehouse_rows=[])
        dropped, errors = teardown_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck,
            names=None,
            run_id=None,
        )
        assert dropped == []
        assert errors == []


class TestListDemoWarehouses:
    def test_filters_to_prefix(self):
        client = FakeClient(show_warehouse_rows=[
            ("SNOWTUNER_DEMO_A", "XSMALL"),
            ("SNOWTUNER_DEMO_B", "SMALL"),
        ])
        # SHOW WAREHOUSES LIKE 'SNOWTUNER_DEMO_%' is server-side; we trust
        # Snowflake's LIKE.  Our return is whatever Snowflake said.
        names = list_demo_warehouses(client)  # type: ignore[arg-type]
        assert names == ["SNOWTUNER_DEMO_A", "SNOWTUNER_DEMO_B"]


# ── End-to-end run_demo with stub workloads ──────────────────────────────


class TestRunDemo:
    def test_happy_path_completes_and_tears_down(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        # Swap the registry to point every spec at a stub workload so we
        # don't depend on real Snowflake.  Restore on teardown.
        from snowtuner.demo import workloads as wl_module
        stub_registry = {"stub": _StubWorkload(n_queries=5)}
        monkeypatch.setattr(wl_module, "DEMO_WORKLOADS", stub_registry)
        # Also swap the runner's view (it imports a name into its module).
        from snowtuner.demo import runner as runner_module
        monkeypatch.setattr(runner_module, "DEMO_WORKLOADS", stub_registry)

        client = FakeClient(show_warehouse_rows=[
            ("SNOWTUNER_DEMO_TEST", "SMALL"),
        ])

        # Just one spec - faster test, exercises the same code paths.
        specs = [DemoWarehouseSpec(
            short_name="TEST",
            size="SMALL",
            auto_suspend_seconds=60,
            workload_key="stub",
            expected_finding="x",
        )]

        run_id = run_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck,
            specs=specs,
        )

        # Run row is COMPLETED + TORN_DOWN-tracked.  (We mark TORN_DOWN
        # synchronously after teardown, so the final status is TORN_DOWN.)
        row = duck.execute(
            "SELECT status, per_workload FROM app.demo_runs WHERE id = ?",
            [run_id],
        ).fetchone()
        assert row[0] == "TORN_DOWN"
        import json
        per = json.loads(row[1])
        assert per["stub"]["queries_succeeded"] == 5

        # Provisioned + dropped.
        sql = " ".join(client.calls)
        assert "CREATE WAREHOUSE IF NOT EXISTS SNOWTUNER_DEMO_TEST" in sql
        assert "DROP WAREHOUSE IF EXISTS SNOWTUNER_DEMO_TEST" in sql

    def test_skip_teardown_leaves_warehouses_up(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        from snowtuner.demo import workloads as wl_module
        from snowtuner.demo import runner as runner_module
        stub_registry = {"stub": _StubWorkload()}
        monkeypatch.setattr(wl_module, "DEMO_WORKLOADS", stub_registry)
        monkeypatch.setattr(runner_module, "DEMO_WORKLOADS", stub_registry)

        client = FakeClient()
        specs = [DemoWarehouseSpec(
            short_name="KEEP",
            size="SMALL",
            auto_suspend_seconds=60,
            workload_key="stub",
            expected_finding="x",
        )]
        run_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck, specs=specs, skip_teardown=True,
        )
        sql = " ".join(client.calls)
        assert "CREATE WAREHOUSE" in sql
        assert "DROP WAREHOUSE" not in sql

    def test_workload_crash_doesnt_skip_teardown(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        from snowtuner.demo import workloads as wl_module
        from snowtuner.demo import runner as runner_module
        # Workload raises.  The runner must still tear down the warehouse it
        # provisioned - leaking real warehouses is the worst possible outcome.
        stub_registry = {"stub": _StubWorkload(fail=True)}
        monkeypatch.setattr(wl_module, "DEMO_WORKLOADS", stub_registry)
        monkeypatch.setattr(runner_module, "DEMO_WORKLOADS", stub_registry)

        client = FakeClient()
        specs = [DemoWarehouseSpec(
            short_name="OOPS",
            size="SMALL",
            auto_suspend_seconds=60,
            workload_key="stub",
            expected_finding="x",
        )]
        run_id = run_demo(
            client=client,  # type: ignore[arg-type]
            conn=duck, specs=specs,
        )

        sql = " ".join(client.calls)
        assert "DROP WAREHOUSE IF EXISTS SNOWTUNER_DEMO_OOPS" in sql

        row = duck.execute(
            "SELECT status, per_workload FROM app.demo_runs WHERE id = ?",
            [run_id],
        ).fetchone()
        # Status lands at TORN_DOWN (teardown ran).  The workload row
        # captures the failure via WorkloadResult.last_error.
        assert row[0] == "TORN_DOWN"
        import json
        per = json.loads(row[1])
        assert "workload crashed" in (per.get("stub") or {}).get("last_error", "")


class TestLatestStatus:
    def test_returns_none_when_no_runs(self, duck: duckdb.DuckDBPyConnection):
        assert latest_status(duck) is None

    def test_returns_most_recent(self, duck: duckdb.DuckDBPyConnection):
        run1 = _insert_run(duck, ["SNOWTUNER_DEMO_A"])
        run2 = _insert_run(duck, ["SNOWTUNER_DEMO_B"])
        s = latest_status(duck)
        assert s is not None
        assert s.run_id == run2
        assert s.warehouses == ["SNOWTUNER_DEMO_B"]


class TestCostSummary:
    def test_mentions_credits_and_dollars(self):
        out = cost_summary()
        assert "credits" in out.lower()
        assert "$" in out
        assert "—" not in out  # em-dash check


class TestSpecCoverage:
    """Cross-check between the spec list and the runner's defaults."""

    def test_runner_default_is_all_six_specs(self):
        # run_demo defaults to DEMO_SPECS - if the constant is renamed or
        # the default changes silently, this test screams.
        import inspect
        sig = inspect.signature(run_demo)
        assert sig.parameters["specs"].default is demo_warehouses.DEMO_SPECS


# ─────────────────────────────────────────────────────────────────────────
# verify_demo: post-hoc check that ACCOUNT_USAGE shows the expected signal
# ─────────────────────────────────────────────────────────────────────────


def _seed_demo_run(
    duck: duckdb.DuckDBPyConnection, warehouse_names: list[str],
) -> int:
    """Helper: insert a demo_runs row so verify_demo has something to query."""
    return _insert_run(duck, warehouse_names)


class TestVerifyNoRun:
    def test_no_runs_returns_none(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        assert verify_demo(client=FakeClient(), conn=duck) is None  # type: ignore[arg-type]


class TestVerifyMemoryHog:
    """memory_hog passes on (n>=30) AND (any remote spill OR >=20% local).

    The n>=30 gate mirrors the right-sizer's MIN_QUERIES_FOR_READINESS -
    a warehouse below it is skipped entirely, so spill alone is not enough.
    """

    def test_pass_remote_spill(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_MEMORY_HOG_WH"])
        # n=34 (clears gate), 1 remote spill
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_MEMORY_HOG_WH'":
                [(34, 0, 1, 0, 200000, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        assert results is not None
        mh = next(r for r in results if r.workload_key == "memory_hog")
        assert mh.is_pass
        assert "remote spill" in mh.verdict.lower()

    def test_pass_high_local_spill(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_MEMORY_HOG_WH"])
        # n=34, 10 local spills = 29%, no remote
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_MEMORY_HOG_WH'":
                [(34, 10, 0, 0, 200000, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        mh = next(r for r in results if r.workload_key == "memory_hog")
        assert mh.is_pass
        assert "local spill" in mh.verdict.lower()

    def test_fail_no_spill(self, duck: duckdb.DuckDBPyConnection):
        """The 2026-06-08 round-1 failure mode: queries ran but produced
        no spill.  Verdict must be FAIL with the actual observed counts
        so the operator can triage."""
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_MEMORY_HOG_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_MEMORY_HOG_WH'":
                [(34, 0, 0, 0, 100, 0)],   # n clears the gate; zero spill
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        mh = next(r for r in results if r.workload_key == "memory_hog")
        assert not mh.is_pass
        assert "no spill" in mh.verdict.lower()

    def test_fail_below_readiness_gate_even_with_spill(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        """The 2026-06-08 round-2 discovery: MEMORY_HOG had only 11 rows
        in QUERY_HISTORY, below MIN_QUERIES_FOR_READINESS=30, so the
        right-sizer skipped the warehouse entirely - perfect spill would
        still produce zero recommendations.  Verify must FAIL on n, not
        PASS on the spill."""
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_MEMORY_HOG_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_MEMORY_HOG_WH'":
                [(11, 2, 2, 0, 200000, 0)],   # spilling, but n=11 < 30
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        mh = next(r for r in results if r.workload_key == "memory_hog")
        assert not mh.is_pass
        assert "readiness" in mh.verdict.lower() or ">=30" in mh.verdict


class TestVerifyLocalSpill:
    def test_pass_at_threshold(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_LOCAL_SPILL_WH"])
        # n=34 (clears gate), 10 local = 29% > 20% threshold
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_LOCAL_SPILL_WH'":
                [(34, 10, 0, 0, 5000, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        ls = next(r for r in results if r.workload_key == "local_spill")
        assert ls.is_pass

    def test_fail_below_threshold(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_LOCAL_SPILL_WH"])
        # n=34, 3 local = 9%; below 20% threshold
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_LOCAL_SPILL_WH'":
                [(34, 3, 0, 0, 5000, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        ls = next(r for r in results if r.workload_key == "local_spill")
        assert not ls.is_pass

    def test_fail_below_readiness_gate(self, duck: duckdb.DuckDBPyConnection):
        """Production's exact round-1 shape: 27 queries with spill ratio
        above threshold would STILL produce no rec (27 < 30)."""
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_LOCAL_SPILL_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_LOCAL_SPILL_WH'":
                [(27, 9, 0, 0, 5000, 0)],   # 33% spill but n=27 < 30
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        ls = next(r for r in results if r.workload_key == "local_spill")
        assert not ls.is_pass
        assert "readiness" in ls.verdict.lower() or ">=30" in ls.verdict


class TestVerifySaturated:
    def test_pass_queue_above_5s(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_SATURATED_WH"])
        # n=60, avg_queue=30s (above 5s threshold)
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_SATURATED_WH'":
                [(60, 0, 0, 30000, 10000, 75000)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        sat = next(r for r in results if r.workload_key == "saturated")
        assert sat.is_pass

    def test_fail_queue_below_5s(self, duck: duckdb.DuckDBPyConnection):
        """The exact 2026-06-08 production observation: 121 queries,
        max_queue 0.28s, avg ~10ms.  Should FAIL clearly."""
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_SATURATED_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_SATURATED_WH'":
                [(121, 0, 0, 10, 200, 280)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        sat = next(r for r in results if r.workload_key == "saturated")
        assert not sat.is_pass
        assert "0.0s" in sat.verdict or "0.01s" in sat.verdict


class TestVerifyOverkill:
    def test_pass_when_fast_no_spill_no_queue(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_OVERKILL_WH"])
        # n=120, p99=300ms, no spill, no queue - matches the production
        # OVERKILL case that the user reported working correctly.
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_OVERKILL_WH'":
                [(120, 0, 0, 0, 300, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        ov = next(r for r in results if r.workload_key == "overkill")
        assert ov.is_pass

    def test_fail_when_p99_too_high(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_OVERKILL_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_OVERKILL_WH'":
                [(120, 0, 0, 0, 5000, 0)],   # p99=5s, breaks rule 4
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        ov = next(r for r in results if r.workload_key == "overkill")
        assert not ov.is_pass


class TestVerifyHealthy:
    """The control case.  PASS = no recommender rule would trigger."""

    def test_pass_when_no_signal(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_HEALTHY_WH"])
        # n=50, no spill, no queue, p99 well under 1s but n<100 so rule 4
        # won't trigger downsize either.
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_HEALTHY_WH'":
                [(50, 0, 0, 0, 2000, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        hl = next(r for r in results if r.workload_key == "healthy")
        assert hl.is_pass

    def test_fail_when_unexpectedly_triggers(self, duck: duckdb.DuckDBPyConnection):
        """If HEALTHY starts producing spill, something's wrong with the
        cooked workload or the warehouse sizing."""
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_HEALTHY_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_HEALTHY_WH'":
                [(50, 0, 5, 0, 2000, 0)],   # 5 remote spills - shouldn't happen
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        hl = next(r for r in results if r.workload_key == "healthy")
        assert not hl.is_pass


class TestVerifyBursty:
    """bursty checks WAREHOUSE_EVENTS_HISTORY, not QUERY_HISTORY."""

    def test_pass_with_ten_cycles(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_BURSTY_WH"])
        # 10 suspends, 10 resumes = 10 complete cycles
        client = FakeClient(rows_for_substring={
            "WAREHOUSE_EVENTS_HISTORY": [(10, 10)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        br = next(r for r in results if r.workload_key == "bursty")
        assert br.is_pass

    def test_fail_with_no_events_calls_out_lag(self, duck: duckdb.DuckDBPyConnection):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_BURSTY_WH"])
        client = FakeClient(rows_for_substring={
            "WAREHOUSE_EVENTS_HISTORY": [(0, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        br = next(r for r in results if r.workload_key == "bursty")
        assert not br.is_pass
        # Must explicitly mention lag so the user knows to retry later.
        assert "lag" in br.verdict.lower() or "caught up" in br.verdict.lower()


class TestVerifyZeroQueriesCallsOutLag:
    """When ACCOUNT_USAGE returns 0 queries (full lag scenario), the
    verdict must say so explicitly so the user understands to retry."""

    def test_zero_queries_mentions_account_usage_lag(
        self, duck: duckdb.DuckDBPyConnection,
    ):
        from snowtuner.demo.runner import verify_demo
        _seed_demo_run(duck, ["SNOWTUNER_DEMO_MEMORY_HOG_WH"])
        client = FakeClient(rows_for_substring={
            "warehouse_name = 'SNOWTUNER_DEMO_MEMORY_HOG_WH'":
                [(0, 0, 0, 0, 0, 0)],
        })
        results = verify_demo(client=client, conn=duck)  # type: ignore[arg-type]
        mh = next(r for r in results if r.workload_key == "memory_hog")
        assert not mh.is_pass
        assert "caught up" in mh.verdict.lower() or "lag" in mh.verdict.lower()
