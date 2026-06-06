"""Shared pytest fixtures for the snowtuner test suite.

Two fixture families:

  * **DuckDB**: ``duck`` is a fresh in-memory connection with the canonical
    schema applied (via ``init_schema``).  Tests that need pre-populated
    data layer on top.
  * **API**: ``api_client`` is a FastAPI ``TestClient`` wired to its own
    in-memory DuckDB so every test starts from a known-empty state.  Auth
    middleware is short-circuited to 'none' mode (loopback-only is
    automatic for TestClient).

Sample-data helpers (``seed_recommendations``, ``seed_experiment_runs``)
take the connection and return the rows they wrote so tests can assert
on specific IDs without re-querying.

Fixtures avoid touching ``~/.snowtuner/`` — credentials, audit archives,
the on-disk DB file — so the suite is isolated from operator state.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

import duckdb
import pytest

from snowtuner.storage.schema import init_schema


# ── DuckDB fixtures ──────────────────────────────────────────────


@pytest.fixture
def duck() -> Iterator[duckdb.DuckDBPyConnection]:
    """An in-memory DuckDB connection with snowtuner's schema applied.

    Fresh per test — fixtures shouldn't leak state across tests, and an
    in-memory connection is cheap enough that it's not worth caching.
    """
    conn = duckdb.connect(":memory:")
    init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


# ── API fixtures ─────────────────────────────────────────────────


@pytest.fixture
def api_client(monkeypatch, tmp_path) -> Iterator[Any]:
    """FastAPI TestClient with an isolated DuckDB.

    The app uses ``snowtuner.storage.db.get_connection()`` everywhere; we
    monkeypatch the underlying ``db_path`` to a fresh tmp_path so each
    test gets its own DB file.  Auth is forced to 'none' mode (the
    TestClient always reports loopback).

    Yields the TestClient directly — tests do
    ``api_client.post('/recommendations/1/accept', json={...})``.
    """
    from fastapi.testclient import TestClient

    # Force a per-test DB path so the singleton in storage.db doesn't
    # leak state.  We also close any existing global connection before
    # the test starts (paranoia — pytest-xdist may have left one).
    from snowtuner.storage import db as storage_db
    monkeypatch.setattr(storage_db, "db_path", lambda: tmp_path / "test.duckdb")
    storage_db.close_connection()

    # Force auth off for the test surface.  TestClient is loopback so
    # 'none' mode authorizes everything; we explicitly set the env so
    # tests don't depend on the user's shell environment.
    monkeypatch.setenv("SNOWTUNER_AUTH_MODE", "none")
    monkeypatch.setenv("SNOWTUNER_AUTOMATION_INTERVAL", "0")  # never start the loop

    from snowtuner.api.app import create_app
    app = create_app()
    with TestClient(app) as client:
        yield client

    # Cleanup
    storage_db.close_connection()


# ── Sample data factories ────────────────────────────────────────


@pytest.fixture
def seed_warehouses(duck: duckdb.DuckDBPyConnection) -> list[dict]:
    """Insert a small set of warehouses covering the cases the recommenders
    care about: a Gen1, a Gen2, one with multi-cluster.  Returns the
    rows for direct lookup.
    """
    warehouses = [
        {
            "name": "ETL_WH", "size": "LARGE",
            "min_cluster_count": 1, "max_cluster_count": 1,
            "auto_suspend_seconds": 60, "auto_resume": True,
            "generation": "1", "qas_state": "off",
        },
        {
            "name": "BI_WH", "size": "MEDIUM",
            "min_cluster_count": 2, "max_cluster_count": 5,
            "auto_suspend_seconds": 60, "auto_resume": True,
            "generation": "2", "qas_state": "on",
        },
    ]
    for w in warehouses:
        duck.execute(
            """
            INSERT INTO raw.warehouses
              (name, size, min_cluster_count, max_cluster_count,
               auto_suspend_seconds, auto_resume, generation, qas_state)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                w["name"], w["size"], w["min_cluster_count"], w["max_cluster_count"],
                w["auto_suspend_seconds"], w["auto_resume"],
                w["generation"], w["qas_state"],
            ],
        )
    return warehouses


@pytest.fixture
def make_run(duck: duckdb.DuckDBPyConnection):
    """Factory for ExperimentRun rows.  Returns a callable that inserts
    one and returns the inserted dict.

    Defaults give a successful run with realistic timing; override any
    field via kwargs::

        make_run(experiment_id=1, arm_name='control', elapsed_ms=1500)
        make_run(experiment_id=1, arm_name='gen2',    elapsed_ms=900)
    """
    rep_counter = {"v": 0}

    def _make(
        *,
        experiment_id: int = 1,
        arm_name: str = "control",
        sampled_query_id: str | None = None,
        rep_index: int | None = None,
        elapsed_ms: int | None = 1000,
        credits_used_estimate: float | None = 0.01,
        status: str = "success",
        bytes_scanned: int | None = 1024,
        error_message: str | None = None,
    ) -> dict:
        if rep_index is None:
            rep_index = rep_counter["v"]
            rep_counter["v"] += 1
        if sampled_query_id is None:
            sampled_query_id = f"q-{rep_index}"
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        duck.execute(
            """
            INSERT INTO app.experiment_runs
              (experiment_id, arm_name, rep_index, sampled_query_id,
               parameterized_hash, replay_query_id, elapsed_ms,
               queued_overload_ms, bytes_scanned, bytes_spilled_local,
               bytes_spilled_remote, credits_used_estimate, status,
               error_message, started_at, completed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                experiment_id, arm_name, rep_index, sampled_query_id,
                "hash-" + sampled_query_id, f"replay-{sampled_query_id}-{arm_name}",
                elapsed_ms,
                0, bytes_scanned, 0, 0, credits_used_estimate,
                status, error_message,
                now - timedelta(seconds=elapsed_ms / 1000 if elapsed_ms else 0),
                now,
            ],
        )
        return {
            "experiment_id": experiment_id,
            "arm_name": arm_name,
            "rep_index": rep_index,
            "sampled_query_id": sampled_query_id,
            "elapsed_ms": elapsed_ms,
            "credits_used_estimate": credits_used_estimate,
            "status": status,
        }

    return _make


@pytest.fixture
def seed_query_history(duck: duckdb.DuckDBPyConnection):
    """Factory for raw.query_history rows.  Returns a callable that
    inserts a batch of synthetic queries against a warehouse.

    Useful for testing the recommenders' candidate-scoring logic
    (Gen2CandidateFinder, QASCandidateFinder).
    """
    def _make(
        warehouse: str,
        *,
        n: int = 100,
        elapsed_ms: int = 2000,
        execution_ms: int | None = None,
        bytes_scanned: int = 1024,
        bytes_spilled_to_local: int = 0,
        bytes_spilled_to_remote: int = 0,
        queued_overload_ms: int = 0,
    ) -> int:
        """Insert ``n`` queries with the given per-query metrics.  Returns
        the count inserted.
        """
        if execution_ms is None:
            execution_ms = int(elapsed_ms * 0.85)
        base = datetime.now(timezone.utc).replace(tzinfo=None)
        for i in range(n):
            duck.execute(
                """
                INSERT INTO raw.query_history
                  (query_id, query_text, query_type, execution_status,
                   user_name, warehouse_name, warehouse_size,
                   start_time, end_time, total_elapsed_ms, execution_ms,
                   queued_overload_ms, bytes_scanned, bytes_spilled_to_local,
                   bytes_spilled_to_remote, query_parameterized_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    f"{warehouse}-q-{i}", f"select {i}", "SELECT", "SUCCESS",
                    "test_user", warehouse, "LARGE",
                    base - timedelta(minutes=i),
                    base - timedelta(minutes=i) + timedelta(milliseconds=elapsed_ms),
                    elapsed_ms, execution_ms, queued_overload_ms,
                    bytes_scanned, bytes_spilled_to_local, bytes_spilled_to_remote,
                    f"hash-{i % 10}",  # 10 distinct query families
                ],
            )
        return n

    return _make
