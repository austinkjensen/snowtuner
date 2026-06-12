"""AutoSuspendSurvivalTuner — cost-minimizing AUTO_SUSPEND via survival analysis.

Model
=====
Let G = idle gap = seconds between one busy period ending and the next
compute-bearing query arriving on the warehouse (from
``features.warehouse_idle_gaps``, derived purely from query history).
Its survival function is ``S(t) = P(G > t)``.  Let C = cold-start cost
(in seconds of equivalent billed time).

If AUTO_SUSPEND is set to ``AS``, the per-gap cost after the last query is:

    cost(AS) = min(G, AS) + C · 1{G > AS}
               └ billed idle ┘   └ cold-start penalty if we suspended ┘

Taking expectation and differentiating gives the classic result that the
**optimal AS is where the hazard rate h(t) = f(t)/S(t) equals 1/C** —
intuitively: set AS at the point where another second of waiting buys exactly
as much expected idle-billing as the amortized cold-start penalty.

Rather than estimate densities, we compute E[cost(AS)] directly from the
empirical sample over a grid of AS candidates (vectorized).  This is
identically what the hazard condition gives, but does not require a parametric
or smoothed density.

Why query-history gaps, not suspend→resume events
=================================================
An earlier version measured T = suspend-event → next-resume-event from
WAREHOUSE_EVENTS_HISTORY.  That observable is T = G − AS₀ (shifted by
whatever AUTO_SUSPEND was in effect during observation) and exists only
for gaps that exceeded AS₀ (censoring): a warehouse whose AUTO_SUSPEND
sits far above its real gaps never suspends, produces zero events, and
was invisible — precisely the warehouses that most need this
recommendation.  Both distortions bias the proposal downward.
Query-history gaps are the uncensored, unshifted quantity the cost model
is actually written in, and QUERY_HISTORY lags ~45 minutes vs the events
view's hours.

Events remain as **C-enrichment**: when resume STARTED→COMPLETED pairs
are available, C is measured (p95 resume duration plus the
60s-minimum-bill floor) instead of assumed from per-size defaults.  See
``_estimate_cold_start_cost``.

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
from snowtuner.ingestion.event_vocab import (
    RESUME_EVENT_NAMES,
    sql_in_list,
)
from snowtuner.recommenders.base import (
    ReadinessReport,
    Recommender,
    TrainingGate,
)


# Minimum modelable idle gaps per warehouse before we start recommending.
# 10 is enough for a coarse fit; the confidence score already down-weights
# small samples (see _confidence below).  Name kept from the events era
# for API stability - the unit is now gaps, not suspend/resume pairs.
MIN_CYCLES_PER_WAREHOUSE = 10
AUTO_SUSPEND_MIN = 60
AUTO_SUSPEND_MAX = 600
AUTO_SUSPEND_STEP = 5  # grid resolution in seconds
MIN_DELTA_SECONDS = 30

# Gaps shorter than the grid floor can never influence the optimum: for any
# candidate AS >= AUTO_SUSPEND_MIN > G the per-gap cost is the constant G
# (no suspend under any candidate).  Including them would only inflate n
# (and therefore confidence) and drag the displayed quantiles toward zero,
# so both the gate and the fit exclude them.
IDLE_GAP_FLOOR_SECONDS = AUTO_SUSPEND_MIN

# Resume STARTED->COMPLETED pairs required before a measured cold-start
# cost is trusted over the per-size default.
_MIN_RESUME_SAMPLES = 3


# Approximate cold-start cost per warehouse size (seconds of equivalent billed
# time).  Larger warehouses take meaningfully longer to resume.  These are
# the FALLBACK values - ``_estimate_cold_start_cost`` measures C from resume
# durations + the billing floor whenever the events data supports it.
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
    """Require enough modelable idle gaps for a stable survival curve.

    Reads ``features.warehouse_idle_gaps`` (query-history derived; run the
    feature pipeline after sync).  Suspend/resume events are deliberately
    NOT consulted here: a warehouse that never suspends still has gaps,
    and those are exactly the warehouses most in need of tuning.
    """

    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        rows = conn.execute(
            f"""
            SELECT warehouse_name, COUNT(*) AS n_gaps
            FROM features.warehouse_idle_gaps
            WHERE idle_seconds >= {IDLE_GAP_FLOOR_SECONDS}
            GROUP BY warehouse_name
            """
        ).fetchall()
        if not rows:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    "no idle gaps computed yet - run a sync and the "
                    "feature pipeline first"
                ),
                signals={"warehouses_with_gaps": 0},
            )
        ready = [w for w, c in rows if c >= MIN_CYCLES_PER_WAREHOUSE]
        if not ready:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    f"no warehouse has >={MIN_CYCLES_PER_WAREHOUSE} idle gaps "
                    f">= {IDLE_GAP_FLOOR_SECONDS}s yet; observed: {dict(rows)}"
                ),
                signals={"warehouses_with_gaps": len(rows)},
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
        """Fit the empirical survival curve and optimal-cost curve per warehouse.

        Gaps come from ``features.warehouse_idle_gaps`` (query-history
        derived - see the module docstring for why events are not the
        source).  Gaps below the grid floor are optimization-inert and
        excluded so they don't inflate n or distort the quantiles.
        """
        rows = conn.execute(
            f"""
            SELECT warehouse_name, idle_seconds
            FROM features.warehouse_idle_gaps
            WHERE idle_seconds >= {IDLE_GAP_FLOOR_SECONDS}
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
            C, c_source, c_detail = _estimate_cold_start_cost(conn, wh, size_key)
            fit = _fit_survival(gaps, cold_start_cost=C)
            fit["cold_start_cost_seconds"] = C
            fit["cold_start_cost_source"] = c_source
            fit["cold_start_cost_detail"] = c_detail
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
            c_verb = (
                "measured at"
                if fit.get("cold_start_cost_source") == "measured"
                else "assumed"
            )
            rationale = (
                f"Survival-based fit on {n} idle gaps between busy periods on {wh}. "
                f"Idle-gap quantiles: p25={q['p25']:.0f}s, p50={q['p50']:.0f}s, "
                f"p75={q['p75']:.0f}s.  Cold-start cost {c_verb} "
                f"{fit['cold_start_cost_seconds']:.0f}s. "
                f"Expected per-gap cost minimized at AUTO_SUSPEND={proposed}s "
                f"(cost={fit['optimal_cost']:.1f}s)."
            )
            if cur_as is not None and cost_at_current is not None:
                rationale += (
                    f"  Current AUTO_SUSPEND={int(cur_as)}s has expected per-gap cost "
                    f"{cost_at_current:.1f}s → saving ~{cost_saved_per_cycle:.1f}s per gap."
                )

            evidence = [
                EvidenceRef(
                    kind="warehouse_idle_gaps",
                    description=f"{n} idle gaps observed between busy periods",
                    metric="n_gaps",
                    value=float(n),
                ),
                EvidenceRef(
                    kind="survival_curve",
                    description="Idle-gap survival curve (empirical)",
                    metric="p50_seconds",
                    value=q["p50"],
                ),
                EvidenceRef(
                    kind="cost_curve",
                    description="Expected per-gap cost minimum",
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
                        f"based on {n} observed idle gaps; cold-start cost "
                        f"{fit['cold_start_cost_seconds']:.0f}s "
                        f"({fit.get('cold_start_cost_source', 'default')})"
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
    """Modelable idle gaps per day - scales the credits/day impact estimate.

    Counts the same gap population the fit uses (>= floor), over the span
    of observed history.  Only affects the impact figure, never the
    recommend/skip decision.
    """
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS n_gaps,
               date_diff('day', MIN(gap_start), MAX(gap_end)) AS days
        FROM features.warehouse_idle_gaps
        WHERE warehouse_name = ?
          AND idle_seconds >= {IDLE_GAP_FLOOR_SECONDS}
        """,
        [warehouse_name],
    ).fetchone()
    if not row or not row[0]:
        return 0.0
    return row[0] / max(row[1] or 0, 1)


def _estimate_cold_start_cost(
    conn: duckdb.DuckDBPyConnection, warehouse_name: str, size_key: str,
) -> tuple[float, str, dict[str, Any]]:
    """C-enrichment: measure the cold-start cost from events when possible.

        C = p95(resume provisioning seconds)          [RESUME STARTED->COMPLETED]
          + max(0, 60 - median busy-interval seconds) [60s minimum bill per resume]

    The first term is the resume latency the user actually experiences on
    this account; the second is the expected billing-floor waste when
    bursts run shorter than Snowflake's minimum billing increment.
    Cache-warmup cost is real but unmeasured here (later refinement).

    Falls back to the per-size defaults when fewer than
    ``_MIN_RESUME_SAMPLES`` measurable resume pairs exist - events still
    lagging, a warehouse that never suspends, or a vocabulary this module
    hasn't met yet.  The recommendation fires either way; only C's
    fidelity changes.

    Returns ``(C, source, detail)`` where source is 'measured' | 'default'.
    """
    default_c = COLD_START_COST_BY_SIZE.get(size_key, DEFAULT_COLD_START_COST)
    try:
        pairs = conn.execute(
            f"""
            WITH resumes AS (
                SELECT timestamp, event_state,
                       LEAD(event_state) OVER (ORDER BY timestamp) AS next_state,
                       LEAD(timestamp)   OVER (ORDER BY timestamp) AS next_ts
                FROM raw.warehouse_events_history
                WHERE warehouse_name = ?
                  AND event_name IN ({sql_in_list(RESUME_EVENT_NAMES)})
            )
            SELECT date_diff('second', timestamp, next_ts) AS resume_seconds
            FROM resumes
            WHERE event_state = 'STARTED'
              AND next_state = 'COMPLETED'
              AND next_ts IS NOT NULL
            """,
            [warehouse_name],
        ).fetchall()
    except Exception:
        pairs = []
    durations = [
        float(r[0]) for r in pairs
        if r[0] is not None and 0 <= float(r[0]) <= 600
    ]
    if len(durations) < _MIN_RESUME_SAMPLES:
        return default_c, "default", {"resume_samples": len(durations)}

    p95_resume = float(np.percentile(np.asarray(durations), 95))
    row = conn.execute(
        """
        SELECT median(duration_sec)
        FROM features.warehouse_active_intervals
        WHERE warehouse_name = ?
        """,
        [warehouse_name],
    ).fetchone()
    median_busy = float(row[0]) if row and row[0] is not None else None
    billing_floor = (
        max(0.0, 60.0 - median_busy) if median_busy is not None else 0.0
    )
    # Clamp to a sane band: sub-2s C makes the optimizer hyper-aggressive
    # on noise; >300s means something upstream is mismeasured.
    c = float(min(max(p95_resume + billing_floor, 2.0), 300.0))
    detail = {
        "resume_samples": len(durations),
        "p95_resume_seconds": round(p95_resume, 1),
        "billing_floor_seconds": round(billing_floor, 1),
        "median_busy_seconds": (
            round(median_busy, 1) if median_busy is not None else None
        ),
    }
    return c, "measured", detail


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
