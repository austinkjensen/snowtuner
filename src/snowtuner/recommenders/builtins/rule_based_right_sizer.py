"""RuleBasedRightSizer — transparent thresholds for ALTER WAREHOUSE WAREHOUSE_SIZE.

Reads recent query_history aggregates per warehouse and applies a small fixed
ladder of rules to decide whether to upsize, downsize, or do nothing.  Output
is one ``AlterWarehouse`` action per recommendation.

Rules (evaluated top-down, first match wins per warehouse):
  1. Any query spilled to remote storage in the window → +1 size
       (Remote spill is much slower than local; memory at the current size
        was clearly insufficient.)
  2. ≥20% of queries spilled to local storage → +1 size
       (Local spill is tolerable but expensive in latency; sustained means
        the workload routinely runs out of memory.)
  3. Mean queue-overload time ≥ 5s AND ≥ 30 queries observed → +1 size
       (Queries are queueing waiting for compute; the warehouse is saturated.)
  4. p99 elapsed_ms ≤ 1s AND ≥ 100 queries AND zero spills AND zero queueing
     → −1 size (overprovisioned for the workload).

If no rule fires, no recommendation is emitted.

Window: last 14 days.  Only SUCCESS queries are considered.
"""
from __future__ import annotations

from typing import Any

import duckdb

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
from snowtuner.recommenders.sizes import credit_rate, normalize, step

WINDOW_DAYS = 14

# Thresholds.  Module-level so they're easy to tune from one place.
LOCAL_SPILL_FRAC_TO_UPSIZE = 0.20
MIN_QUERIES_FOR_QUEUEING_RULE = 30
QUEUE_OVERLOAD_MS_TO_UPSIZE = 5_000
MIN_QUERIES_FOR_DOWNSIZE = 100
DOWNSIZE_P99_MS = 1_000
MIN_QUERIES_FOR_READINESS = 30


class RuleBasedRightSizerGate(TrainingGate):
    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        rows = conn.execute(
            f"""
            SELECT warehouse_name, COUNT(*) AS n
            FROM raw.query_history
            WHERE start_time >= now() - INTERVAL {WINDOW_DAYS} DAY
              AND warehouse_name IS NOT NULL
              AND execution_status = 'SUCCESS'
            GROUP BY warehouse_name
            """
        ).fetchall()
        if not rows:
            return ReadinessReport(
                is_ready=False,
                reason="no warehouse activity in the last "
                       f"{WINDOW_DAYS} days",
                signals={"warehouses_with_activity": 0},
            )
        ready = [w for w, n in rows if n >= MIN_QUERIES_FOR_READINESS]
        if not ready:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    f"no warehouse has ≥{MIN_QUERIES_FOR_READINESS} queries in the "
                    f"last {WINDOW_DAYS} days; observed: {dict(rows)}"
                ),
                signals={"warehouses_with_activity": len(rows)},
            )
        return ReadinessReport(
            is_ready=True,
            reason=f"{len(ready)} warehouse(s) have enough queries to evaluate",
            signals={"ready_warehouses": ready},
        )


class RuleBasedRightSizer(Recommender):
    name = "rule_based_right_sizer"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE
    required_feature_tables: set[str] = set()  # reads raw.* directly
    training_gate = RuleBasedRightSizerGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        # No learned state; the rules are constants.  We still record the
        # current per-warehouse summary so the UI/CLI can show "what was the
        # most-recently-observed picture this recommender saw."
        rows = conn.execute(_AGG_SQL).fetchall()
        per_wh: dict[str, dict[str, Any]] = {}
        for r in rows:
            (
                wh, current_size, n_queries, n_remote, n_local, avg_queue_ms,
                p99_ms, total_remote_bytes, total_local_bytes,
            ) = r
            per_wh[wh] = {
                "current_size": current_size,
                "n_queries": n_queries,
                "n_remote_spill": n_remote,
                "n_local_spill": n_local,
                "avg_queue_ms": float(avg_queue_ms or 0.0),
                "p99_elapsed_ms": float(p99_ms or 0.0),
                "total_remote_spill_bytes": int(total_remote_bytes or 0),
                "total_local_spill_bytes": int(total_local_bytes or 0),
            }
        return {"per_warehouse": per_wh, "window_days": WINDOW_DAYS}

    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        per_wh = (model_state or {}).get("per_warehouse") or {}
        out: list[Recommendation] = []
        for wh, m in per_wh.items():
            current_size = normalize(m.get("current_size"))
            if current_size is None:
                continue  # unknown size — skip rather than guess
            n = int(m["n_queries"])
            if n < MIN_QUERIES_FOR_READINESS:
                continue

            decision = _decide(m)
            if decision is None:
                continue
            new_size = step(current_size, decision.delta)
            if new_size is None:
                continue  # at the ladder edge
            if new_size == current_size:
                continue  # shouldn't happen, but guard

            credits_delta_daily = _estimate_credits_delta_daily(
                conn, wh, current_size, new_size,
            )

            evidence = [
                EvidenceRef(
                    kind="query_history",
                    description=f"{n:,} queries observed in last {WINDOW_DAYS} days",
                    metric="n_queries",
                    value=float(n),
                ),
                EvidenceRef(
                    kind="query_history",
                    description=decision.evidence_description,
                    metric=decision.evidence_metric,
                    value=decision.evidence_value,
                ),
            ]

            action = AlterWarehouse(
                warehouse_name=wh,
                changes=[KnobChange(
                    knob=WarehouseKnob.WAREHOUSE_SIZE,
                    current_value=current_size,
                    proposed_value=new_size,
                )],
            )
            out.append(Recommendation(
                generated_by=self.generated_by,
                action=action,
                rationale=decision.rationale,
                evidence=evidence,
                expected_impact=Impact(
                    credits_delta_daily=credits_delta_daily,
                    confidence=_confidence(n, decision.delta),
                    notes=decision.impact_notes,
                ),
            ))
        return out


# ---------------------------------------------------------------------------
# Rule machinery
# ---------------------------------------------------------------------------

class _Decision:
    __slots__ = (
        "delta", "rationale", "evidence_description", "evidence_metric",
        "evidence_value", "impact_notes",
    )

    def __init__(
        self, delta: int, rationale: str,
        evidence_description: str, evidence_metric: str, evidence_value: float,
        impact_notes: str,
    ):
        self.delta = delta
        self.rationale = rationale
        self.evidence_description = evidence_description
        self.evidence_metric = evidence_metric
        self.evidence_value = evidence_value
        self.impact_notes = impact_notes


def _decide(m: dict[str, Any]) -> _Decision | None:
    n = int(m["n_queries"])
    n_remote = int(m["n_remote_spill"])
    n_local = int(m["n_local_spill"])
    avg_queue_ms = float(m["avg_queue_ms"])
    p99_ms = float(m["p99_elapsed_ms"])

    # Rule 1: any remote spill → upsize.
    if n_remote > 0:
        return _Decision(
            delta=+1,
            rationale=(
                f"{n_remote} of {n} queries spilled to remote storage in the last "
                f"{WINDOW_DAYS} days.  Remote spill is much slower than running on "
                f"a warehouse with adequate memory; upsizing typically eliminates "
                f"the spill and recovers the latency."
            ),
            evidence_description=f"{n_remote} queries spilled to remote storage",
            evidence_metric="n_remote_spill",
            evidence_value=float(n_remote),
            impact_notes=(
                f"based on {n} queries; spill ratio {n_remote / n:.1%}"
            ),
        )

    # Rule 2: significant local spill → upsize.
    if n > 0 and (n_local / n) >= LOCAL_SPILL_FRAC_TO_UPSIZE:
        return _Decision(
            delta=+1,
            rationale=(
                f"{n_local / n:.0%} of queries ({n_local}/{n}) spilled to local storage "
                f"over the last {WINDOW_DAYS} days, suggesting the warehouse is routinely "
                f"running out of memory.  Upsizing should eliminate the spill and improve "
                f"latency."
            ),
            evidence_description=f"{n_local}/{n} queries spilled to local",
            evidence_metric="local_spill_fraction",
            evidence_value=float(n_local / n),
            impact_notes=f"local spill fraction {n_local / n:.1%}",
        )

    # Rule 3: queueing → upsize.
    if (
        avg_queue_ms >= QUEUE_OVERLOAD_MS_TO_UPSIZE
        and n >= MIN_QUERIES_FOR_QUEUEING_RULE
    ):
        return _Decision(
            delta=+1,
            rationale=(
                f"Queries spent {avg_queue_ms / 1000:.1f}s on average waiting for compute "
                f"({n} queries observed).  This usually indicates the warehouse is "
                f"under-resourced; upsizing or adding clusters reduces queueing.  "
                f"(Multi-cluster scaling will be considered in a future release.)"
            ),
            evidence_description=f"average queue-overload {avg_queue_ms / 1000:.1f}s",
            evidence_metric="avg_queue_overload_ms",
            evidence_value=avg_queue_ms,
            impact_notes=f"observed across {n} queries",
        )

    # Rule 4: overprovisioned → downsize.
    if (
        n >= MIN_QUERIES_FOR_DOWNSIZE
        and p99_ms <= DOWNSIZE_P99_MS
        and n_local == 0
        and n_remote == 0
        and avg_queue_ms < 1_000
    ):
        return _Decision(
            delta=-1,
            rationale=(
                f"99% of queries finished within {p99_ms:.0f}ms over the last {WINDOW_DAYS} "
                f"days, with no spills and no queueing.  The current size is overkill "
                f"for the observed workload; downsizing roughly halves the credit rate."
            ),
            evidence_description=f"p99 elapsed = {p99_ms:.0f}ms with no spills/queueing",
            evidence_metric="p99_elapsed_ms",
            evidence_value=p99_ms,
            impact_notes=f"based on {n} queries",
        )

    return None


def _estimate_credits_delta_daily(
    conn: duckdb.DuckDBPyConnection,
    warehouse_name: str,
    current_size: str,
    new_size: str,
) -> float:
    """Project credit/day delta if the warehouse had been at *new_size* over the window.

    Uses observed credits_used per hour from warehouse_metering_history,
    scaled linearly by the credit-rate ratio (Snowflake doubles credits per
    step up the ladder, halves per step down).
    """
    row = conn.execute(
        f"""
        SELECT SUM(credits_used) / GREATEST(1, date_diff('day', MIN(start_time),
                                              MAX(start_time))) AS credits_per_day
        FROM raw.warehouse_metering_history
        WHERE warehouse_name = ?
          AND start_time >= now() - INTERVAL {WINDOW_DAYS} DAY
        """,
        [warehouse_name],
    ).fetchone()
    if not row or row[0] is None:
        return 0.0
    observed_credits_per_day = float(row[0])
    ratio = credit_rate(new_size) / credit_rate(current_size)
    new_credits_per_day = observed_credits_per_day * ratio
    return round(new_credits_per_day - observed_credits_per_day, 2)


def _confidence(n: int, delta: int) -> float:
    """Same shape as the auto_suspend confidence; scales with sample size.

    Slight asymmetric prior: downsizes get a small confidence haircut because
    they're easier to notice if wrong (queries get slower) but harder to validate
    in advance (we don't observe what would have happened at the smaller size).
    """
    if n <= 0:
        return 0.0
    base = 1.0 - 30.0 / (n + 30.0)  # 0.5 at n=30, 0.77 at n=100
    if delta < 0:
        base *= 0.85
    return min(1.0, base)


_AGG_SQL = f"""
SELECT
    qh.warehouse_name,
    w.size AS current_size,
    COUNT(*) AS n_queries,
    SUM(CASE WHEN qh.bytes_spilled_to_remote > 0 THEN 1 ELSE 0 END) AS n_remote_spill,
    SUM(CASE WHEN qh.bytes_spilled_to_local  > 0 THEN 1 ELSE 0 END) AS n_local_spill,
    AVG(qh.queued_overload_ms) AS avg_queue_ms,
    quantile_cont(qh.total_elapsed_ms, 0.99) AS p99_elapsed_ms,
    SUM(qh.bytes_spilled_to_remote) AS total_remote_spill_bytes,
    SUM(qh.bytes_spilled_to_local)  AS total_local_spill_bytes
FROM raw.query_history qh
LEFT JOIN raw.warehouses w ON UPPER(w.name) = UPPER(qh.warehouse_name)
WHERE qh.start_time >= now() - INTERVAL {WINDOW_DAYS} DAY
  AND qh.warehouse_name IS NOT NULL
  AND qh.execution_status = 'SUCCESS'
GROUP BY qh.warehouse_name, w.size
"""
