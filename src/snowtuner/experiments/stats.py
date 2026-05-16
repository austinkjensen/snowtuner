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
    ExperimentKind,
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
    control_arm_name: str | None,
    non_control_arms: list[str],
    kind: ExperimentKind = ExperimentKind.TUNING,
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

    # Resolve which arms to compute stats for.  For tuning we *require* a
    # control; for benchmark control is optional.
    control_rows = by_arm.get(control_arm_name, {}) if control_arm_name else {}
    if kind == ExperimentKind.TUNING and not control_rows:
        # No successful control runs — can't compute paired stats.
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

    # The arms we'll report on.  For tuning that's the non-control arms;
    # for benchmark it's every arm (including any designated control, since
    # the report is a comparison across all arms).
    arms_to_observe = list(non_control_arms)
    if kind == ExperimentKind.BENCHMARK and control_arm_name:
        # Include the designated reference arm so the report shows its
        # absolute stats too.
        arms_to_observe = [control_arm_name, *non_control_arms]

    arm_observations: list[ArmObservation] = []
    # Bonferroni correction is computed across paired tests only — irrelevant
    # for arms with no control.  When there *is* a control we test 2 metrics
    # × N non-control arms.
    correction_factor = max(1, len(non_control_arms) * 2) if control_rows else 1

    for arm_name in arms_to_observe:
        arm_rows = by_arm.get(arm_name, {})

        # ── Absolute stats: this arm's own elapsed + credits, no pairing ──
        abs_elapsed = np.array(
            [r.elapsed_ms for r in arm_rows.values() if r.elapsed_ms is not None],
            dtype=float,
        )
        abs_credits = np.array(
            [r.credits_used_estimate for r in arm_rows.values()
             if r.credits_used_estimate is not None],
            dtype=float,
        )
        abs_elapsed_mean = float(abs_elapsed.mean()) if abs_elapsed.size else 0.0
        abs_elapsed_p50 = float(np.percentile(abs_elapsed, 50)) if abs_elapsed.size else 0.0
        abs_elapsed_p95 = float(np.percentile(abs_elapsed, 95)) if abs_elapsed.size else 0.0
        abs_credits_mean = float(abs_credits.mean()) if abs_credits.size else 0.0

        # ── Paired-delta stats: only meaningful when this arm != control AND
        # there's a control to pair against ─────────────────────────────────
        elapsed_pairs: list[tuple[float, float]] = []
        credits_pairs: list[tuple[float, float]] = []
        failed_count = 0
        is_control = (arm_name == control_arm_name)

        if control_rows and not is_control:
            for key, ctrl in control_rows.items():
                if key not in arm_rows:
                    failed_count += 1
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

        n = len(elapsed_pairs) if not is_control else len(arm_rows)
        if not is_control and n < 5:
            sample_size_warnings.append(
                f"arm {arm_name!r} has only {n} paired observations; "
                f"results are not statistically reliable"
            )

        # Default delta fields to zero (used when no control or arm is control).
        elapsed_delta_mean = 0.0
        elapsed_delta_p50 = 0.0
        elapsed_delta_p95 = 0.0
        e_ci_low = 0.0
        e_ci_high = 0.0
        c_delta_mean = 0.0
        c_ci_low = 0.0
        c_ci_high = 0.0
        e_p_corrected: float | None = None
        c_p_corrected: float | None = None

        if elapsed_pairs:
            elapsed_deltas = np.array([a - c for c, a in elapsed_pairs])
            elapsed_delta_mean = float(elapsed_deltas.mean())
            elapsed_delta_p50 = float(np.median(elapsed_deltas))
            elapsed_delta_p95 = float(np.percentile(elapsed_deltas, 95))
            e_ci_low, e_ci_high = _ci(elapsed_deltas)
            e_p = _paired_p_value(elapsed_deltas) if len(elapsed_deltas) >= 2 else None
            if e_p is not None:
                e_p_corrected = min(1.0, e_p * correction_factor)

        if credits_pairs:
            credits_deltas = np.array([a - c for c, a in credits_pairs])
            c_delta_mean = float(credits_deltas.mean())
            c_ci_low, c_ci_high = _ci(credits_deltas)
            c_p = _paired_p_value(credits_deltas) if len(credits_deltas) >= 2 else None
            if c_p is not None:
                c_p_corrected = min(1.0, c_p * correction_factor)

        arm_observations.append(ArmObservation(
            arm_name=arm_name,
            n_queries_run=len(arm_rows),
            n_queries_failed=failed_count,
            n_queries_excluded=0,
            elapsed_ms_mean=abs_elapsed_mean,
            elapsed_ms_p50=abs_elapsed_p50,
            elapsed_ms_p95=abs_elapsed_p95,
            credits_per_query_mean=abs_credits_mean,
            elapsed_ms_delta_mean=elapsed_delta_mean,
            elapsed_ms_delta_p50=elapsed_delta_p50,
            elapsed_ms_delta_p95=elapsed_delta_p95,
            elapsed_ms_delta_ci_low=e_ci_low,
            elapsed_ms_delta_ci_high=e_ci_high,
            credits_per_query_delta_mean=c_delta_mean,
            credits_per_query_delta_ci_low=c_ci_low,
            credits_per_query_delta_ci_high=c_ci_high,
            elapsed_p_value_corrected=e_p_corrected,
            credits_p_value_corrected=c_p_corrected,
        ))

    # Determine best arm — branches by kind.
    if kind == ExperimentKind.TUNING:
        best = _pick_best_arm_tuning(
            arm_observations,
            objective=objective,
            latency_regression_tolerance=latency_regression_tolerance,
            control_p95_elapsed=_control_p95_elapsed(control_rows),
        )
    else:
        # Mark Pareto-optimal arms in-place and pick a single "best" arm by
        # the lowest-credit-on-Pareto-frontier heuristic.
        _mark_pareto_optimal(arm_observations)
        best = _pick_best_arm_benchmark(arm_observations)

    # Annual savings projection — tuning only.  For benchmark there's no
    # baseline against which to compute "savings"; the report surfaces the
    # Pareto frontier instead.
    annual_savings_lo: float | None = None
    annual_savings_hi: float | None = None
    p95_pct_lo: float | None = None
    p95_pct_hi: float | None = None
    if (
        kind == ExperimentKind.TUNING
        and best is not None
        and annual_query_count_low is not None
        and annual_query_count_high is not None
    ):
        best_obs = next(o for o in arm_observations if o.arm_name == best.name)
        # Per-query credit delta: negative = savings.  Convert to savings (positive number).
        per_query_savings = -best_obs.credits_per_query_delta_mean
        annual_savings_lo = per_query_savings * annual_query_count_low
        annual_savings_hi = per_query_savings * annual_query_count_high
        ctrl_p95 = _control_p95_elapsed(control_rows)
        if ctrl_p95 > 0:
            p95_pct_lo = 100.0 * best_obs.elapsed_ms_delta_ci_low / ctrl_p95
            p95_pct_hi = 100.0 * best_obs.elapsed_ms_delta_ci_high / ctrl_p95

    corrections: list[str] = []
    if control_rows:
        corrections.append(
            f"Bonferroni: p-values multiplied by {correction_factor} "
            f"({len(non_control_arms)} non-control arms × 2 metrics)"
        )
    if kind == ExperimentKind.BENCHMARK:
        corrections.append(
            "Pareto frontier: arms on the frontier are non-dominated on "
            "(credits_per_query_mean, elapsed_ms_p95).  Single 'best' arm "
            "is the cheapest on the frontier; full frontier is exposed via "
            "ArmObservation.is_pareto_optimal."
        )

    return ExperimentReport(
        experiment_id=experiment_id,
        arms=arm_observations,
        best_arm_name=best.name if best else None,
        best_arm_rationale=best.rationale if best else None,
        best_arm_objective=(
            objective if kind == ExperimentKind.TUNING
            else "pareto_minimize_credits_then_p95"
        ),
        projected_annual_savings_low_credits=annual_savings_lo,
        projected_annual_savings_high_credits=annual_savings_hi,
        projected_p95_latency_delta_pct_low=p95_pct_lo,
        projected_p95_latency_delta_pct_high=p95_pct_hi,
        sample_size_warnings=sample_size_warnings,
        excluded_query_count=excluded,
        statistical_corrections_applied=corrections,
        assumptions=_default_assumptions(),
    )


# ── internals ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _BestArm:
    name: str
    rationale: str


def _pick_best_arm_tuning(
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


def _mark_pareto_optimal(observations: list[ArmObservation]) -> None:
    """Mark each arm as Pareto-optimal on (credits_per_query_mean, elapsed_ms_p95).

    Arm A *dominates* arm B if A is strictly better on at least one metric
    and not worse on the other.  An arm is Pareto-optimal if no other arm
    dominates it.  The frontier is the set of optimal arms — they represent
    the meaningful trade-offs (any non-frontier arm is strictly worse than
    something on the frontier).

    Mutates ``observations`` in place.
    """
    runnable = [o for o in observations if o.n_queries_run > 0]
    for obs in runnable:
        dominated = False
        for other in runnable:
            if other.arm_name == obs.arm_name:
                continue
            # other dominates obs iff:
            #   other.credits <= obs.credits AND other.p95 <= obs.p95
            #   AND (other.credits < obs.credits OR other.p95 < obs.p95)
            other_no_worse = (
                other.credits_per_query_mean <= obs.credits_per_query_mean
                and other.elapsed_ms_p95 <= obs.elapsed_ms_p95
            )
            other_strictly_better = (
                other.credits_per_query_mean < obs.credits_per_query_mean
                or other.elapsed_ms_p95 < obs.elapsed_ms_p95
            )
            if other_no_worse and other_strictly_better:
                dominated = True
                break
        obs.is_pareto_optimal = not dominated


def _pick_best_arm_benchmark(
    observations: list[ArmObservation],
) -> _BestArm | None:
    """For benchmark: pick the lowest-credit arm on the Pareto frontier.

    No "this saves vs control" gate — every Pareto-optimal arm is a
    legitimate choice depending on the user's credits-vs-latency preference.
    We surface a single "best" suggestion by tie-breaking on credits
    (cheapest wins), but the report shows the full frontier so the user
    can pick differently.
    """
    frontier = [o for o in observations if o.is_pareto_optimal and o.n_queries_run > 0]
    if not frontier:
        return None
    frontier.sort(key=lambda o: (o.credits_per_query_mean, o.elapsed_ms_p95))
    winner = frontier[0]
    other_optimal = [o.arm_name for o in frontier if o.arm_name != winner.arm_name]
    if other_optimal:
        rationale = (
            f"arm {winner.arm_name!r} is the cheapest configuration on the "
            f"Pareto frontier ({winner.credits_per_query_mean:.5f} credits/query, "
            f"p95 {winner.elapsed_ms_p95:.0f}ms).  Other frontier configurations "
            f"trade more credits for lower p95: {', '.join(other_optimal)}."
        )
    else:
        rationale = (
            f"arm {winner.arm_name!r} dominates all others on both credits "
            f"and p95 elapsed ({winner.credits_per_query_mean:.5f} credits/query, "
            f"p95 {winner.elapsed_ms_p95:.0f}ms)."
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
