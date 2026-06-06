"""Multi-cluster reducer recommender.

What this is
------------
**Path 1** in the recommender taxonomy — direct recommendation, no
experiment intermediate.  For warehouses configured with multi-cluster
scale-out (``MAX_CLUSTER_COUNT > 1``), we observe whether Snowflake
actually used the headroom.  If a warehouse with ``MIN=2, MAX=5`` has
only ever provisioned cluster #1 over the past 14 days, then:

  * **MIN=2** is paying for a second always-on cluster that never does
    work — pure waste while the warehouse is active.
  * **MAX=5** offers headroom for spikes that haven't materialized —
    not a cost today, but a future-risk surface if a query storm ever
    triggers a 4-cluster scale-out.

Both knobs are tunable down with no measurable risk: the peak observed
fleet size is what's empirically needed, plus a safety margin on MAX.

Why direct recommendation (not experiment)
------------------------------------------
Unlike Gen2 / QAS / size sweeps, this isn't a workload-quality question.
The data is unambiguous: Snowflake's auto-scaler only provisions clusters
that are needed.  If cluster #3 was never provisioned in 14 days,
``MAX=3`` is safe with no further measurement.  Running an experiment to
"check whether MAX=3 still works" would just confirm what the data
already shows.  ``derive_actions`` style indirection adds latency without
adding information.

Where the signal comes from
---------------------------
``raw.warehouse_events_history.cluster_number`` is populated by Snowflake
for cluster-level provisioning events (cluster #N became active /
inactive at time T).  ``MAX(cluster_number)`` over the window is the
authoritative peak observed fleet size.

Caveats
-------
* If ``cluster_number`` is NULL on every event for a warehouse (older
  Snowflake versions, or a sync that predates this column being
  available), we can't classify safely — skip.
* If the warehouse's MAX is currently 1, there's nothing to reduce.
* We don't predict the savings of MAX-only tightening because lowered MAX
  doesn't save anything today (Snowflake wasn't using those clusters).
  We emit the rec anyway as a guardrail recommendation — savings shown
  as 0, rationale frames it as future-risk reduction.
"""
from __future__ import annotations

from typing import Any

import duckdb

from snowtuner.actions.alter_warehouse import AlterWarehouse, KnobChange, WarehouseKnob
from snowtuner.actions.base import ActionType
from snowtuner.recommendations.model import EvidenceRef, Impact, Recommendation
from snowtuner.recommenders.base import AlwaysReadyGate, Recommender


_LOOKBACK_DAYS = 14
_MIN_CREDITS_PER_WEEK = 20.0     # same cost gate as the other v0.2 finders


class MultiClusterReducer(Recommender):
    """Lowers MIN_CLUSTER_COUNT / MAX_CLUSTER_COUNT to the empirically-needed
    fleet size + safety margin.  Direct recommendation, no experiment."""

    name = "multi_cluster_reducer"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE
    required_feature_tables: set[str] = set()
    training_gate = AlwaysReadyGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        return {}

    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        candidates = _score_candidates(conn)
        out: list[Recommendation] = []
        for c in candidates:
            rec = _build_recommendation(c)
            if rec is not None:
                out.append(rec)
        return out


# ── candidate scoring ──────────────────────────────────────────────


class _Candidate:
    """A warehouse's current cluster bounds + observed peak."""

    def __init__(
        self,
        *,
        warehouse_name: str,
        current_min: int,
        current_max: int,
        peak_observed_cluster: int,
        credits_per_week: float,
    ) -> None:
        self.warehouse_name = warehouse_name
        self.current_min = current_min
        self.current_max = current_max
        self.peak_observed_cluster = peak_observed_cluster
        self.credits_per_week = credits_per_week

    @property
    def recommended_min(self) -> int:
        """At least 1 cluster (Snowflake's floor); else floor on peak."""
        return max(1, self.peak_observed_cluster)

    @property
    def recommended_max(self) -> int:
        """Peak + 1 for safety, but never below the recommended MIN."""
        return max(self.recommended_min, self.peak_observed_cluster + 1)

    @property
    def has_change(self) -> bool:
        return (
            self.recommended_min < self.current_min
            or self.recommended_max < self.current_max
        )

    @property
    def min_savings_per_week(self) -> float:
        """Approximate credits/week saved by lowering MIN.

        Snowflake bills each running cluster independently while the
        warehouse is active.  Lowering MIN from N to M removes (N-M)
        always-on clusters; that fraction of the active-time credit spend
        goes away.  This estimate assumes the workload remains within the
        new MAX (which the peak-observed signal confirms is true).
        """
        if self.current_min <= 0:
            return 0.0
        reduction = self.current_min - self.recommended_min
        if reduction <= 0:
            return 0.0
        return self.credits_per_week * (reduction / self.current_min)


def _score_candidates(conn: duckdb.DuckDBPyConnection) -> list[_Candidate]:
    """Compute the peak-observed cluster for every multi-cluster warehouse."""
    rows = conn.execute(
        """
        SELECT name, min_cluster_count, max_cluster_count
        FROM raw.warehouses
        WHERE max_cluster_count IS NOT NULL
          AND max_cluster_count > 1
        ORDER BY name
        """
    ).fetchall()
    if not rows:
        return []

    out: list[_Candidate] = []
    for name, current_min, current_max in rows:
        # Peak cluster ever provisioned in the window.  COALESCE 0 means
        # "never seen" — we treat that as no signal and skip below.
        peak_row = conn.execute(
            f"""
            SELECT COALESCE(MAX(cluster_number), 0) AS peak
            FROM raw.warehouse_events_history
            WHERE upper(warehouse_name) = upper(?)
              AND timestamp >= now() - INTERVAL {_LOOKBACK_DAYS} DAYS
              AND cluster_number IS NOT NULL
            """,
            [name],
        ).fetchone()
        peak = int(peak_row[0]) if peak_row else 0
        if peak == 0:
            # No cluster-level events at all — either the warehouse was
            # idle the whole window or the seed/sync didn't populate
            # cluster_number.  Can't classify safely; skip.
            continue

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
        if credits_per_week < _MIN_CREDITS_PER_WEEK:
            continue

        out.append(_Candidate(
            warehouse_name=name,
            current_min=int(current_min or 1),
            current_max=int(current_max),
            peak_observed_cluster=peak,
            credits_per_week=credits_per_week,
        ))
    return out


# ── recommendation construction ────────────────────────────────────


def _build_recommendation(c: _Candidate) -> Recommendation | None:
    if not c.has_change:
        return None

    changes: list[KnobChange] = []
    if c.recommended_min < c.current_min:
        changes.append(KnobChange(
            knob=WarehouseKnob.MIN_CLUSTER_COUNT,
            current_value=c.current_min,
            proposed_value=c.recommended_min,
        ))
    if c.recommended_max < c.current_max:
        changes.append(KnobChange(
            knob=WarehouseKnob.MAX_CLUSTER_COUNT,
            current_value=c.current_max,
            proposed_value=c.recommended_max,
        ))

    rationale_parts = [
        f"Over the last {_LOOKBACK_DAYS} days, {c.warehouse_name} provisioned at "
        f"most cluster #{c.peak_observed_cluster} (out of "
        f"MIN={c.current_min}, MAX={c.current_max}).",
    ]
    if c.recommended_min < c.current_min:
        savings = c.min_savings_per_week
        rationale_parts.append(
            f"MIN_CLUSTER_COUNT={c.current_min} keeps "
            f"{c.current_min - c.recommended_min} always-on cluster(s) running "
            f"that never received work — lowering to "
            f"{c.recommended_min} saves ~{savings:.1f} credits/week."
        )
    if c.recommended_max < c.current_max:
        if c.recommended_min < c.current_min:
            rationale_parts.append(
                f"MAX_CLUSTER_COUNT={c.current_max} also offers more headroom "
                f"than the observed peak; reducing to {c.recommended_max} "
                f"(peak + 1 safety margin) doesn't change today's bill but "
                f"caps the surprise-scale-out risk."
            )
        else:
            rationale_parts.append(
                f"MAX_CLUSTER_COUNT={c.current_max} offers more headroom than "
                f"the observed peak; reducing to {c.recommended_max} (peak + 1 "
                f"safety margin) caps the surprise-scale-out risk.  MIN is "
                f"already right-sized."
            )

    # Daily savings = weekly / 7.  The Impact model uses daily as the
    # canonical unit for cross-recommendation comparison.
    credits_delta_daily = -c.min_savings_per_week / 7.0  # negative = savings

    return Recommendation(
        generated_by=f"multi_cluster_reducer@0.1.0",
        action=AlterWarehouse(
            warehouse_name=c.warehouse_name,
            changes=changes,
        ),
        rationale=" ".join(rationale_parts),
        evidence=[
            EvidenceRef(
                kind="warehouse_events",
                description=(
                    f"peak cluster_number observed for {c.warehouse_name} "
                    f"over {_LOOKBACK_DAYS}d"
                ),
                filters={
                    "warehouse_name": c.warehouse_name,
                    "lookback_days": _LOOKBACK_DAYS,
                },
                metric="peak_cluster_number",
                value=float(c.peak_observed_cluster),
            ),
            EvidenceRef(
                kind="metering",
                description=(
                    f"sustained credit consumption for {c.warehouse_name}"
                ),
                filters={
                    "warehouse_name": c.warehouse_name,
                    "lookback_days": _LOOKBACK_DAYS,
                },
                metric="credits_per_week",
                value=c.credits_per_week,
            ),
        ],
        expected_impact=Impact(
            credits_delta_daily=credits_delta_daily,
            confidence=0.9,
        ),
    )
