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


class ProposedExperiment(BaseModel):
    """What a recommender (or a user via the UI) proposes when more data is
    needed before recommending.

    Mirrors the role ``Recommendation`` plays for advisory output.  Frozen
    once accepted — the engine reads from this same spec to drive the run.
    """
    recipe_name: str            # which preset constructed this
    target_warehouse: str       # control warehouse (UPPERCASE)
    hypothesis: str             # plain-English statement
    arms: list[Arm]             # always includes the control arm
    sample_size: int            # number of distinct queries to sample
    reps_per_arm: int           # repetitions per (arm, query) pair
    cost_estimate: CostEstimate
    eligibility_issues: list[Issue] = Field(default_factory=list)
    proposed_by: str            # recommender name@version, or 'user' for UI-built


class ArmObservation(BaseModel):
    """Aggregated metrics for a single non-control arm vs the control arm."""
    arm_name: str
    n_queries_run: int
    n_queries_failed: int
    n_queries_excluded: int

    # Paired-test deltas vs control.  Negative elapsed/credits = improvement.
    elapsed_ms_delta_mean: float
    elapsed_ms_delta_p50: float
    elapsed_ms_delta_p95: float
    elapsed_ms_delta_ci_low: float
    elapsed_ms_delta_ci_high: float

    credits_per_query_delta_mean: float
    credits_per_query_delta_ci_low: float
    credits_per_query_delta_ci_high: float

    # Bonferroni-corrected p-value for "this arm differs from control."
    elapsed_p_value_corrected: float | None = None
    credits_p_value_corrected: float | None = None


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
