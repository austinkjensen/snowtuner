"""AutoSuspendTuner — recommends AUTO_SUSPEND for each warehouse.

Heuristic v1 (deliberately simple + explainable):

  For each warehouse:
    1. Training: collect observed idle-gap durations from features.warehouse_idle_gaps
       AND warehouse re-activation gaps (gap between SUSPEND_WAREHOUSE and the next
       RESUME_WAREHOUSE).  We treat re-activation gaps as the "true cost" of waking
       up, and observed idle-gaps as the opportunity to suspend sooner.
    2. Inference: choose AUTO_SUSPEND = p25(re_activation_gaps), clamped to
       [60, 600] seconds.  Rationale: if a quarter of suspend→resume cycles
       happen within N seconds, that's the floor below which dropping auto_suspend
       will trigger expensive cold-starts.
    3. Only emit a recommendation if the proposed value differs from the current
       warehouse setting by more than 30s (reduces noise).

Training gate: at least N distinct (suspend, resume) cycles per warehouse.

This heuristic is a placeholder for a proper model.  The point of the scaffold
is that swapping in a better one only requires replacing this file.
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


MIN_CYCLES_PER_WAREHOUSE = 20
AUTO_SUSPEND_MIN = 60
AUTO_SUSPEND_MAX = 600
MIN_DELTA_SECONDS = 30


class AutoSuspendReadinessGate(TrainingGate):
    def evaluate(self, conn: duckdb.DuckDBPyConnection) -> ReadinessReport:
        row = conn.execute(
            """
            SELECT warehouse_name, COUNT(*) AS cycle_count
            FROM raw.warehouse_events_history
            WHERE event_name IN ('SUSPEND_WAREHOUSE', 'RESUME_WAREHOUSE')
            GROUP BY warehouse_name
            """
        ).fetchall()
        if not row:
            return ReadinessReport(
                is_ready=False,
                reason="no warehouse events ingested yet",
                signals={"warehouses_with_events": 0},
            )
        ready_warehouses = [w for w, c in row if c >= MIN_CYCLES_PER_WAREHOUSE * 2]
        if not ready_warehouses:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    f"no warehouse has ≥{MIN_CYCLES_PER_WAREHOUSE} suspend/resume cycles yet; "
                    f"observed: {dict(row)}"
                ),
                signals={"warehouses_with_events": len(row)},
            )
        return ReadinessReport(
            is_ready=True,
            reason=f"{len(ready_warehouses)} warehouse(s) have enough history",
            signals={"ready_warehouses": ready_warehouses},
        )


class AutoSuspendTuner(Recommender):
    name = "auto_suspend_tuner"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE
    required_feature_tables = {"features.warehouse_idle_gaps"}
    training_gate = AutoSuspendReadinessGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        """Cache per-warehouse distributions of re-activation gaps."""
        rows = conn.execute(
            """
            WITH evts AS (
                SELECT warehouse_name, event_name, timestamp,
                       LEAD(event_name) OVER (PARTITION BY warehouse_name ORDER BY timestamp) AS next_name,
                       LEAD(timestamp) OVER (PARTITION BY warehouse_name ORDER BY timestamp) AS next_ts
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

        per_wh: dict[str, list[float]] = {}
        for wh, gap in rows:
            if gap is None:
                continue
            per_wh.setdefault(wh, []).append(float(gap))

        state: dict[str, Any] = {}
        for wh, gaps in per_wh.items():
            if len(gaps) < MIN_CYCLES_PER_WAREHOUSE:
                continue
            arr = np.asarray(gaps)
            state[wh] = {
                "n": int(arr.size),
                "p25": float(np.percentile(arr, 25)),
                "p50": float(np.percentile(arr, 50)),
                "p75": float(np.percentile(arr, 75)),
                "mean": float(arr.mean()),
            }
        return {"per_warehouse": state}

    def predict(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        state = (model_state or {}).get("per_warehouse") or {}
        if not state:
            return []

        current = {
            row[0]: {"auto_suspend_seconds": row[1], "size": row[2]}
            for row in conn.execute(
                "SELECT name, auto_suspend_seconds, size FROM raw.warehouses"
            ).fetchall()
        }

        recs: list[Recommendation] = []
        for wh, stats in state.items():
            cur_row = current.get(wh) or current.get(wh.upper())
            current_as = cur_row["auto_suspend_seconds"] if cur_row else None
            proposed = int(np.clip(round(stats["p25"]), AUTO_SUSPEND_MIN, AUTO_SUSPEND_MAX))

            if current_as is not None and abs(proposed - int(current_as)) < MIN_DELTA_SECONDS:
                continue

            # Rough impact estimate: if we're lowering auto_suspend by K seconds
            # per suspend cycle, and we see ~cycles_per_day cycles daily, that's
            # K * cycles_per_day seconds of saved billed idle time.  Convert to
            # credits using the warehouse size floor (XS = 1 credit/hour).
            cycles_per_day = _estimate_cycles_per_day(conn, wh)
            seconds_saved_daily = (
                max(0, int(current_as or AUTO_SUSPEND_MAX) - proposed) * cycles_per_day
            )
            credit_rate = _credit_rate_for_size((cur_row or {}).get("size"))
            credits_delta_daily = -round(
                (seconds_saved_daily / 3600.0) * credit_rate, 2
            )

            evidence = [
                EvidenceRef(
                    kind="warehouse_events",
                    description=f"{stats['n']} suspend→resume cycles observed",
                    metric="reactivation_gap_p25_seconds",
                    value=stats["p25"],
                ),
                EvidenceRef(
                    kind="warehouse_events",
                    description="Reactivation gap distribution",
                    filters={"warehouse_name": wh},
                    metric="reactivation_gap_p50_seconds",
                    value=stats["p50"],
                ),
            ]

            rationale = (
                f"25% of suspend→resume cycles on {wh} happen within {stats['p25']:.0f}s. "
                f"Setting AUTO_SUSPEND = {proposed}s captures the idle time below that floor "
                f"without forcing extra cold-starts."
            )
            if current_as is not None:
                rationale += f" Current AUTO_SUSPEND is {current_as}s."

            action = AlterWarehouse(
                warehouse_name=wh,
                changes=[KnobChange(
                    knob=WarehouseKnob.AUTO_SUSPEND,
                    current_value=int(current_as) if current_as is not None else None,
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
                    confidence=_confidence_from_n(stats["n"]),
                    notes=f"based on {stats['n']} observed cycles",
                ),
            ))
        return recs


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
    cycles, days = row
    return cycles / max(days, 1)


def _credit_rate_for_size(size: str | None) -> float:
    """Snowflake warehouse credit rates (credits/hour)."""
    if not size:
        return 1.0
    s = size.upper().replace("-", "").replace(" ", "")
    return {
        "XSMALL": 1, "SMALL": 2, "MEDIUM": 4, "LARGE": 8, "XLARGE": 16,
        "2XLARGE": 32, "3XLARGE": 64, "4XLARGE": 128, "5XLARGE": 256,
        "6XLARGE": 512,
    }.get(s, 1.0)


def _confidence_from_n(n: int) -> float:
    """Monotonic, bounded confidence: ~0.5 at n=20, approaches 1 as n grows."""
    if n <= 0:
        return 0.0
    return min(1.0, 1.0 - 10.0 / (n + 10.0))
