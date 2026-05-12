"""Cost estimation for experiment runs.

Two distinct numbers per experiment:

  1. **Cost to run the experiment itself** — narrow range, hard cap, used by
     the engine to abort mid-run if the high end is approached.
  2. **Projected annual savings if the winning arm is applied** — wide range
     with explicit assumptions, surfaced in the UI but never used as a
     control signal.

Both are produced at proposal time; the engine tracks actual spend against
(1) and updates the report's annual-savings projection at completion.
"""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field


class CostEstimate(BaseModel):
    """Estimated cost of running a proposed experiment, and the savings
    projection if it succeeds.  Frozen at proposal time."""

    # ── Experiment cost (narrow, hard-capped) ───────────────────────────
    low_credits: float
    high_credits: float
    rationale: str  # human-readable derivation, e.g.
                    # "30 queries × 3 reps × 4 arms; assumes p50 historical
                    #  elapsed holds; 20% overhead for suspend/resume cycles"

    # ── Annual-savings projection if winning arm wins ──────────────────
    # Wide range, explicit assumptions.  Both None = not estimated.
    projected_annual_savings_low_credits: float | None = None
    projected_annual_savings_high_credits: float | None = None
    projected_annual_savings_assumptions: list[str] = Field(default_factory=list)

    # ── Performance projection ─────────────────────────────────────────
    projected_p50_elapsed_delta_pct_low: float | None = None
    projected_p50_elapsed_delta_pct_high: float | None = None


@dataclass
class QueryStats:
    """Historical statistics for a single sampled query, used to estimate
    its share of experiment cost.  Pulled from QUERY_HISTORY at proposal time."""
    query_id: str
    p50_elapsed_ms: float
    mean_elapsed_ms: float
    bytes_scanned: int | None


# Per-arm fixed overhead — suspend/resume cycles for cache control + the time
# the test warehouse spends idle but provisioned.  Conservative estimate;
# refined empirically from completed-experiment data later.
_PER_ARM_OVERHEAD_SECONDS = 30.0
_VARIANCE_FACTOR_LOW = 0.7
_VARIANCE_FACTOR_HIGH = 1.5


def estimate_experiment_cost(
    *,
    sample_query_stats: list[QueryStats],
    arm_credit_rates_per_hour: dict[str, float],   # arm_name → credits/hr
    reps_per_arm: int,
    annual_workload_credits: float | None = None,  # last-N-day extrapolation
    target_credit_delta_pct_low: float | None = None,   # winning-arm hypothesis
    target_credit_delta_pct_high: float | None = None,
) -> CostEstimate:
    """Estimate experiment cost from historical stats + arm credit rates.

    Algorithm:
      total_arm_seconds = (sum of historical mean elapsed) × reps_per_arm
                          + per_arm_overhead_seconds
      arm_cost = total_arm_seconds / 3600 × credit_rate
      total = sum across arms

    Variance bands: low/high multipliers reflect uncertainty in elapsed-time
    estimates (queries can be slower under load, etc.).
    """
    total_query_seconds = sum(q.mean_elapsed_ms / 1000.0 for q in sample_query_stats)

    arm_cost_low = 0.0
    arm_cost_high = 0.0
    arm_cost_lines: list[str] = []
    for arm_name, credits_per_hour in arm_credit_rates_per_hour.items():
        arm_seconds = (total_query_seconds * reps_per_arm) + _PER_ARM_OVERHEAD_SECONDS
        arm_credits_typical = (arm_seconds / 3600.0) * credits_per_hour
        arm_cost_low += arm_credits_typical * _VARIANCE_FACTOR_LOW
        arm_cost_high += arm_credits_typical * _VARIANCE_FACTOR_HIGH
        arm_cost_lines.append(
            f"  {arm_name}: ~{arm_seconds:.0f}s @ {credits_per_hour}cr/hr "
            f"≈ {arm_credits_typical:.3f} credits"
        )

    rationale = (
        f"{len(sample_query_stats)} sample queries × {reps_per_arm} reps × "
        f"{len(arm_credit_rates_per_hour)} arms.  "
        f"Per-arm overhead {_PER_ARM_OVERHEAD_SECONDS:.0f}s for cache-clearing "
        f"suspend/resume cycles.  Variance band ±50% covers query elapsed-time "
        f"jitter under varying load.\n"
        + "\n".join(arm_cost_lines)
    )

    projected_low: float | None = None
    projected_high: float | None = None
    assumptions: list[str] = []
    if (
        annual_workload_credits is not None
        and target_credit_delta_pct_low is not None
        and target_credit_delta_pct_high is not None
    ):
        projected_low = annual_workload_credits * target_credit_delta_pct_low
        projected_high = annual_workload_credits * target_credit_delta_pct_high
        assumptions = [
            "last-14-day workload is representative of next 12 months",
            "winning-arm credit delta observed on sample queries holds across "
            "the warehouse's full traffic",
            "credit-rate ratio between arms is stable",
        ]

    return CostEstimate(
        low_credits=round(arm_cost_low, 3),
        high_credits=round(arm_cost_high, 3),
        rationale=rationale,
        projected_annual_savings_low_credits=projected_low,
        projected_annual_savings_high_credits=projected_high,
        projected_annual_savings_assumptions=assumptions,
    )
