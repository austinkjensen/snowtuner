"""SpillAwareRightSizer — empirical memory-requirement model for warehouse sizing.

Alternative to RuleBasedRightSizer.  Not registered in default_registry; users
who want to A/B against the rule-based approach can swap it in by editing
``snowtuner/recommenders/registry.py``.

Approach
========
For each query that *did spill*, we estimate the memory it actually wanted:

    required_gb = warehouse_memory_gb + bytes_spilled_total / 2**30

Queries that didn't spill give us a lower bound (≤ warehouse_memory_gb), but
they don't tell us *how much less* memory they needed.  So we fit only on
spilling queries, take the p95 of required_gb across them, and pick the
smallest size whose memory budget covers it.  If no queries spilled at all,
we look at the inverse: was the current size dramatically more than needed?
That's where we'd downsize, but conservatively — the rule-based recommender
handles the obvious-overprovisioning case better, so we only emit downsizes
here when the rule-based path would also have downsized.

Compared to the rule-based recommender, this one:
  * Handles graduated spill — a single warehouse with many borderline-large
    queries that just-barely-spill won't trigger Rule 1's "any remote spill →
    upsize", but the p95 here will reveal whether they need 2x or 4x more
    memory.
  * Outputs a more nuanced rationale (the actual GB target) but the
    sizes/cost numbers are educated approximations.

Limitations
-----------
* `APPROX_MEMORY_GB` is community-observed, not officially published.
* Sizing also gives more parallelism, not just memory; this model ignores
  that, treating "spill" as the whole story.  That's why it's the
  *alternative*, not the default.
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
from snowtuner.recommenders.sizes import (
    APPROX_MEMORY_GB,
    SIZES,
    credit_rate,
    memory_gb,
    normalize,
    step,
)

WINDOW_DAYS = 14
MIN_QUERIES_FOR_READINESS = 50
TARGET_PERCENTILE = 95
MIN_DELTA_GB = 0.0  # always honor the model output if it picks a different size


class SpillAwareReadinessGate(TrainingGate):
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
                is_ready=False, reason="no warehouse activity in window"
            )
        ready = [w for w, n in rows if n >= MIN_QUERIES_FOR_READINESS]
        if not ready:
            return ReadinessReport(
                is_ready=False,
                reason=(
                    f"need ≥{MIN_QUERIES_FOR_READINESS} queries per warehouse; "
                    f"observed: {dict(rows)}"
                ),
            )
        return ReadinessReport(
            is_ready=True,
            reason=f"{len(ready)} warehouse(s) eligible",
            signals={"ready_warehouses": ready},
        )


class SpillAwareRightSizer(Recommender):
    name = "spill_aware_right_sizer"
    version = "0.1.0"
    action_type = ActionType.ALTER_WAREHOUSE
    required_feature_tables: set[str] = set()
    training_gate = SpillAwareReadinessGate()

    def fit(self, conn: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        rows = conn.execute(
            f"""
            SELECT
                qh.warehouse_name,
                w.size AS current_size,
                qh.bytes_spilled_to_local,
                qh.bytes_spilled_to_remote,
                qh.total_elapsed_ms,
                qh.queued_overload_ms
            FROM raw.query_history qh
            LEFT JOIN raw.warehouses w ON UPPER(w.name) = UPPER(qh.warehouse_name)
            WHERE qh.start_time >= now() - INTERVAL {WINDOW_DAYS} DAY
              AND qh.warehouse_name IS NOT NULL
              AND qh.execution_status = 'SUCCESS'
            """
        ).fetchall()

        per_wh: dict[str, dict[str, Any]] = {}
        for wh, current_size, spill_local, spill_remote, _, queued in rows:
            entry = per_wh.setdefault(
                wh,
                {
                    "current_size": current_size,
                    "n_queries": 0,
                    "required_gb_samples": [],
                    "queued_total_ms": 0,
                    "n_with_queue": 0,
                },
            )
            entry["n_queries"] += 1
            mem = memory_gb(current_size or "")
            spill = float((spill_local or 0) + (spill_remote or 0))
            if spill > 0:
                # Working set ~ resident memory + (spilled bytes converted to GB).
                required = mem + spill / (2 ** 30)
                entry["required_gb_samples"].append(required)
            if queued and queued > 0:
                entry["queued_total_ms"] += float(queued)
                entry["n_with_queue"] += 1
        return {"per_warehouse": per_wh, "window_days": WINDOW_DAYS}

    def predict(
        self, conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[Recommendation]:
        per_wh = (model_state or {}).get("per_warehouse") or {}
        out: list[Recommendation] = []
        for wh, m in per_wh.items():
            current_size = normalize(m.get("current_size"))
            if current_size is None:
                continue
            n = int(m["n_queries"])
            if n < MIN_QUERIES_FOR_READINESS:
                continue

            samples = m.get("required_gb_samples") or []
            n_spilled = len(samples)
            if n_spilled == 0:
                continue  # no spill signal → nothing to say with this approach

            arr = np.asarray(samples, dtype=float)
            target_gb = float(np.percentile(arr, TARGET_PERCENTILE))
            new_size = _smallest_size_covering(target_gb)
            if new_size is None or new_size == current_size:
                continue

            current_idx = SIZES.index(current_size)
            new_idx = SIZES.index(new_size)
            if abs(new_idx - current_idx) == 0:
                continue

            credits_delta_daily = _estimate_credits_delta_daily(
                conn, wh, current_size, new_size,
            )

            evidence = [
                EvidenceRef(
                    kind="query_history",
                    description=(
                        f"{n_spilled} of {n} queries spilled in last {WINDOW_DAYS} days"
                    ),
                    metric="n_spilled",
                    value=float(n_spilled),
                ),
                EvidenceRef(
                    kind="query_history",
                    description=(
                        f"p{TARGET_PERCENTILE} memory required: {target_gb:.1f} GB"
                    ),
                    metric="target_memory_gb",
                    value=target_gb,
                ),
            ]

            direction = "upsize" if new_idx > current_idx else "downsize"
            rationale = (
                f"At p{TARGET_PERCENTILE}, queries on {wh} appear to need ~{target_gb:.1f} GB "
                f"of memory.  The current size ({current_size}, ~{memory_gb(current_size):.0f} GB) "
                f"is below that for {n_spilled} of {n} queries — they spilled.  "
                f"{new_size} (~{memory_gb(new_size):.0f} GB) covers p{TARGET_PERCENTILE} of the "
                f"observed working sets, eliminating the spill."
                if direction == "upsize"
                else
                f"At p{TARGET_PERCENTILE}, queries on {wh} need ~{target_gb:.1f} GB — well below "
                f"the current size ({current_size}, ~{memory_gb(current_size):.0f} GB).  "
                f"{new_size} (~{memory_gb(new_size):.0f} GB) is the smallest size that still "
                f"comfortably covers the workload."
            )

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
                rationale=rationale,
                evidence=evidence,
                expected_impact=Impact(
                    credits_delta_daily=credits_delta_daily,
                    confidence=_confidence(n_spilled),
                    notes=(
                        f"based on {n_spilled} spilled queries; memory targets are "
                        f"approximations (community-observed values for each size class)"
                    ),
                ),
            ))
        return out


def _smallest_size_covering(target_gb: float) -> str | None:
    for s in SIZES:
        if APPROX_MEMORY_GB[s] >= target_gb:
            return s
    return SIZES[-1]


def _estimate_credits_delta_daily(
    conn: duckdb.DuckDBPyConnection,
    warehouse_name: str,
    current_size: str,
    new_size: str,
) -> float:
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
    observed = float(row[0])
    ratio = credit_rate(new_size) / credit_rate(current_size)
    return round(observed * ratio - observed, 2)


def _confidence(n_spilled: int) -> float:
    if n_spilled <= 0:
        return 0.0
    return min(1.0, 1.0 - 10.0 / (n_spilled + 10.0))
