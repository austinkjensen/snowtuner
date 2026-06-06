"""Unit tests for ``snowtuner.experiments.cost_estimate``.

Cost estimation feeds two things:
  1. The experiment's PROPOSED-time budget shown to the user.
  2. The hard cost cap that aborts a runaway experiment.

Both consumers need the math to be correct under realistic inputs.
"""
from __future__ import annotations

from snowtuner.experiments.cost_estimate import (
    QueryStats,
    estimate_experiment_cost,
)


def _stats(p50: float, mean: float | None = None) -> QueryStats:
    """Build a QueryStats with sensible defaults — mean ≈ p50 unless overridden."""
    return QueryStats(
        query_id=f"q-{p50}",
        p50_elapsed_ms=p50,
        mean_elapsed_ms=mean if mean is not None else p50,
        bytes_scanned=1024,
    )


class TestBasicMath:
    """Sanity checks: more queries / longer queries / more arms / more reps
    all scale credit estimates the right direction."""

    def test_higher_credit_rate_scales_high_end(self):
        sample = [_stats(2000) for _ in range(10)]
        low_cr = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"control": 1.0, "arm": 1.0},
            reps_per_arm=3,
        )
        high_cr = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"control": 1.0, "arm": 4.0},
            reps_per_arm=3,
        )
        # Doubling-plus the arm's rate must monotonically grow the estimate.
        assert high_cr.high_credits > low_cr.high_credits
        assert high_cr.low_credits >= low_cr.low_credits

    def test_more_reps_scales_credits(self):
        sample = [_stats(2000) for _ in range(10)]
        c1 = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=1,
        )
        c5 = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=5,
        )
        assert c5.high_credits > c1.high_credits

    def test_more_queries_scales_credits(self):
        few = [_stats(2000) for _ in range(5)]
        many = [_stats(2000) for _ in range(50)]
        c_few = estimate_experiment_cost(
            sample_query_stats=few,
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=3,
        )
        c_many = estimate_experiment_cost(
            sample_query_stats=many,
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=3,
        )
        assert c_many.high_credits > c_few.high_credits

    def test_low_le_high(self):
        sample = [_stats(2000) for _ in range(10)]
        est = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.5},
            reps_per_arm=3,
        )
        assert est.low_credits <= est.high_credits


class TestEdgeCases:
    def test_empty_sample_returns_zero_or_overhead_only(self):
        # No queries to replay → cost is at most the per-arm overhead.
        est = estimate_experiment_cost(
            sample_query_stats=[],
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=3,
        )
        # Per-arm overhead is small but nonzero (resume + metric polling);
        # acceptable to return a tiny number rather than strictly 0.
        assert est.low_credits >= 0
        assert est.high_credits >= 0
        assert est.high_credits < 0.5  # well under 1 credit total

    def test_single_arm_still_works(self):
        # Edge case: 1-arm "experiment" — degenerate but shouldn't crash.
        sample = [_stats(1000) for _ in range(5)]
        est = estimate_experiment_cost(
            sample_query_stats=sample,
            arm_credit_rates_per_hour={"only": 1.0},
            reps_per_arm=1,
        )
        assert est.high_credits > 0

    def test_rationale_populated(self):
        # The rationale string is shown to users — must always be present.
        est = estimate_experiment_cost(
            sample_query_stats=[_stats(1000)],
            arm_credit_rates_per_hour={"a": 1.0, "b": 1.0},
            reps_per_arm=3,
        )
        assert est.rationale
        assert isinstance(est.rationale, str)
