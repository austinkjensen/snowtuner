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
    ) -> None:
        self.calls: list[str] = []
        self.raise_for_substring = raise_for_substring or {}
        self.show_warehouse_rows = show_warehouse_rows or []
        self.credentials = "fake-creds"  # what clone() copies

    def execute(self, sql: str, params: list | None = None) -> list[tuple]:
        self.calls.append(sql)
        for substr, exc in self.raise_for_substring.items():
            if substr.lower() in sql.lower():
                raise exc
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
