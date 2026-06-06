"""Unit tests for ``snowtuner.experiments.stats.aggregate``.

The aggregate function is the most-load-bearing piece of the experiments
framework — it turns per-(arm, query, rep) raw observations into the
ArmObservation / ExperimentReport that the UI shows and the autonomous
runner reads.  Regressions here would silently produce wrong
recommendations.

Coverage focus:
  * Happy path: known-better arm wins, correct delta + CI
  * Edge case: no successful control runs → safe failure
  * Edge case: control wins (no arm beats it)
  * Statistical correction: Bonferroni multiplies p-value
  * Excluded runs counted but not aggregated
  * Benchmark mode: Pareto frontier marking
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snowtuner.experiments.model import (
    ArmObservation,
    ExperimentKind,
    ExperimentRun,
    RunStatus,
)
from snowtuner.experiments.stats import aggregate


def _run(
    *,
    arm: str,
    query: str,
    rep: int = 0,
    elapsed_ms: int | None = 1000,
    credits: float | None = 0.01,
    status: RunStatus = RunStatus.SUCCESS,
) -> ExperimentRun:
    """Construct a minimal ExperimentRun for tests.  Keeping the helper
    inline (vs in conftest) because every test in this file uses it and
    the shape is small."""
    return ExperimentRun(
        experiment_id=1,
        arm_name=arm,
        rep_index=rep,
        sampled_query_id=query,
        elapsed_ms=elapsed_ms,
        credits_used_estimate=credits,
        status=status,
        started_at=datetime.now(timezone.utc).replace(tzinfo=None),
        completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )


# ── Happy path ──────────────────────────────────────────────────


class TestKnownWinner:
    """An arm that's consistently faster than control should be picked."""

    def test_aggregate_emits_correct_per_arm_means(self):
        # Control = 1000ms across 10 queries; gen2 = 500ms.
        runs = []
        for i in range(10):
            qid = f"q-{i}"
            runs.append(_run(arm="control", query=qid, elapsed_ms=1000, credits=0.02))
            runs.append(_run(arm="gen2", query=qid, elapsed_ms=500, credits=0.01))

        report = aggregate(
            experiment_id=1,
            runs=runs,
            control_arm_name="control",
            non_control_arms=["gen2"],
        )

        # Report doesn't include control as an arm in TUNING mode — only
        # non-control arms.  This is a deliberate choice in the codebase.
        assert len(report.arms) == 1
        gen2 = report.arms[0]
        assert gen2.arm_name == "gen2"
        assert gen2.n_queries_run == 10
        assert gen2.n_queries_failed == 0
        assert gen2.elapsed_ms_mean == pytest.approx(500, rel=1e-6)
        # Paired delta = arm - control = 500 - 1000 = -500ms (gen2 faster)
        assert gen2.elapsed_ms_delta_mean == pytest.approx(-500, rel=1e-6)
        # Credits delta likewise negative (savings)
        assert gen2.credits_per_query_delta_mean == pytest.approx(-0.01, rel=1e-6)

    def test_clear_winner_is_picked(self):
        # Set up such that gen2 wins with statistical confidence.  Need
        # variation in the deltas so the t-test has signal — perfect
        # uniformity has zero variance and produces NaN p-values.
        runs = []
        for i in range(20):
            ctrl_ms = 1000 + (i % 5) * 50  # noisy around 1100
            arm_ms = ctrl_ms - 400 - (i % 3) * 20  # gen2 consistently faster
            qid = f"q-{i}"
            runs.append(_run(arm="control", query=qid, elapsed_ms=ctrl_ms, credits=0.02))
            runs.append(_run(arm="gen2", query=qid, elapsed_ms=arm_ms, credits=0.01))

        report = aggregate(
            experiment_id=1,
            runs=runs,
            control_arm_name="control",
            non_control_arms=["gen2"],
        )
        assert report.best_arm_name == "gen2"
        assert report.best_arm_rationale is not None
        assert "gen2" in report.best_arm_rationale


# ── No control = early-exit ─────────────────────────────────────


class TestNoSuccessfulControl:
    """If the control arm has zero SUCCESS runs, aggregate must short-circuit
    and not produce a phantom best-arm.  This was the live bug we just hit
    with replay.py — control runs existed but elapsed_ms=None, so they were
    excluded, leaving no usable control data."""

    def test_zero_runs(self):
        runs = [
            _run(arm="gen2", query="q-1", elapsed_ms=500),
            _run(arm="gen2", query="q-2", elapsed_ms=500),
        ]
        report = aggregate(
            experiment_id=1,
            runs=runs,
            control_arm_name="control",
            non_control_arms=["gen2"],
        )
        assert report.best_arm_name is None
        assert "control arm produced no successful runs" in (report.sample_size_warnings or [""])[0]
        # Arms should be empty; no phantom observation.
        assert report.arms == []

    def test_elapsed_ms_none_excluded(self):
        """SUCCESS runs with elapsed_ms=None are excluded from aggregation —
        this is what produced the 'no successful runs' bug pre-fix."""
        runs = [
            # All SUCCESS but no metrics → all excluded
            _run(arm="control", query="q-1", elapsed_ms=None),
            _run(arm="control", query="q-2", elapsed_ms=None),
            _run(arm="gen2", query="q-1", elapsed_ms=None),
            _run(arm="gen2", query="q-2", elapsed_ms=None),
        ]
        report = aggregate(
            experiment_id=1,
            runs=runs,
            control_arm_name="control",
            non_control_arms=["gen2"],
        )
        assert report.best_arm_name is None
        assert report.excluded_query_count == 4  # all four rows excluded
        assert "control arm produced no successful runs" in (report.sample_size_warnings or [""])[0]


# ── Control wins = no best arm ──────────────────────────────────


class TestControlWins:
    """If no arm beats control on credits with confidence, best_arm_name
    must be None.  The win rule: CI upper bound for credits delta must be
    strictly < 0."""

    def test_arm_slower_than_control_not_picked(self):
        runs = []
        for i in range(20):
            qid = f"q-{i}"
            # gen2 strictly slower → positive credits delta → fails win rule
            runs.append(_run(arm="control", query=qid, elapsed_ms=500, credits=0.01))
            runs.append(_run(arm="gen2", query=qid, elapsed_ms=1000, credits=0.02))

        report = aggregate(
            experiment_id=1,
            runs=runs,
            control_arm_name="control",
            non_control_arms=["gen2"],
        )
        assert report.best_arm_name is None
        # But the observation row IS produced — just not selected
        assert len(report.arms) == 1
        assert report.arms[0].elapsed_ms_delta_mean > 0  # slower


# ── Bonferroni correction ───────────────────────────────────────


class TestBonferroni:
    """Multiple arms = stricter p-value threshold.  With N arms × 2 metrics
    being tested, p-values are multiplied by 2N before comparison."""

    def test_two_arms_doubles_correction_vs_one(self):
        # Use the same single-arm data but with a second (irrelevant) arm
        # added.  The second arm's presence should make the first arm's
        # corrected p-value HIGHER (less significant) than it would be
        # in a single-arm experiment.
        runs_one_arm = []
        runs_two_arm = []
        for i in range(20):
            ctrl_ms = 1000 + (i % 5) * 50
            arm_ms = ctrl_ms - 200
            qid = f"q-{i}"
            ctrl = _run(arm="control", query=qid, elapsed_ms=ctrl_ms, credits=0.02)
            armA = _run(arm="armA", query=qid, elapsed_ms=arm_ms, credits=0.015)
            armB = _run(arm="armB", query=qid, elapsed_ms=arm_ms, credits=0.015)
            runs_one_arm.extend([ctrl, armA])
            runs_two_arm.extend([ctrl, armA, armB])

        r1 = aggregate(
            experiment_id=1, runs=runs_one_arm,
            control_arm_name="control", non_control_arms=["armA"],
        )
        r2 = aggregate(
            experiment_id=1, runs=runs_two_arm,
            control_arm_name="control", non_control_arms=["armA", "armB"],
        )
        # armA's p-value should be larger (less significant) in the 2-arm
        # experiment due to Bonferroni multiplying by 4 vs 2.
        p1 = next(a for a in r1.arms if a.arm_name == "armA").elapsed_p_value_corrected
        p2 = next(a for a in r2.arms if a.arm_name == "armA").elapsed_p_value_corrected
        assert p1 is not None and p2 is not None
        assert p2 >= p1  # 4x correction ≥ 2x correction

    def test_correction_capped_at_one(self):
        """Bonferroni can push a p-value past 1.0; the code should cap it."""
        runs = []
        for i in range(20):
            qid = f"q-{i}"
            # Identical means produce a borderline p-value that × many
            # arms would exceed 1.  Real-world likely.
            runs.append(_run(arm="control", query=qid, elapsed_ms=1000 + (i % 3) * 5))
            runs.append(_run(arm="x", query=qid, elapsed_ms=999 + (i % 3) * 5))

        report = aggregate(
            experiment_id=1, runs=runs,
            control_arm_name="control", non_control_arms=["x"],
        )
        p = report.arms[0].elapsed_p_value_corrected
        assert p is not None
        assert 0 <= p <= 1.0


# ── Excluded counts ─────────────────────────────────────────────


class TestExcludedCount:
    """FAILED and metrics-less SUCCESS runs should be counted in
    excluded_query_count but not affect the per-arm aggregates."""

    def test_failed_runs_excluded(self):
        runs = []
        for i in range(5):
            qid = f"q-{i}"
            runs.append(_run(arm="control", query=qid, elapsed_ms=1000))
            runs.append(_run(arm="gen2", query=qid, elapsed_ms=500))
        # Add 3 failed runs
        runs.append(_run(arm="control", query="bad-1", status=RunStatus.FAILED, elapsed_ms=None))
        runs.append(_run(arm="gen2", query="bad-1", status=RunStatus.FAILED, elapsed_ms=None))
        runs.append(_run(arm="gen2", query="bad-2", status=RunStatus.EXCLUDED, elapsed_ms=None))

        report = aggregate(
            experiment_id=1, runs=runs,
            control_arm_name="control", non_control_arms=["gen2"],
        )
        assert report.excluded_query_count == 3
        # The 5 SUCCESS runs per arm still aggregate correctly
        assert report.arms[0].n_queries_run == 5


# ── Benchmark mode ──────────────────────────────────────────────


class TestBenchmarkMode:
    """BENCHMARK experiments use Pareto-frontier marking; control is
    optional; the report includes every arm including control."""

    def test_benchmark_with_no_control_marks_pareto(self):
        # Three arms: A (cheap+fast), B (cheap+slow), C (expensive+fast).
        # A dominates B (same credits, faster), so B is NOT pareto-optimal.
        # A vs C: A is cheaper, C is faster → both pareto-optimal.
        runs = []
        for i in range(10):
            qid = f"q-{i}"
            runs.append(_run(arm="A", query=qid, elapsed_ms=500, credits=0.01))
            runs.append(_run(arm="B", query=qid, elapsed_ms=1000, credits=0.01))
            runs.append(_run(arm="C", query=qid, elapsed_ms=300, credits=0.05))

        report = aggregate(
            experiment_id=1, runs=runs,
            control_arm_name=None,
            non_control_arms=["A", "B", "C"],
            kind=ExperimentKind.BENCHMARK,
        )
        by_name = {a.arm_name: a for a in report.arms}
        assert by_name["A"].is_pareto_optimal is True
        assert by_name["B"].is_pareto_optimal is False
        assert by_name["C"].is_pareto_optimal is True

    def test_benchmark_picks_cheapest_frontier_arm(self):
        # A is cheapest AND on the frontier → should be "best" by the
        # benchmark heuristic.
        runs = []
        for i in range(10):
            qid = f"q-{i}"
            runs.append(_run(arm="A", query=qid, elapsed_ms=500, credits=0.01))
            runs.append(_run(arm="C", query=qid, elapsed_ms=300, credits=0.05))

        report = aggregate(
            experiment_id=1, runs=runs,
            control_arm_name=None,
            non_control_arms=["A", "C"],
            kind=ExperimentKind.BENCHMARK,
        )
        assert report.best_arm_name == "A"


# ── Sample-size warnings ────────────────────────────────────────


class TestSampleSizeWarnings:
    """Arms with too few paired observations should produce a warning so
    operators don't trust low-power results."""

    def test_warning_when_under_5_paired(self):
        runs = []
        for i in range(3):  # only 3 queries — under the 5 threshold
            qid = f"q-{i}"
            runs.append(_run(arm="control", query=qid, elapsed_ms=1000))
            runs.append(_run(arm="x", query=qid, elapsed_ms=900))

        report = aggregate(
            experiment_id=1, runs=runs,
            control_arm_name="control", non_control_arms=["x"],
        )
        assert any("not statistically reliable" in w for w in report.sample_size_warnings)
