"""Preset recipes — convenience constructors for common experiment shapes.

Each recipe is a function that:

  - takes a ``WarehouseConfig`` (the candidate target warehouse) and an
    ``AccountInfo``,
  - returns a fully-built ``ProposedExperiment``, or
  - returns ``None`` if the recipe is ineligible for this warehouse/account
    (e.g. proposing Gen1→Gen2 on an already-Gen2 warehouse).

Recommenders import these and call them when proposing experiments.  The UI
exposes them as one-click "Add experiment" templates.

Adding a new recipe is a single function added to this module + an entry in
``PRESET_RECIPES``.
"""
from __future__ import annotations

from collections.abc import Callable

from snowtuner.experiments.arm import Arm
from snowtuner.experiments.axes import Generation, QASState
from snowtuner.experiments.config_delta import WarehouseConfig, WarehouseConfigDelta
from snowtuner.experiments.cost_estimate import CostEstimate, QueryStats, estimate_experiment_cost
from snowtuner.experiments.eligibility import AccountInfo, check_arm_eligibility
from snowtuner.experiments.model import ProposedExperiment
from snowtuner.recommenders.sizes import credit_rate, step

# Default knobs used by every recipe; recipes that need to deviate set their own.
_DEFAULT_SAMPLE_SIZE = 30  # 6 families × 5 queries
_DEFAULT_REPS_PER_ARM = 3


Recipe = Callable[
    [WarehouseConfig, AccountInfo, list[QueryStats] | None],
    "ProposedExperiment | None",
]


# ── gen1_to_gen2 ─────────────────────────────────────────────────────────

def gen1_to_gen2(
    warehouse: WarehouseConfig,
    account: AccountInfo,
    sample_query_stats: list[QueryStats] | None = None,
) -> ProposedExperiment | None:
    """2-arm: control vs. ``GENERATION = '2'``.

    Returns None if the warehouse is already Gen2 or Gen2 is unavailable in
    the account's region.
    """
    if warehouse.generation == Generation.GEN2:
        return None  # already there

    arms = [
        Arm.control(),
        Arm.from_delta(WarehouseConfigDelta(generation=Generation.GEN2), name="gen2"),
    ]
    return _finalize(
        recipe_name="gen1_to_gen2",
        target=warehouse,
        account=account,
        arms=arms,
        hypothesis=(
            f"Switching {warehouse.name} from Gen1 to Gen2 reduces credits "
            f"and/or elapsed time on the same workload."
        ),
        sample_query_stats=sample_query_stats,
    )


# ── size_sweep_pm1 ───────────────────────────────────────────────────────

def size_sweep_pm1(
    warehouse: WarehouseConfig,
    account: AccountInfo,
    sample_query_stats: list[QueryStats] | None = None,
) -> ProposedExperiment | None:
    """3-arm: control, current+1, current-1.

    Returns None if the warehouse is at a ladder edge with no neighbour, OR
    if size is unknown (we'd be guessing).
    """
    if warehouse.size is None:
        return None

    up = step(warehouse.size, +1)
    down = step(warehouse.size, -1)
    arms: list[Arm] = [Arm.control()]
    if up is not None:
        arms.append(Arm.from_delta(WarehouseConfigDelta(size=up), name=f"size_up_{up}"))
    if down is not None:
        arms.append(Arm.from_delta(WarehouseConfigDelta(size=down), name=f"size_down_{down}"))

    if len(arms) < 2:
        return None  # at the edges of the ladder; nothing to sweep

    return _finalize(
        recipe_name="size_sweep_pm1",
        target=warehouse,
        account=account,
        arms=arms,
        hypothesis=(
            f"For {warehouse.name}'s observed workload, a one-step size change "
            f"(up or down from {warehouse.size}) yields better cost/perf than "
            f"the current setting."
        ),
        sample_query_stats=sample_query_stats,
    )


# ── qas_on_off ───────────────────────────────────────────────────────────

def qas_on_off(
    warehouse: WarehouseConfig,
    account: AccountInfo,
    sample_query_stats: list[QueryStats] | None = None,
) -> ProposedExperiment | None:
    """2-arm: control vs. QAS flipped.

    Returns None if the account isn't on Enterprise+ (QAS unavailable).
    """
    if not account.qas_available:
        return None

    flipped = QASState.OFF if warehouse.qas_state == QASState.ON else QASState.ON
    arms = [
        Arm.control(),
        Arm.from_delta(
            WarehouseConfigDelta(qas_state=flipped),
            name=f"qas_{flipped.value}",
        ),
    ]
    if flipped == QASState.ON:
        hypothesis = (
            f"Enabling Query Acceleration Service on {warehouse.name} reduces "
            f"elapsed time on large-scan queries enough to justify the "
            f"serverless surcharge."
        )
    else:
        hypothesis = (
            f"Disabling QAS on {warehouse.name} reduces total cost without "
            f"unacceptable latency regression — the warehouse may not benefit "
            f"from acceleration in practice."
        )

    return _finalize(
        recipe_name="qas_on_off",
        target=warehouse,
        account=account,
        arms=arms,
        hypothesis=hypothesis,
        sample_query_stats=sample_query_stats,
    )


# ── factorial_gen_x_size ────────────────────────────────────────────────

def factorial_gen_x_size(
    warehouse: WarehouseConfig,
    account: AccountInfo,
    sample_query_stats: list[QueryStats] | None = None,
) -> ProposedExperiment | None:
    """4-arm: (Gen × current-size) factorial.

    Returns None if Gen2 isn't available, the warehouse is already Gen2, or
    the size ladder has no neighbour to sweep.
    """
    if warehouse.generation == Generation.GEN2:
        return None
    if warehouse.size is None:
        return None
    up = step(warehouse.size, +1)
    if up is None:
        return None  # can't form a 2×2 if no upper neighbour

    arms: list[Arm] = [
        Arm.control(),
        Arm.from_delta(WarehouseConfigDelta(generation=Generation.GEN2), name="gen2"),
        Arm.from_delta(WarehouseConfigDelta(size=up), name=f"size_{up}"),
        Arm.from_delta(
            WarehouseConfigDelta(generation=Generation.GEN2, size=up),
            name=f"gen2_size_{up}",
        ),
    ]
    return _finalize(
        recipe_name="factorial_gen_x_size",
        target=warehouse,
        account=account,
        arms=arms,
        hypothesis=(
            f"For {warehouse.name}, the joint optimum across "
            f"(Gen1|Gen2)×({warehouse.size}|{up}) outperforms tuning "
            f"either axis in isolation."
        ),
        sample_query_stats=sample_query_stats,
    )


# ── PRESET_RECIPES registry ─────────────────────────────────────────────

PRESET_RECIPES: dict[str, Recipe] = {
    "gen1_to_gen2": gen1_to_gen2,
    "size_sweep_pm1": size_sweep_pm1,
    "qas_on_off": qas_on_off,
    "factorial_gen_x_size": factorial_gen_x_size,
}


# ── shared finalization ─────────────────────────────────────────────────

def _finalize(
    *,
    recipe_name: str,
    target: WarehouseConfig,
    account: AccountInfo,
    arms: list[Arm],
    hypothesis: str,
    sample_query_stats: list[QueryStats] | None,
    sample_size: int = _DEFAULT_SAMPLE_SIZE,
    reps_per_arm: int = _DEFAULT_REPS_PER_ARM,
) -> ProposedExperiment | None:
    """Run eligibility, drop blocked arms, and assemble the ProposedExperiment.

    Returns None if every non-control arm is blocked — there's nothing to
    test against the control.
    """
    # Run eligibility on every arm.
    for arm in arms:
        arm.eligibility_issues = check_arm_eligibility(arm, target, account)

    # Drop arms with blocking errors.  Keep the control regardless (it has
    # an empty delta and shouldn't fail eligibility, but be defensive).
    runnable_arms = [
        a for a in arms if a.is_control or not a.has_blocking_issues
    ]
    if not any(not a.is_control for a in runnable_arms):
        return None  # no non-control arms survived eligibility

    # Cost estimation.  If we have no historical query stats we can still
    # propose the experiment but mark the cost as "unknown."
    if sample_query_stats:
        # Pre-compute credit-rate for each arm (control inherits, others
        # use the merged config's effective size).
        arm_rates = {
            a.name: credit_rate((target.merge(a.delta)).size or "XSMALL")
            for a in runnable_arms
        }
        cost_estimate = estimate_experiment_cost(
            sample_query_stats=sample_query_stats,
            arm_credit_rates_per_hour=arm_rates,
            reps_per_arm=reps_per_arm,
        )
    else:
        cost_estimate = CostEstimate(
            low_credits=0.0,
            high_credits=0.0,
            rationale="cost not estimated (no historical query stats provided)",
        )

    # Aggregate any warning-level issues onto the experiment so the UI can
    # surface them at acceptance time.
    warning_issues = [
        i for arm in runnable_arms for i in arm.eligibility_issues
        if i.severity == "warning"
    ]

    return ProposedExperiment(
        recipe_name=recipe_name,
        target_warehouse=target.name.upper(),
        hypothesis=hypothesis,
        arms=runnable_arms,
        sample_size=sample_size,
        reps_per_arm=reps_per_arm,
        cost_estimate=cost_estimate,
        eligibility_issues=warning_issues,
        proposed_by="recipe:" + recipe_name,
    )
