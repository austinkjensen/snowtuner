"""AutoSuspendSurvivalTuner — cost-minimizing AUTO_SUSPEND via survival analysis.

Model
=====
Let T = reactivation gap = seconds from SUSPEND_WAREHOUSE to the next
RESUME_WAREHOUSE on the same warehouse.  Its survival function is
``S(t) = P(T > t)``.  Let C = cold-start cost (in seconds of equivalent
billed time).

If AUTO_SUSPEND is set to ``AS``, the per-cycle cost after the last query is:

    cost(AS) = min(T, AS) + C · 1{T > AS}
               └ billed idle ┘   └ cold-start penalty if we suspended ┘

Taking expectation and differentiating gives the classic result that the
**optimal AS is where the hazard rate h(t) = f(t)/S(t) equals 1/C** —
intuitively: set AS at the point where another second of waiting buys exactly
as much expected idle-billing as the amortized cold-start penalty.

Rather than estimate densities, we compute E[cost(AS)] directly from the
empirical sample over a grid of AS candidates (vectorized).  This is
identically what the hazard condition gives, but does not require a parametric
or smoothed density.

Compared to the p25 heuristic, this:
  * uses the full distribution, not one percentile
  * is explicitly cost-minimizing (sensitive to C)
  * degrades gracefully for bi-modal / long-tailed patterns
  * stores the survival curve in model_state so the UI can plot it
"""
from __future__ import annotations

from typing import Any

import duckdb
import numpy as np

from snowtuner.actions import AlterWarehouse, WarehouseKnob
from snowtuner.actions.alter_warehouse import KnobChange
from snowtuner.actions.base import ActionType
from snowtuner.recommendations.model import (
    EvidenceRef,
    Impact,
    Recommendation,
)
from snowtuner.recommenders.base import (
    ReadinessReport,
    Recommender,
    TrainingGate,
)


# Minimum suspend/resume cycles per warehouse before we start recommending.
# 10 is enough to get a coarse p25 estimate; the confidence score already
# down-weights small samples (see _confidence below).  Real-world small
# Snowflake accounts can take weeks to accumulate the originally-defaulted 30.
MIN_CYCLES_PER_WAREHOUSE = 10
AUTO_SUSPEND_MIN = 60
AUTO_SUSPEND_MAX = 600
AUTO_SUSPEND_STEP = 5  # grid resolution in seconds
MIN_DELTA_SECONDS = 30


# Approximate cold-start cost per warehouse size (seconds of equivalent billed
# time).  Larger warehouses take meaningfully longer to resume.  These are
# conservative defaults; the value really ought to come from an observed p95
# of resume durations in warehouse_events_history, which is a later refinement.
COLD_START_COST_BY_SIZE: dict[str, float] = {
    "XSMALL":   8,
    "SMALL":    10,
    "MEDIUM":   15,
    "LARGE":    20,
    "XLARGE":   25,
    "2XLARGE":  30,
    "3XLARGE":  40,
    "4XLARGE":  50,
    "5XLARGE":  60,
    "6XLARGE":  75,
}
DEFAULT_COLD_START_COST = 15.0


class SurvivalReadinessGate(TrainingGate):
    """Require enough suspend/resume cycles to get a stable survival curve."""

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        rows = conn.execute(
            """
            SELECT warehouse_name, COUNT(*) AS cycles
            FROM raw.warehouse_events_history
            WHERE event_name IN ('SUSPEND_WAREHOUSE', 'RESUME_WAREHOUSE')
            GROUP BY warehouse_name
            """
        ).fetchall()
        if not rows:
            return ReadinessReport(
                is_ready=False,
                reason="no warehouse events ingested yet",
                signals={"warehouses_with_events": 0},
            )
        ready = [w for w, c in rows if c >= MIN_CYCLES_PER_WAREHOUSE * 2]
        if not ready:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    f"no warehouse has ≥{MIN_CYCLES_PER_WAREHOUSE} suspend/resume cycles yet; "
                    f"observed: {dict(rows)}"
                ),
                signals={"warehouses_with_events": len(rows)},
            )
        return ReadinessReport(
            is_ready=True,
            reason=f"{len(ready)} warehouse(s) have enough history",
            signals={"ready_warehouses": ready},
        )


class AutoSuspendSurvivalTuner(Recommender):
    name = "auto_suspend_survival_tuner"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE
    required_feature_tables = {"features.warehouse_idle_gaps"}
    training_gate = SurvivalReadinessGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        """Fit the empirical survival curve and optimal-cost curve per warehouse."""
        rows = conn.execute(
            """
            WITH evts AS (
                SELECT warehouse_name, event_name, timestamp,
                       LEAD(event_name) OVER (
                           PARTITION BY warehouse_name ORDER BY timestamp
                       ) AS next_name,
                       LEAD(timestamp) OVER (
                           PARTITION BY warehouse_name ORDER BY timestamp
                       ) AS next_ts
                FROM raw.warehouse_events_history
                WHERE event_name IN ('SUSPEND_WAREHOUSE', 'RESUME_WAREHOUSE')
            )
            SELECT warehouse_name,
                   date_diff('second', timestamp, next_ts) AS reactivation_seconds
            FROM evts
            WHERE event_name = 'SUSPEND_WAREHOUSE'
              AND next_name = 'RESUME_WAREHOUSE'
              AND next_ts IS NOT NULL
            """
        ).fetchall()

        gaps_by_wh: dict[str, list[float]] = {}
        for wh, gap in rows:
            if gap is None or gap < 0:
                continue
            gaps_by_wh.setdefault(wh, []).append(float(gap))

        sizes = {
            r[0].upper(): (r[1] or "").upper().replace("-", "").replace(" ", "")
            for r in conn.execute("SELECT name, size FROM raw.warehouses").fetchall()
        }

        state: dict[str, Any] = {}
        for wh, gaps in gaps_by_wh.items():
            if len(gaps) < MIN_CYCLES_PER_WAREHOUSE:
                continue
            size_key = sizes.get(wh.upper(), "")
            C = COLD_START_COST_BY_SIZE.get(size_key, DEFAULT_COLD_START_COST)
            fit = _fit_survival(gaps, cold_start_cost=C)
            fit["cold_start_cost_seconds"] = C
            state[wh] = fit
        return {"per_warehouse": state}

    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        per_wh = (model_state or {}).get("per_warehouse") or {}
        if not per_wh:
            return []

        current = {
            row[0]: {"auto_suspend_seconds": row[1], "size": row[2]}
            for row in conn.execute(
                "SELECT name, auto_suspend_seconds, size FROM raw.warehouses"
            ).fetchall()
        }

        recs: list[Recommendation] = []
        for wh, fit in per_wh.items():
            cur = current.get(wh) or current.get(wh.upper())
            cur_as = cur["auto_suspend_seconds"] if cur else None
            proposed = int(fit["optimal_as"])

            if cur_as is not None and abs(proposed - int(cur_as)) < MIN_DELTA_SECONDS:
                continue

            # Expected savings: per-cycle cost at current setting minus per-cycle
            # cost at optimum, times cycles/day, converted to credits.
            cycles_per_day = _estimate_cycles_per_day(conn, wh)
            cost_at_current = _lookup_cost(fit, cur_as) if cur_as is not None else None
            cost_saved_per_cycle = (
                (cost_at_current - fit["optimal_cost"]) if cost_at_current is not None else 0.0
            )
            seconds_saved_daily = max(0.0, cost_saved_per_cycle * cycles_per_day)
            credit_rate = _credit_rate_for_size((cur or {}).get("size"))
            credits_delta_daily = -round((seconds_saved_daily / 3600.0) * credit_rate, 2)

            # Describe distribution succinctly for the rationale
            q = fit["quantiles"]
            n = fit["n"]
            rationale = (
                f"Survival-based fit on {n} suspend→resume cycles for {wh}. "
                f"Reactivation-gap quantiles: p25={q['p25']:.0f}s, p50={q['p50']:.0f}s, "
                f"p75={q['p75']:.0f}s.  Cold-start cost assumed {fit['cold_start_cost_seconds']:.0f}s. "
                f"Expected per-cycle cost minimized at AUTO_SUSPEND={proposed}s "
                f"(cost={fit['optimal_cost']:.1f}s)."
            )
            if cur_as is not None and cost_at_current is not None:
                rationale += (
                    f"  Current AUTO_SUSPEND={int(cur_as)}s has expected per-cycle cost "
                    f"{cost_at_current:.1f}s → saving ~{cost_saved_per_cycle:.1f}s per cycle."
                )

            evidence = [
                EvidenceRef(
                    kind="warehouse_events",
                    description=f"{n} suspend→resume cycles observed",
                    metric="n_cycles",
                    value=float(n),
                ),
                EvidenceRef(
                    kind="survival_curve",
                    description="Reactivation survival curve (empirical)",
                    metric="p50_seconds",
                    value=q["p50"],
                ),
                EvidenceRef(
                    kind="cost_curve",
                    description="Expected per-cycle cost minimum",
                    metric="optimal_cost_seconds",
                    value=fit["optimal_cost"],
                ),
            ]

            action = AlterWarehouse(
                warehouse_name=wh,
                changes=[KnobChange(
                    knob=WarehouseKnob.AUTO_SUSPEND,
                    current_value=int(cur_as) if cur_as is not None else None,
                    proposed_value=proposed,
                )],
            )
            recs.append(Recommendation(
                generated_by=self.generated_by,
                action=action,
                rationale=rationale,
                evidence=evidence,
                expected_impact=Impact(
                    credits_delta_daily=credits_delta_daily,
                    confidence=_confidence(n, fit["optimal_cost_margin"]),
                    notes=(
                        f"based on {n} observed cycles; cold-start cost "
                        f"{fit['cold_start_cost_seconds']:.0f}s"
                    ),
                ),
            ))
        return recs


# ---------------------------------------------------------------------------
# Core fit: empirical cost-minimizing grid search
# ---------------------------------------------------------------------------

def _fit_survival(gaps: list[float], *, cold_start_cost: float) -> dict[str, Any]:
    """Compute the empirical survival curve + optimal AUTO_SUSPEND.

    Returns a JSON-serializable dict suitable for storage in model_state.
    """
    g = np.asarray(gaps, dtype=float)
    n = int(g.size)

    # Empirical survival function — sorted gaps at S(t) = (n - rank) / n.
    order = np.argsort(g)
    sorted_gaps = g[order]
    survival = 1.0 - (np.arange(1, n + 1) / n)  # S at each sorted gap

    # Cost curve: E[min(T, AS) + C·1{T>AS}] over AS grid, vectorized.
    grid = np.arange(AUTO_SUSPEND_MIN, AUTO_SUSPEND_MAX + 1, AUTO_SUSPEND_STEP, dtype=float)
    gaps_col = g[:, None]
    grid_row = grid[None, :]
    min_vals = np.minimum(gaps_col, grid_row)
    cold_penalty = cold_start_cost * (gaps_col > grid_row).astype(float)
    cost_by_as = (min_vals + cold_penalty).mean(axis=0)
    best_idx = int(np.argmin(cost_by_as))
    optimal_as = float(grid[best_idx])
    optimal_cost = float(cost_by_as[best_idx])

    # "Flatness" margin: how much cost would go up at ±30s from the optimum.
    # A small margin means the minimum is broad / insensitive → lower confidence.
    margin = float(np.mean([
        abs(cost_by_as[min(best_idx + int(30 / AUTO_SUSPEND_STEP), len(cost_by_as) - 1)] - optimal_cost),
        abs(cost_by_as[max(best_idx - int(30 / AUTO_SUSPEND_STEP), 0)] - optimal_cost),
    ]))

    quantiles = {
        "p25": float(np.percentile(g, 25)),
        "p50": float(np.percentile(g, 50)),
        "p75": float(np.percentile(g, 75)),
        "p90": float(np.percentile(g, 90)),
    }

    # Down-sample survival curve to ~50 points for compact storage.
    if n > 50:
        idx = np.linspace(0, n - 1, num=50).astype(int)
    else:
        idx = np.arange(n)
    survival_curve = [
        {"t": float(sorted_gaps[i]), "s": float(survival[i])} for i in idx
    ]

    # Down-sample cost curve too.
    cost_curve = [
        {"as": float(a), "cost": float(c)}
        for a, c in zip(grid[::2], cost_by_as[::2])
    ]

    return {
        "n": n,
        "optimal_as": optimal_as,
        "optimal_cost": optimal_cost,
        "optimal_cost_margin": margin,
        "quantiles": quantiles,
        "survival_curve": survival_curve,
        "cost_curve": cost_curve,
    }


def _lookup_cost(fit: dict[str, Any], as_value: float | None) -> float | None:
    """Evaluate the stored cost curve at an arbitrary AS value (linear interp)."""
    if as_value is None:
        return None
    curve = fit.get("cost_curve") or []
    if not curve:
        return None
    xs = np.array([p["as"] for p in curve])
    ys = np.array([p["cost"] for p in curve])
    # If as_value is outside grid, clamp to nearest endpoint.
    return float(np.interp(float(as_value), xs, ys))


def _estimate_cycles_per_day(
    conn: duckdb.DuckDBPyConnection, warehouse_name: str,
) -> float:
    row = conn.execute(
        """
        SELECT COUNT(*) AS cycles,
               date_diff('day', MIN(timestamp), MAX(timestamp)) AS days
        FROM raw.warehouse_events_history
        WHERE warehouse_name = ? AND event_name = 'SUSPEND_WAREHOUSE'
        """,
        [warehouse_name],
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return 0.0
    return row[0] / max(row[1], 1)


def _credit_rate_for_size(size: str | None) -> float:
    if not size:
        return 1.0
    s = size.upper().replace("-", "").replace(" ", "")
    return {
        "XSMALL": 1, "SMALL": 2, "MEDIUM": 4, "LARGE": 8, "XLARGE": 16,
        "2XLARGE": 32, "3XLARGE": 64, "4XLARGE": 128, "5XLARGE": 256,
        "6XLARGE": 512,
    }.get(s, 1.0)


def _confidence(n: int, cost_margin: float) -> float:
    """Blend sample-size confidence with the sharpness of the cost minimum.

    A wide-flat minimum (small margin) means many AS values are near-optimal —
    less reason to insist on our proposal.  A sharp minimum means the answer
    is well-identified.
    """
    if n <= 0:
        return 0.0
    size_conf = 1.0 - 10.0 / (n + 10.0)          # 0.67 at n=20, 0.87 at n=65
    sharp_conf = min(1.0, cost_margin / 5.0)      # 5s margin → 1.0
    return float(0.5 * size_conf + 0.5 * sharp_conf)
