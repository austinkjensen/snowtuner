"""Gen1→Gen2 candidate-finder recommender.

What this is
------------
A "candidate finder" — not a "decider".  Snowflake Gen2 charges ~1.35×
the credits/hour of Gen1, so Gen2 only saves money when wall-clock time
drops below ~74% of Gen1 wall-clock.  From passive QUERY_HISTORY data
alone we **cannot** predict that ratio reliably — performance depends on
CPU-bound vs IO-bound profile, memory pressure, concurrency, query
shape, and cache behavior, none of which we can simulate offline.

So this recommender's job is to **find the warehouses most likely to be
worth measuring**, and propose a ``gen1_to_gen2`` experiment for each.
The experiment's actual measurements then derive the real recommendation
through ``experiments/derive.py``.

How it scores candidates
------------------------
For each Gen1 warehouse, computes four signals over a recent lookback
window (14 days by default):

1. **Sustained credit consumption** — total credits used (from
   ``raw.warehouse_metering_history``), scaled to credits/week.  Gates
   on a minimum threshold; below it the experiment overhead probably
   exceeds any plausible savings.
2. **Compute-bound ratio** — ``SUM(execution_ms) / SUM(total_elapsed_ms)``.
   High ratio means queries spend their time computing, not queueing or
   compiling, so Gen2's faster cores have something to bite into.
3. **Local-spill prevalence** — count of queries that spilled to local
   disk.  Gen2's larger memory may eliminate the spill outright (often a
   2-10× win on those queries).
4. **"Real" query mass** — fraction of queries with elapsed > 1 second.
   Sub-second queries are dominated by Snowflake cloud-services overhead;
   Gen2 won't help them.

No composite score; the rationale lists each signal as raw evidence
attached to the proposed experiment's hypothesis.  Operators see exactly
why a warehouse was flagged.

Output
------
``predict()`` returns ``[]`` — this recommender never emits a direct
``Recommendation``.  ``propose_experiments()`` returns up to N
``ProposedExperiment`` objects (one per top-ranked Gen1 candidate).
"""
from __future__ import annotations

from typing import Any

import duckdb

from snowtuner.actions.base import ActionType
from snowtuner.experiments.cost_estimate import QueryStats
from snowtuner.experiments.eligibility import AccountInfo
from snowtuner.experiments.config_delta import WarehouseConfig
from snowtuner.experiments.model import ProposedExperiment
from snowtuner.experiments.recipes import gen1_to_gen2
from snowtuner.recommendations.model import Recommendation
from snowtuner.recommenders.base import AlwaysReadyGate, Recommender


# Tunables.  Each gate is conservative; relax if your account is small
# enough that nothing currently passes.
_LOOKBACK_DAYS = 14
_MIN_CREDITS_PER_WEEK = 20.0       # below this, experiment overhead > savings
_MIN_COMPUTE_BOUND_RATIO = 0.7     # queries spend ≥70% in actual compute
_MIN_REAL_QUERY_MASS = 0.3         # ≥30% of queries take > 1s (vs sub-second)
_MAX_PROPOSALS_PER_RUN = 3         # cap so an account-wide sweep doesn't drown the UI
_COST_ESTIMATE_SAMPLE_SIZE = 30    # how many query stats to feed the recipe's cost estimator


class Gen2CandidateFinder(Recommender):
    """Finds Gen1 warehouses worth measuring on Gen2.  Output: experiment
    proposals, not direct recommendations.
    """

    name = "gen2_candidate_finder"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE   # lineage: derived recs alter generation
    required_feature_tables: set[str] = set()  # reads raw.* directly
    training_gate = AlwaysReadyGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        # Stateless — no model parameters to fit.  Return an empty dict so
        # the orchestrator persists *something* (its persistence layer
        # expects a dict).
        return {}

    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        # This recommender never emits direct recommendations.  The
        # eventual AlterWarehouse rec comes from the experiment's
        # derive_actions() output once the experiment completes.
        return []

    def propose_experiments(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[ProposedExperiment]:
        candidates = _score_candidates(conn)
        # Drop anything that fails the gates.
        eligible = [c for c in candidates if _passes_gates(c)]
        # Rank by credit consumption (highest = most upside if Gen2 wins).
        eligible.sort(key=lambda c: c.credits_per_week, reverse=True)
        eligible = eligible[:_MAX_PROPOSALS_PER_RUN]
        if not eligible:
            return []

        # AccountInfo: optimistic defaults; the recipe's per-arm
        # eligibility check is the real gate (region, edition).  A future
        # revision can populate region/edition from CURRENT_REGION() once
        # we cache it.
        account = AccountInfo()

        proposals: list[ProposedExperiment] = []
        for cand in eligible:
            warehouse_config = _load_warehouse_config(conn, cand.warehouse_name)
            if warehouse_config is None:
                continue
            sample_stats = _sample_query_stats(
                conn, cand.warehouse_name, limit=_COST_ESTIMATE_SAMPLE_SIZE,
            )
            proposed = gen1_to_gen2(
                warehouse_config, account, sample_query_stats=sample_stats,
            )
            if proposed is None:
                # Recipe declined — warehouse is already Gen2, or no
                # non-control arms survived eligibility.  Either way, move on.
                continue
            # Enrich the hypothesis with the candidacy rationale so the UI
            # surfaces *why* this warehouse was flagged.  The recipe's
            # default hypothesis is appended for context.
            proposed.hypothesis = (
                f"{cand.rationale}\n\n"
                f"Recipe rationale: {proposed.hypothesis}"
            )
            proposed.proposed_by = f"recommender:{self.name}@{self.version}"
            proposals.append(proposed)
        return proposals


# ── candidate scoring ──────────────────────────────────────────────


class _Candidate:
    """A single warehouse's score components.  Pure data + a rendered
    rationale string for the experiment's hypothesis field.
    """

    def __init__(
        self,
        *,
        warehouse_name: str,
        credits_per_week: float,
        compute_bound_ratio: float,
        local_spill_query_count: int,
        real_query_mass: float,
        total_queries: int,
    ) -> None:
        self.warehouse_name = warehouse_name
        self.credits_per_week = credits_per_week
        self.compute_bound_ratio = compute_bound_ratio
        self.local_spill_query_count = local_spill_query_count
        self.real_query_mass = real_query_mass
        self.total_queries = total_queries

    @property
    def rationale(self) -> str:
        """Plain-English explanation of why this warehouse was flagged.

        Lists every signal as a quantified fact — no composite score, no
        hidden weighting.  Operators can judge for themselves whether the
        signals match a workload that's likely to win on Gen2.
        """
        return (
            f"Gen1 candidate for Gen2 experiment.  Over the last "
            f"{_LOOKBACK_DAYS} days, warehouse {self.warehouse_name} ran "
            f"{self.total_queries} queries with these signals:\n"
            f"  • sustained ~{self.credits_per_week:.1f} credits/week\n"
            f"  • {self.compute_bound_ratio:.0%} of wall-clock spent in "
            f"actual compute (vs queueing / compilation)\n"
            f"  • {self.local_spill_query_count} queries spilled to local "
            f"disk (Gen2's larger memory may eliminate them)\n"
            f"  • {self.real_query_mass:.0%} of queries ran > 1 second "
            f"(sub-second queries don't benefit from faster compute)\n"
            f"\nGen2 typically charges ~1.35× credits/hour; the experiment "
            f"will measure whether the actual speedup beats that premium."
        )


def _score_candidates(conn: duckdb.DuckDBPyConnection) -> list[_Candidate]:
    """Compute the four signals for every Gen1 warehouse in raw.warehouses."""
    # Pull Gen1 warehouses.  When generation is unknown (None — older
    # Snowflake or fetch error), exclude rather than guess: better to
    # silently skip than to propose an experiment on a warehouse that's
    # already Gen2.
    rows = conn.execute(
        """
        SELECT name FROM raw.warehouses
        WHERE generation = '1'
        ORDER BY name
        """
    ).fetchall()
    gen1_names = [r[0] for r in rows]
    if not gen1_names:
        return []

    out: list[_Candidate] = []
    for name in gen1_names:
        # All signals computed in one SQL pass per warehouse for
        # readability; warehouse counts are tens, not thousands, so
        # per-warehouse round-trips aren't a hotspot.
        signal_row = conn.execute(
            f"""
            WITH window_queries AS (
                SELECT
                    total_elapsed_ms,
                    execution_ms,
                    bytes_spilled_to_local
                FROM raw.query_history
                WHERE upper(warehouse_name) = upper(?)
                  AND start_time >= now() - INTERVAL {_LOOKBACK_DAYS} DAYS
                  AND execution_status = 'SUCCESS'
            )
            SELECT
                COUNT(*) AS total_queries,
                COALESCE(SUM(execution_ms), 0) AS sum_execution_ms,
                COALESCE(SUM(total_elapsed_ms), 0) AS sum_elapsed_ms,
                COALESCE(SUM(CASE WHEN bytes_spilled_to_local > 0 THEN 1 ELSE 0 END), 0)
                    AS local_spill_count,
                COALESCE(SUM(CASE WHEN total_elapsed_ms > 1000 THEN 1 ELSE 0 END), 0)
                    AS real_query_count
            FROM window_queries
            """,
            [name],
        ).fetchone()
        if signal_row is None:
            continue
        total_queries, sum_exec, sum_elapsed, spill_count, real_count = signal_row
        total_queries = int(total_queries or 0)
        if total_queries == 0:
            # No queries in the window = no signal.  Skip rather than
            # propose; an idle warehouse isn't worth experimenting on.
            continue

        compute_ratio = float(sum_exec) / float(sum_elapsed) if sum_elapsed else 0.0
        real_mass = float(real_count) / float(total_queries) if total_queries else 0.0

        # Credits over the window from metering_history (more authoritative
        # than counting up per-query credits, which we don't store).
        credit_row = conn.execute(
            f"""
            SELECT COALESCE(SUM(credits_used), 0)
            FROM raw.warehouse_metering_history
            WHERE upper(warehouse_name) = upper(?)
              AND start_time >= now() - INTERVAL {_LOOKBACK_DAYS} DAYS
            """,
            [name],
        ).fetchone()
        total_credits = float(credit_row[0] or 0) if credit_row else 0.0
        credits_per_week = total_credits * (7.0 / _LOOKBACK_DAYS)

        out.append(_Candidate(
            warehouse_name=name,
            credits_per_week=credits_per_week,
            compute_bound_ratio=compute_ratio,
            local_spill_query_count=int(spill_count or 0),
            real_query_mass=real_mass,
            total_queries=total_queries,
        ))
    return out


def _passes_gates(c: _Candidate) -> bool:
    """Apply the three eligibility gates.

    A warehouse needs to clear ALL of:
      * credits/week ≥ _MIN_CREDITS_PER_WEEK     (worth the experiment cost)
      * compute_bound_ratio ≥ _MIN_COMPUTE_BOUND_RATIO  (Gen2 has signal to bite into)
      * one of: local-spill present  OR  real_query_mass ≥ _MIN_REAL_QUERY_MASS
                (some workload exists that's plausibly speedup-able)
    """
    if c.credits_per_week < _MIN_CREDITS_PER_WEEK:
        return False
    if c.compute_bound_ratio < _MIN_COMPUTE_BOUND_RATIO:
        return False
    if c.local_spill_query_count == 0 and c.real_query_mass < _MIN_REAL_QUERY_MASS:
        return False
    return True


# ── helpers shared with the experiments API ───────────────────────


def _load_warehouse_config(
    conn: duckdb.DuckDBPyConnection, warehouse_name: str,
) -> WarehouseConfig | None:
    """Mirror of ``ExperimentEngine._load_control_config`` — kept local to
    avoid an import cycle (the engine imports recommenders for size
    utilities, recommenders shouldn't import the engine in return).
    """
    row = conn.execute(
        """
        SELECT name, size, auto_suspend_seconds, auto_resume, generation
        FROM raw.warehouses
        WHERE upper(name) = upper(?)
        """,
        [warehouse_name],
    ).fetchone()
    if not row:
        return None
    return WarehouseConfig(
        name=row[0],
        size=row[1],
        auto_suspend_seconds=row[2],
        auto_resume=bool(row[3]) if row[3] is not None else None,
        # Generation gets pulled separately by the recipe via Arm.delta;
        # WarehouseConfig.generation is None when unknown.
        generation=None,
    )


def _sample_query_stats(
    conn: duckdb.DuckDBPyConnection,
    warehouse_name: str,
    *,
    limit: int,
) -> list[QueryStats]:
    """Pull recent SUCCESS-status query stats for the recipe's cost estimator.

    Duplicates ``snowtuner.api.app._resolve_workload`` in spirit but with a
    much simpler shape — the recipe just needs ~30 timing samples to size
    the experiment's credit budget.  No safety filters here; the recipe and
    the engine apply them before replay.
    """
    rows = conn.execute(
        """
        SELECT query_id, total_elapsed_ms, total_elapsed_ms, bytes_scanned
        FROM raw.query_history
        WHERE upper(warehouse_name) = upper(?)
          AND execution_status = 'SUCCESS'
          AND query_type = 'SELECT'
          AND query_parameterized_hash IS NOT NULL
        ORDER BY start_time DESC
        LIMIT ?
        """,
        [warehouse_name, limit],
    ).fetchall()
    return [
        QueryStats(
            query_id=r[0],
            p50_elapsed_ms=float(r[1] or 0),
            mean_elapsed_ms=float(r[2] or 0),
            bytes_scanned=int(r[3]) if r[3] is not None else None,
        )
        for r in rows
    ]
