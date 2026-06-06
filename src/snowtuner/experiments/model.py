"""Experiment domain models — ProposedExperiment, Experiment, ExperimentReport."""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from snowtuner.actions.base import Issue
from snowtuner.experiments.arm import Arm
from snowtuner.experiments.cost_estimate import CostEstimate


class ExperimentStatus(str, Enum):
    PROPOSED = "PROPOSED"
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class ExperimentKind(str, Enum):
    """What problem the experiment is trying to solve.

    Two kinds, distinguished by what's anchored:

    ``TUNING`` — anchored to a specific warehouse.  We're asking
    "how should *this* warehouse change?"  The control arm is implicit
    (the warehouse's current config); non-control arms are *deltas* on
    top of that config.  Produces a recommendation if a winning arm is
    found.

    ``BENCHMARK`` — anchored to a workload.  We're asking "across these
    N candidate configurations, which performs best on this set of
    queries?"  Arms are *absolute* configs; control is optional (one of
    the arms may be designated as reference, or none).  Produces a
    benchmark report; doesn't directly emit a recommendation since
    there's no specific warehouse being optimized.
    """
    TUNING = "tuning"
    BENCHMARK = "benchmark"


class ProposedExperiment(BaseModel):
    """What a recommender (or a user via the UI) proposes when more data is
    needed before recommending.

    Mirrors the role ``Recommendation`` plays for advisory output.  Frozen
    once accepted — the engine reads from this same spec to drive the run.
    """
    kind: ExperimentKind = ExperimentKind.TUNING
    recipe_name: str            # which preset constructed this; "user_built" for from-scratch
    target_warehouse: str | None = None
        # TUNING:   required — the warehouse being optimized (also workload source + control)
        # BENCHMARK: optional — if set, the workload source; never used as control
    workload_warehouse: str | None = None
        # BENCHMARK only — explicit workload source.  Falls back to target_warehouse
        # when None.  In v1 we only support "queries from one warehouse"; a saved
        # query group ref will land here in a later slice.
    control_arm_name: str | None = "control"
        # TUNING:   always "control" (the implicit empty-delta arm)
        # BENCHMARK: arm name designated as reference baseline, or None for no control
    hypothesis: str
    arms: list[Arm]             # TUNING includes implicit control; BENCHMARK is whatever the user built
    sample_size: int
    reps_per_arm: int
    cost_estimate: CostEstimate
    eligibility_issues: list[Issue] = Field(default_factory=list)
    proposed_by: str            # recommender name@version, or 'user' for UI-built

    # ── Workload resolution ──────────────────────────────────────────
    # The frozen list of query IDs that will actually be replayed.  Resolved
    # at propose-time (from either the warehouse auto-sampler or a saved query
    # group) so the user can preview and edit the workload before accepting.
    # Engine reads from this list at run time instead of re-sampling.
    #
    # Empty list means "fall back to live sampling at run time" — taken when
    # a recommender's ``propose_experiments()`` returns a recipe-built
    # ProposedExperiment that bypasses the API workload resolver.
    sampled_query_ids: list[str] = Field(default_factory=list)
    # Source provenance — purely informational, lets the UI render
    # "Workload: 30 queries from saved group 'ETL slow queries'".
    workload_source: str = "auto"   # 'auto' | 'group:<id>'
    # Non-blocking issues surfaced during resolution: "only 18 eligible
    # queries in the group; requested 30", "5 queries excluded for unsafe
    # text", etc.  Carried forward into the report's sample_size_warnings.
    sample_warnings: list[str] = Field(default_factory=list)


class ArmObservation(BaseModel):
    """Aggregated metrics for a single arm.

    Carries two complementary views:

    1. **Absolute stats** (always populated): the arm's own mean/p50/p95
       elapsed and per-query credits.  Used by the benchmark Pareto-frontier
       ranking and shown on every report regardless of kind.
    2. **Delta stats** (populated only when there's a control to pair
       against): mean/p50/p95 of (arm_elapsed - control_elapsed) and per-query
       credit deltas, plus Bonferroni-corrected p-values.  Used by the
       tuning best-arm rule.
    """
    arm_name: str
    n_queries_run: int
    n_queries_failed: int
    n_queries_excluded: int

    # ── Absolute stats (always populated) ───────────────────────────
    elapsed_ms_mean: float = 0.0
    elapsed_ms_p50: float = 0.0
    elapsed_ms_p95: float = 0.0
    credits_per_query_mean: float = 0.0

    # ── Paired-test deltas vs control ────────────────────────────────
    # Negative elapsed/credits = improvement vs control.
    # All zero when there's no control (benchmark experiments without a
    # designated reference arm).
    elapsed_ms_delta_mean: float = 0.0
    elapsed_ms_delta_p50: float = 0.0
    elapsed_ms_delta_p95: float = 0.0
    elapsed_ms_delta_ci_low: float = 0.0
    elapsed_ms_delta_ci_high: float = 0.0

    credits_per_query_delta_mean: float = 0.0
    credits_per_query_delta_ci_low: float = 0.0
    credits_per_query_delta_ci_high: float = 0.0

    # Bonferroni-corrected p-value for "this arm differs from control."
    elapsed_p_value_corrected: float | None = None
    credits_p_value_corrected: float | None = None

    # ── Pareto-frontier metadata (benchmark only) ────────────────────
    # True if this arm is on the Pareto frontier of (credits_per_query_mean,
    # elapsed_ms_p95) — no other arm dominates it on both metrics.
    is_pareto_optimal: bool = False


class ExperimentReport(BaseModel):
    """Final report.  Generated once; the engine writes it onto the
    ``Experiment`` and computes a derived recommendation if a clear winner
    emerges.
    """
    experiment_id: int
    arms: list[ArmObservation]   # excludes control (it's the baseline)

    # Best arm vs default objective: minimize credits, no p95 latency regression
    # beyond a configured tolerance.
    best_arm_name: str | None = None
    best_arm_rationale: str | None = None
    best_arm_objective: str | None = None  # which objective was used

    # Annual savings projection (wide range, explicit assumptions).
    projected_annual_savings_low_credits: float | None = None
    projected_annual_savings_high_credits: float | None = None

    # Latency projections.
    projected_p95_latency_delta_pct_low: float | None = None
    projected_p95_latency_delta_pct_high: float | None = None

    # Honesty signals.
    sample_size_warnings: list[str] = Field(default_factory=list)
    excluded_query_count: int = 0
    statistical_corrections_applied: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)


class Experiment(BaseModel):
    """A persisted experiment row.  Everything we know about one experiment
    from proposal to completion (or abortion / failure).
    """
    id: int
    proposed: ProposedExperiment
    status: ExperimentStatus

    # Lifecycle timestamps.
    proposed_at: datetime
    accepted_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    aborted_reason: str | None = None

    # Runtime accounting.
    actual_cost_credits: float | None = None
    cost_cap_hit: bool = False

    # Outputs.
    report: ExperimentReport | None = None
    derived_recommendation_id: int | None = None

    # Cleanup discipline.
    test_warehouse_names: list[str] = Field(default_factory=list)
    test_warehouses_cleaned: bool = False


class RunStatus(str, Enum):
    """Outcome of a single (arm, query, rep) replay."""
    SUCCESS = "success"
    FAILED = "failed"
    EXCLUDED = "excluded"


class ExperimentRun(BaseModel):
    """One row of ``app.experiment_runs`` — a single replay of one sampled
    query against one arm at one rep index.

    The engine writes these as they complete; the stats step aggregates them
    into the per-arm ``ArmObservation``s on the final report.  Stored
    individually (rather than only as aggregates) so we can re-aggregate with
    different exclusion rules without re-running the experiment.
    """
    experiment_id: int
    arm_name: str
    rep_index: int
    sampled_query_id: str           # the historical query_id we sampled from
    parameterized_hash: str | None = None  # family fingerprint

    # Outputs of the actual replay.
    replay_query_id: str | None = None     # the new query_id Snowflake assigned
    elapsed_ms: int | None = None
    queued_overload_ms: int | None = None
    bytes_scanned: int | None = None
    bytes_spilled_local: int | None = None
    bytes_spilled_remote: int | None = None
    credits_used_estimate: float | None = None

    status: RunStatus
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
