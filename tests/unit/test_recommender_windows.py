"""Tests for the configurable recommender consideration windows.

Two layers:

* Resolver mechanics (``recommenders/windows.py``): precedence between
  per-recommender env, global env, and built-in defaults; the 0=unbounded
  sentinel; clamping; junk-value fallthrough; the reserved per-warehouse
  env warning.
* Behavior: the windows actually bound what the recommenders read - the
  right-sizer's gate ignores rows older than the window, and the survival
  tuner's fit excludes old gaps (with 0 restoring the unbounded behavior).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import duckdb

from snowtuner.recommenders.windows import (
    DEFAULT_WINDOW_DAYS,
    FALLBACK_WINDOW_DAYS,
    MAX_WINDOW_DAYS,
    UNBOUNDED,
    WindowResolution,
    resolve_window_days,
)


class TestResolutionPrecedence:
    def test_builtin_default_when_no_env(self, monkeypatch):
        monkeypatch.delenv("SNOWTUNER_WINDOW_DAYS", raising=False)
        win = resolve_window_days("rule_based_right_sizer")
        assert win.days == DEFAULT_WINDOW_DAYS["rule_based_right_sizer"]
        assert win.source == "default"

    def test_survival_default_is_30(self, monkeypatch):
        """The survival tuner's bounded-by-default behavior is deliberate -
        its pre-2026-06 unbounded window made proposals drift as history
        accumulated.  30 days, not 14: low-traffic warehouses need more
        runway to accumulate >=10 modelable gaps."""
        monkeypatch.delenv("SNOWTUNER_WINDOW_DAYS", raising=False)
        win = resolve_window_days("auto_suspend_survival_tuner")
        assert win.days == 30

    def test_unknown_recommender_gets_fallback(self):
        win = resolve_window_days("some_future_recommender")
        assert win.days == FALLBACK_WINDOW_DAYS
        assert win.source == "default"

    def test_global_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "21")
        win = resolve_window_days("rule_based_right_sizer")
        assert win.days == 21
        assert win.source == "env:SNOWTUNER_WINDOW_DAYS"

    def test_per_recommender_env_beats_global(self, monkeypatch):
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "21")
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER", "7")
        win = resolve_window_days("rule_based_right_sizer")
        assert win.days == 7
        assert win.source == "env:SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER"
        # Other recommenders still see the global.
        other = resolve_window_days("qas_candidate_finder")
        assert other.days == 21

    def test_zero_means_unbounded(self, monkeypatch):
        monkeypatch.setenv(
            "SNOWTUNER_WINDOW_DAYS__AUTO_SUSPEND_SURVIVAL_TUNER", "0",
        )
        win = resolve_window_days("auto_suspend_survival_tuner")
        assert win.days == UNBOUNDED
        assert win.is_unbounded

    def test_junk_value_falls_through_to_next_level(self, monkeypatch, caplog):
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER", "soon")
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "21")
        with caplog.at_level(logging.WARNING):
            win = resolve_window_days("rule_based_right_sizer")
        assert win.days == 21  # fell through to the global
        assert any("not an integer" in r.message for r in caplog.records)

    def test_negative_value_falls_through(self, monkeypatch, caplog):
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "-5")
        with caplog.at_level(logging.WARNING):
            win = resolve_window_days("rule_based_right_sizer")
        assert win.days == DEFAULT_WINDOW_DAYS["rule_based_right_sizer"]
        assert any("negative" in r.message for r in caplog.records)

    def test_huge_value_clamps(self, monkeypatch, caplog):
        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "99999")
        with caplog.at_level(logging.WARNING):
            win = resolve_window_days("rule_based_right_sizer")
        assert win.days == MAX_WINDOW_DAYS


class TestReservedPerWarehouseHatch:
    """The per-(warehouse, recommender) env shape is reserved.  Setting one
    must WARN (discoverable) and not change the result (not yet applied)."""

    def test_reserved_env_warns_and_is_ignored(self, monkeypatch, caplog):
        monkeypatch.setenv(
            "SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER__ETL_WH", "60",
        )
        with caplog.at_level(logging.WARNING):
            win = resolve_window_days(
                "rule_based_right_sizer", warehouse_name="ETL_WH",
            )
        assert win.days == DEFAULT_WINDOW_DAYS["rule_based_right_sizer"]
        assert any("reserved" in r.message.lower() for r in caplog.records)

    def test_warehouse_name_kwarg_accepted_and_inert(self):
        a = resolve_window_days("rule_based_right_sizer")
        b = resolve_window_days("rule_based_right_sizer", warehouse_name="X")
        assert a == b


class TestWindowResolutionHelpers:
    def test_sql_filter_bounded(self):
        win = WindowResolution(days=14, source="default")
        assert win.sql_filter("start_time") == (
            "start_time >= now() - INTERVAL 14 DAY"
        )

    def test_sql_filter_unbounded_is_true(self):
        win = WindowResolution(days=0, source="default")
        assert win.sql_filter("start_time") == "TRUE"

    def test_describe(self):
        assert WindowResolution(14, "default").describe() == "the last 14 days"
        assert WindowResolution(0, "default").describe() == "all ingested history"

    def test_weekly_factor_bounded(self):
        win = WindowResolution(days=14, source="default")
        assert win.weekly_factor(None) == 0.5  # 7/14

    def test_weekly_factor_unbounded_uses_span(self):
        win = WindowResolution(days=0, source="default")
        assert win.weekly_factor(70.0) == 0.1  # 7/70
        # Span floor of one day - a single burst doesn't extrapolate to inf.
        assert win.weekly_factor(0.0) == 7.0
        assert win.weekly_factor(None) == 7.0


# ── Behavioral: the windows actually bound the reads ─────────────────────


def _insert_query_rows(
    duck: duckdb.DuckDBPyConnection,
    warehouse: str,
    *,
    n: int,
    age_days: float,
) -> None:
    base = datetime.now() - timedelta(days=age_days)
    for i in range(n):
        start = base + timedelta(seconds=i * 60)
        duck.execute(
            """
            INSERT INTO raw.query_history
              (query_id, query_text, query_type, execution_status,
               warehouse_name, start_time, end_time,
               total_elapsed_ms, execution_ms,
               queued_overload_ms, bytes_spilled_to_local, bytes_spilled_to_remote)
            VALUES (?, 'select 1', 'SELECT', 'SUCCESS', ?, ?, ?, 2000, 1700, 0, 0, 0)
            """,
            [f"{warehouse}-{age_days}-{i}", warehouse,
             start, start + timedelta(seconds=2)],
        )


class TestRightSizerGateRespectsWindow:
    def test_old_rows_age_out_under_short_window(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        from snowtuner.recommenders.builtins.rule_based_right_sizer import (
            RuleBasedRightSizerGate,
        )
        # 40 rows, all 10 days old: ready under the 14d default,
        # not ready under a 2-day window.
        _insert_query_rows(duck, "OLD_WH", n=40, age_days=10)

        monkeypatch.delenv("SNOWTUNER_WINDOW_DAYS", raising=False)
        assert RuleBasedRightSizerGate().evaluate(duck).is_ready

        monkeypatch.setenv("SNOWTUNER_WINDOW_DAYS", "2")
        report = RuleBasedRightSizerGate().evaluate(duck)
        assert not report.is_ready
        assert report.signals.get("window_days") == 2


class TestSurvivalWindowBehavior:
    def _seed_gaps(
        self, duck: duckdb.DuckDBPyConnection, *, n: int, age_days: float,
    ) -> None:
        base = datetime.now() - timedelta(days=age_days)
        for i in range(n):
            gap_start = base + timedelta(minutes=10 * i)
            duck.execute(
                """
                INSERT INTO features.warehouse_idle_gaps
                  (warehouse_name, gap_start, gap_end, idle_seconds)
                VALUES ('STALE_WH', ?, ?, 180.0)
                """,
                [gap_start, gap_start + timedelta(seconds=180)],
            )

    def test_old_gaps_excluded_by_default_window(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        """Gaps older than the 30d default no longer drive the fit - the
        regime-change drift the unbounded version suffered from."""
        from snowtuner.recommenders.builtins.auto_suspend_survival import (
            AutoSuspendSurvivalTuner,
        )
        monkeypatch.delenv(
            "SNOWTUNER_WINDOW_DAYS__AUTO_SUSPEND_SURVIVAL_TUNER", raising=False,
        )
        monkeypatch.delenv("SNOWTUNER_WINDOW_DAYS", raising=False)
        self._seed_gaps(duck, n=15, age_days=90)  # stale regime
        state = AutoSuspendSurvivalTuner().fit(duck)
        assert "STALE_WH" not in state["per_warehouse"]
        assert state["window_days"] == 30

    def test_zero_restores_unbounded(
        self, duck: duckdb.DuckDBPyConnection, monkeypatch,
    ):
        from snowtuner.recommenders.builtins.auto_suspend_survival import (
            AutoSuspendSurvivalTuner,
        )
        monkeypatch.setenv(
            "SNOWTUNER_WINDOW_DAYS__AUTO_SUSPEND_SURVIVAL_TUNER", "0",
        )
        self._seed_gaps(duck, n=15, age_days=90)
        state = AutoSuspendSurvivalTuner().fit(duck)
        assert "STALE_WH" in state["per_warehouse"]
        assert state["per_warehouse"]["STALE_WH"]["n"] == 15
        assert state["window_days"] == UNBOUNDED
