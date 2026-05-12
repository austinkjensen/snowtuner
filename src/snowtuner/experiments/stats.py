"""Statistical aggregation: per-(arm, query) replay rows → ArmObservations.

The core operation is a paired t-test on (control, arm) elapsed-time and
credits-per-query deltas, with Bonferroni correction across the number of
non-control arms × metrics tested (2 metrics × N arms).

We use ``scipy.stats.ttest_rel`` for the t-test and the t-distribution for
the confidence-interval critical value.  No tricks; everything is documented
in ``ExperimentReport.statistical_corrections_applied`` and ``.assumptions``.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from scipy import stats as sp_stats

from snowtuner.experiments.model import (
    ArmObservation,
    ExperimentReport,
    ExperimentRun,
    RunStatus,
)


# Default tolerance for "no unacceptable p95 latency regression."  A winning
# arm must not be worse than +10% on p95 elapsed compared to control,
# expressed as a fraction of control p95.
DEFAULT_LATENCY_REGRESSION_TOLERANCE = 0.10

# Two-sided 95% confidence interval.
_CI_CONFIDENCE = 0.95


def aggregate(
    *,
    experiment_id: int,
    runs: list[ExperimentRun],
    control_arm_name: str,
    non_control_arms: list[str],
    objective: str = "minimize_credits_no_latency_regression",
    latency_regression_tolerance: float = DEFAULT_LATENCY_REGRESSION_TOLERANCE,
    annual_query_count_low: float | None = None,
    annual_query_count_high: float | None = None,
) -> ExperimentReport:
    """Build the full ``ExperimentReport`` from raw run rows.

    Parameters
    ----------
    runs
        All run rows for this experiment, including the control.  Rows with
        status != SUCCESS are excluded from aggregation but counted in
        ``excluded_query_count``.
    control_arm_name
        Which arm is the baseline.  Always "control" today, but parameterized
        for the case where a recipe overrides.
    non_control_arms
        Names of arms to compare *against* control.  The Bonferroni
        correction divides by ``len(non_control_arms) * 2``.
    annual_query_count_*
        Optional bounds for projecting annual savings.  If absent, the
        savings projection fields are left None and ``assumptions`` notes
        that projection requires historical query counts.
    """
    # Index runs by (arm_name, sampled_query_id, rep_index).  We need to
    # pair them up control-vs-arm at the same (query, rep).
    by_arm: dict[str, dict[tuple[str, int], ExperimentRun]] = defaultdict(dict)
    excluded = 0
    sample_size_warnings: list[str] = []
    for r in runs:
        if r.status == RunStatus.SUCCESS and r.elapsed_ms is not None:
            by_arm[r.arm_name][(r.sampled_query_id, r.rep_index)] = r
        else:
            excluded += 1

    control_rows = by_arm.get(control_arm_name, {})
    if not control_rows:
        # No successful control runs — can't compute anything.
        return ExperimentReport(
            experiment_id=experiment_id,
            arms=[],
            best_arm_name=None,
            best_arm_rationale="experiment failed: no successful control runs",
            best_arm_objective=objective,
            excluded_query_count=excluded,
            sample_size_warnings=["control arm produced no successful runs"],
            assumptions=_default_assumptions(),
        )

    arm_observations: list[ArmObservation] = []
    correction_factor = max(1, len(non_control_arms) * 2)  # 2 metrics per arm

    for arm_name in non_control_arms:
        arm_rows = by_arm.get(arm_name, {})
        elapsed_pairs: list[tuple[float, float]] = []
        credits_pairs: list[tuple[float, float]] = []
        failed_count = 0
        for key, ctrl in control_rows.items():
            if key not in arm_rows:
                failed_count += 1  # treat missing-in-arm as a failure
                continue
            a = arm_rows[key]
            if a.elapsed_ms is None or ctrl.elapsed_ms is None:
                continue
            elapsed_pairs.append((float(ctrl.elapsed_ms), float(a.elapsed_ms)))
            if (
                a.credits_used_estimate is not None
                and ctrl.credits_used_estimate is not None
            ):
                credits_pairs.append((
                    float(ctrl.credits_used_estimate),
                    float(a.credits_used_estimate),
                ))

        n = len(elapsed_pairs)
        if n < 5:
            sample_size_warnings.append(
                f"arm {arm_name!r} has only {n} paired observations; "
                f"results are not statistically reliable"
            )

        if n == 0:
            arm_observations.append(ArmObservation(
                arm_name=arm_name,
                n_queries_run=0,
                n_queries_failed=failed_count,
                n_queries_excluded=0,
                elapsed_ms_delta_mean=0.0,
                elapsed_ms_delta_p50=0.0,
                elapsed_ms_delta_p95=0.0,
                elapsed_ms_delta_ci_low=0.0,
                elapsed_ms_delta_ci_high=0.0,
                credits_per_query_delta_mean=0.0,
                credits_per_query_delta_ci_low=0.0,
                credits_per_query_delta_ci_high=0.0,
            ))
            continue

        # Paired deltas: arm - control (negative = arm faster/cheaper).
        elapsed_deltas = np.array([a - c for c, a in elapsed_pairs])
        elapsed_mean = float(elapsed_deltas.mean())
        elapsed_p50 = float(np.median(elapsed_deltas))
        elapsed_p95 = float(np.percentile(elapsed_deltas, 95))
        e_ci_low, e_ci_high = _ci(elapsed_deltas)
        e_p = _paired_p_value(elapsed_deltas) if n >= 2 else None

        if credits_pairs:
            credits_deltas = np.array([a - c for c, a in credits_pairs])
            c_mean = float(credits_deltas.mean())
            c_ci_low, c_ci_high = _ci(credits_deltas)
            c_p = _paired_p_value(credits_deltas) if len(credits_deltas) >= 2 else None
        else:
            c_mean = 0.0
            c_ci_low = 0.0
            c_ci_high = 0.0
            c_p = None

        arm_observations.append(ArmObservation(
            arm_name=arm_name,
            n_queries_run=n,
            n_queries_failed=failed_count,
            n_queries_excluded=0,
            elapsed_ms_delta_mean=elapsed_mean,
            elapsed_ms_delta_p50=elapsed_p50,
            elapsed_ms_delta_p95=elapsed_p95,
            elapsed_ms_delta_ci_low=e_ci_low,
            elapsed_ms_delta_ci_high=e_ci_high,
            credits_per_query_delta_mean=c_mean,
            credits_per_query_delta_ci_low=c_ci_low,
            credits_per_query_delta_ci_high=c_ci_high,
            elapsed_p_value_corrected=(
                min(1.0, e_p * correction_factor) if e_p is not None else None
            ),
            credits_p_value_corrected=(
                min(1.0, c_p * correction_factor) if c_p is not None else None
            ),
        ))

    # Determine best arm via the requested objective.
    best = _pick_best_arm(
        arm_observations,
        objective=objective,
        latency_regression_tolerance=latency_regression_tolerance,
        control_p95_elapsed=_control_p95_elapsed(control_rows),
    )

    # Annual savings projection.
    annual_savings_lo: float | None = None
    annual_savings_hi: float | None = None
    p95_pct_lo: float | None = None
    p95_pct_hi: float | None = None
    if best is not None and annual_query_count_low is not None and annual_query_count_high is not None:
        # Pick the absolute credit delta per query for the best arm.
        best_obs = next(o for o in arm_observations if o.arm_name == best.name)
        # Per-query credit delta: negative = savings.  Convert to savings (positive number).
        per_query_savings = -best_obs.credits_per_query_delta_mean
        annual_savings_lo = per_query_savings * annual_query_count_low
        annual_savings_hi = per_query_savings * annual_query_count_high
        # Control p95 from raw rows (not pair-based — overall p95).
        ctrl_p95 = _control_p95_elapsed(control_rows)
        if ctrl_p95 > 0:
            p95_pct_lo = 100.0 * best_obs.elapsed_ms_delta_ci_low / ctrl_p95
            p95_pct_hi = 100.0 * best_obs.elapsed_ms_delta_ci_high / ctrl_p95

    return ExperimentReport(
        experiment_id=experiment_id,
        arms=arm_observations,
        best_arm_name=best.name if best else None,
        best_arm_rationale=best.rationale if best else None,
        best_arm_objective=objective,
        projected_annual_savings_low_credits=annual_savings_lo,
        projected_annual_savings_high_credits=annual_savings_hi,
        projected_p95_latency_delta_pct_low=p95_pct_lo,
        projected_p95_latency_delta_pct_high=p95_pct_hi,
        sample_size_warnings=sample_size_warnings,
        excluded_query_count=excluded,
        statistical_corrections_applied=[
            f"Bonferroni: p-values multiplied by {correction_factor} "
            f"({len(non_control_arms)} non-control arms × 2 metrics)"
        ],
        assumptions=_default_assumptions(),
    )


# ── internals ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _BestArm:
    name: str
    rationale: str


def _pick_best_arm(
    observations: list[ArmObservation],
    *,
    objective: str,
    latency_regression_tolerance: float,
    control_p95_elapsed: float,
) -> _BestArm | None:
    """Default objective: minimize credits subject to p95 latency not
    regressing more than ``latency_regression_tolerance`` of control.

    Returns None if no arm beats control on credits AND meets the latency
    constraint with statistical confidence (CI upper bound < 0).
    """
    if objective != "minimize_credits_no_latency_regression":
        # v0.2 only ships one objective.  Hook for future objectives.
        return None

    eligible: list[tuple[ArmObservation, float]] = []
    for obs in observations:
        if obs.n_queries_run == 0:
            continue
        # Credits CI upper bound must be < 0 to claim "this arm saves credits."
        if obs.credits_per_query_delta_ci_high >= 0:
            continue
        # Latency regression: p95 elapsed delta must not exceed the tolerance.
        if control_p95_elapsed > 0:
            p95_pct_increase = obs.elapsed_ms_delta_p95 / control_p95_elapsed
            if p95_pct_increase > latency_regression_tolerance:
                continue
        eligible.append((obs, obs.credits_per_query_delta_mean))

    if not eligible:
        return None
    eligible.sort(key=lambda x: x[1])  # most negative (most savings) first
    winner, mean_savings = eligible[0]
    rationale = (
        f"arm {winner.arm_name!r} saves an estimated "
        f"{-mean_savings:.4f} credits per query (95% CI: "
        f"{-winner.credits_per_query_delta_ci_high:.4f} to "
        f"{-winner.credits_per_query_delta_ci_low:.4f}) and stays within "
        f"+{latency_regression_tolerance*100:.0f}% p95 latency of control"
    )
    return _BestArm(name=winner.arm_name, rationale=rationale)


def _ci(deltas: np.ndarray) -> tuple[float, float]:
    """Two-sided 95% CI on the mean of paired deltas using the t-distribution."""
    n = len(deltas)
    if n < 2:
        return (float(deltas.mean()), float(deltas.mean()))
    m = float(deltas.mean())
    se = float(deltas.std(ddof=1)) / np.sqrt(n)
    t_crit = float(sp_stats.t.ppf((1 + _CI_CONFIDENCE) / 2, df=n - 1))
    return (m - t_crit * se, m + t_crit * se)


def _paired_p_value(deltas: np.ndarray) -> float:
    """Two-sided p-value for H0: mean delta = 0 against H1: mean delta != 0.

    Equivalent to ``ttest_rel`` of arm vs control because we already paired
    them.  ``ttest_1samp`` on the delta series is the canonical form.
    """
    if len(deltas) < 2:
        return 1.0
    result = sp_stats.ttest_1samp(deltas, 0.0)
    p = float(result.pvalue)
    if np.isnan(p):
        # Happens when all deltas are identical (zero variance).
        return 1.0 if deltas.mean() == 0 else 0.0
    return p


def _control_p95_elapsed(control_rows: dict) -> float:
    if not control_rows:
        return 0.0
    elapsed = np.array([
        r.elapsed_ms for r in control_rows.values()
        if r.elapsed_ms is not None
    ])
    if len(elapsed) == 0:
        return 0.0
    return float(np.percentile(elapsed, 95))


def _default_assumptions() -> list[str]:
    return [
        "Replays use cleared result cache; local warehouse disk cache warms "
        "during the run, biasing absolute latency lower than production but "
        "applies symmetrically across arms.",
        "Credits per query are allocated from WAREHOUSE_METERING_HISTORY "
        "proportionally to elapsed time; this approximates Snowflake's "
        "credit-billing model but is not the exact billed amount.",
        "Annual savings projections assume the sampled query mix is "
        "representative of forward-looking workload and that query volume "
        "stays within the provided bounds.",
        "P-values are Bonferroni-corrected for multiple comparisons across "
        "arms and metrics; corrected p < 0.05 indicates a real effect at "
        "the family-wise 5% error rate.",
    ]
