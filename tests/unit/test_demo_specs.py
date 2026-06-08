"""Unit tests for the demo data layer: warehouse specs and workload registry.

These tests guard the wiring between the specs and the workload registry -
if a spec points at a workload key that doesn't exist, ``snowtuner demo
seed`` fails at runtime with a confusing KeyError.  Catching it here keeps
the bug from reaching a real-Snowflake invocation.

We also pin the safety properties that matter operationally:
  - Every demo warehouse starts with the SNOWTUNER_DEMO_ prefix so teardown
    can filter by name without false matches.
  - Every spec's auto_suspend is bounded (no infinite-idle warehouses).
  - The workload registry round-trips: every spec has a matching workload.
"""
from __future__ import annotations

import duckdb
import pytest

from snowtuner.demo import (
    DEMO_SPECS,
    DEMO_WAREHOUSE_PREFIX,
    DEMO_WORKLOADS,
    DemoWarehouseSpec,
)
from snowtuner.demo.warehouses import find_spec


class TestDemoSpecs:
    def test_six_specs(self):
        """The design promises exactly 6 cooked warehouses.  If this fails
        the README, docs, and CLI help text all need updating."""
        assert len(DEMO_SPECS) == 6

    def test_all_use_demo_prefix(self):
        """Teardown filters by name prefix.  A spec missing the prefix
        would leak a warehouse that teardown can't find."""
        for spec in DEMO_SPECS:
            assert spec.warehouse_name.startswith(DEMO_WAREHOUSE_PREFIX), (
                f"{spec.short_name} doesn't expand to a SNOWTUNER_DEMO_-prefixed name"
            )

    def test_short_names_are_unique(self):
        names = [s.short_name for s in DEMO_SPECS]
        assert len(names) == len(set(names)), f"duplicate short_name in {names}"

    def test_auto_suspend_bounded(self):
        """Cap at 120s across the board so a crashed run can't burn many
        credits between auto-suspend cycles.  If this fails, someone added
        a long-AUTO_SUSPEND spec without thinking about blast radius."""
        for spec in DEMO_SPECS:
            assert 30 <= spec.auto_suspend_seconds <= 120, (
                f"{spec.short_name}: auto_suspend={spec.auto_suspend_seconds} "
                f"outside [30, 120]"
            )

    def test_every_spec_has_workload(self):
        """A spec referencing an unknown workload key is a runtime time bomb."""
        for spec in DEMO_SPECS:
            assert spec.workload_key in DEMO_WORKLOADS, (
                f"{spec.short_name}: workload_key={spec.workload_key!r} "
                f"not in registry"
            )

    def test_every_workload_used(self):
        """Catch dead workloads - if a workload isn't referenced by any
        spec, it's unreachable from the CLI.  Either wire it up or delete it."""
        used = {s.workload_key for s in DEMO_SPECS}
        for key in DEMO_WORKLOADS:
            assert key in used, f"workload {key!r} has no spec"

    def test_expected_finding_nonempty(self):
        """Used by `snowtuner demo status` to tell the user what to expect."""
        for spec in DEMO_SPECS:
            assert spec.expected_finding.strip(), (
                f"{spec.short_name}: expected_finding is empty"
            )

    def test_no_em_dashes_in_user_facing_strings(self):
        """All customer-facing copy uses regular dashes (project policy)."""
        for spec in DEMO_SPECS:
            assert "—" not in spec.expected_finding, (
                f"{spec.short_name}: expected_finding contains em-dash"
            )
        for w in DEMO_WORKLOADS.values():
            assert "—" not in w.description, (
                f"workload {w.key}: description contains em-dash"
            )


class TestFindSpec:
    def test_finds_by_exact_short_name(self):
        spec = find_spec("MEMORY_HOG_WH")
        assert spec is not None
        assert spec.short_name == "MEMORY_HOG_WH"

    def test_case_insensitive(self):
        assert find_spec("memory_hog_wh") is not None

    def test_unknown_returns_none(self):
        assert find_spec("DOES_NOT_EXIST") is None


class TestWorkloadProperties:
    """Quick sanity on the workload registry.  Doesn't execute queries."""

    def test_all_keys_match_field(self):
        """``DEMO_WORKLOADS`` dict key must equal the workload's ``.key``."""
        for key, workload in DEMO_WORKLOADS.items():
            assert workload.key == key, (
                f"registry key {key!r} doesn't match workload.key={workload.key!r}"
            )

    def test_estimated_minutes_reasonable(self):
        """Catch typos like 320 minutes (probably meant 32)."""
        for w in DEMO_WORKLOADS.values():
            assert 0 < w.estimated_minutes <= 60, (
                f"{w.key}: estimated_minutes={w.estimated_minutes} outside (0, 60]"
            )


class TestDemoRunsSchema:
    """The schema migration for app.demo_runs must apply cleanly and the
    expected columns must be queryable.  Catches typos in the DDL."""

    def test_table_exists_after_init(self, duck: duckdb.DuckDBPyConnection):
        # duck fixture already calls init_schema.
        rows = duck.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'app' AND table_name = 'demo_runs'"
        ).fetchall()
        assert rows == [("demo_runs",)]

    def test_can_insert_and_read_row(self, duck: duckdb.DuckDBPyConnection):
        import json
        warehouses = ["SNOWTUNER_DEMO_MEMORY_HOG_WH", "SNOWTUNER_DEMO_HEALTHY_WH"]
        per_workload = {"memory_hog": {"queries_succeeded": 2}}
        duck.execute(
            """
            INSERT INTO app.demo_runs (status, warehouses, per_workload, notes)
            VALUES ('RUNNING', ?, ?, ?)
            """,
            [json.dumps(warehouses), json.dumps(per_workload), "test run"],
        )
        row = duck.execute(
            "SELECT status, warehouses, per_workload, notes FROM app.demo_runs"
        ).fetchone()
        assert row[0] == "RUNNING"
        # DuckDB JSON columns round-trip as JSON strings.
        assert json.loads(row[1]) == warehouses
        assert json.loads(row[2]) == per_workload
        assert row[3] == "test run"

    def test_sequence_auto_assigns_id(self, duck: duckdb.DuckDBPyConnection):
        import json
        for _ in range(3):
            duck.execute(
                "INSERT INTO app.demo_runs (status, warehouses) VALUES ('RUNNING', ?)",
                [json.dumps([])],
            )
        ids = [r[0] for r in duck.execute(
            "SELECT id FROM app.demo_runs ORDER BY id"
        ).fetchall()]
        assert ids == sorted(set(ids))
        assert len(ids) == 3


class TestDemoWarehouseSpec:
    def test_warehouse_name_is_prefixed(self):
        spec = DemoWarehouseSpec(
            short_name="EXAMPLE_WH",
            size="SMALL",
            auto_suspend_seconds=60,
            workload_key="memory_hog",
            expected_finding="x",
        )
        assert spec.warehouse_name == "SNOWTUNER_DEMO_EXAMPLE_WH"

    def test_immutable(self):
        """frozen=True - prevents accidental mutation by the runner."""
        spec = DEMO_SPECS[0]
        with pytest.raises((AttributeError, TypeError)):
            spec.size = "XLARGE"  # type: ignore[misc]
