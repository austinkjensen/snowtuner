"""QAS on/off candidate-finder recommender.

What this is
------------
A "candidate finder" — same shape as ``gen2_candidate_finder``.  Query
Acceleration Service (QAS) charges credits per offloaded query in addition
to the warehouse's compute; whether it nets out positive depends entirely
on the workload's character.  From passive QUERY_HISTORY data we cannot
predict the actual cost/benefit ratio, so this recommender's job is to
**flag warehouses whose workload shape suggests QAS behavior would change
meaningfully** — in either direction — and propose the ``qas_on_off``
experiment for each.  The experiment's measured deltas then drive the
final ``Recommendation`` via ``experiments/derive.py``.

What we flag
------------
Two cohorts get flagged:

1. **QAS currently OFF + workload looks like QAS could help.**
   Signals: heavy scan-bound queries, remote spill, sustained queue
   overload.  These suggest the warehouse is starved for burst capacity
   and QAS might absorb that load.

2. **QAS currently ON + workload looks like QAS isn't paying off.**
   Signals: low average scan, no spill, no queue overload.  QAS's
   per-query surcharge might exceed any latency benefit.

Both cohorts get the same ``qas_on_off`` experiment recipe (which flips
whatever the current state is), but with different rationale strings so
the operator knows whether they're testing "should we turn it on?" or
"should we turn it off?".

What we don't flag
------------------
* Warehouses where ``qas_state`` is NULL (older Snowflake, lookup failure,
  Standard edition where QAS isn't available).  Same conservative posture
  as the Gen2 finder — skip rather than guess.
* Warehouses with no sustained credit consumption (same threshold as the
  Gen2 finder).
* Warehouses with no queries in the window.
"""
from __future__ import annotations

from typing import Any

import duckdb

from snowtuner.actions.base import ActionType
from snowtuner.experiments.cost_estimate import QueryStats
from snowtuner.experiments.eligibility import AccountInfo
from snowtuner.experiments.axes import QASState
from snowtuner.experiments.config_delta import WarehouseConfig
from snowtuner.experiments.model import ProposedExperiment
from snowtuner.experiments.recipes import qas_on_off
from snowtuner.recommendations.model import Recommendation
from snowtuner.recommenders.base import AlwaysReadyGate, Recommender


# Tunables — kept aligned with the Gen2 finder where the meaning carries
# over (credit-consumption gate, sample size for cost estimator).
_LOOKBACK_DAYS = 14
_MIN_CREDITS_PER_WEEK = 20.0

# "Heavy scan" — average bytes_scanned per query.  100 MB is the
# rule-of-thumb where QAS scan-offload starts to have something to bite
# into.  Below ~10 MB the cloud-services overhead dominates.
_MIN_AVG_SCAN_BYTES_FOR_QAS_ON = 100 * 1024 * 1024     # 100 MB

# "Low scan" threshold — when QAS is currently ON and average scan is
# under this, QAS's per-query surcharge may not be earning its keep.
_LOW_AVG_SCAN_BYTES_FOR_QAS_OFF = 10 * 1024 * 1024     # 10 MB

# Queue-overload time over the window that suggests the warehouse is
# concurrency-bound.  60 seconds total over 14 days is a low bar; tune
# upward if false-positives are noisy.
_MIN_TOTAL_QUEUE_OVERLOAD_MS = 60_000

_MAX_PROPOSALS_PER_RUN = 3
_COST_ESTIMATE_SAMPLE_SIZE = 30


class QASCandidateFinder(Recommender):
    """Finds warehouses where toggling QAS is worth measuring."""

    name = "qas_candidate_finder"
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
        return []

    def propose_experiments(
        self,
        conn: duckdb.DuckDBPyConnection,
        model_state: dict[str, Any] | None,
    ) -> list[ProposedExperiment]:
        candidates = _score_candidates(conn)
        eligible = [c for c in candidates if _passes_gates(c)]
        # Rank by credit consumption (highest = most upside either direction).
        eligible.sort(key=lambda c: c.credits_per_week, reverse=True)
        eligible = eligible[:_MAX_PROPOSALS_PER_RUN]
        if not eligible:
            return []

        # AccountInfo defaults; the qas_on_off recipe's own
        # ``account.qas_available`` check is the real edition gate (returns
        # None if not Enterprise+).
        account = AccountInfo()

        proposals: list[ProposedExperiment] = []
        for cand in eligible:
            warehouse_config = _load_warehouse_config(conn, cand.warehouse_name)
            if warehouse_config is None:
                continue
            sample_stats = _sample_query_stats(
                conn, cand.warehouse_name, limit=_COST_ESTIMATE_SAMPLE_SIZE,
            )
            proposed = qas_on_off(
                warehouse_config, account, sample_query_stats=sample_stats,
            )
            if proposed is None:
                continue
            # Enrich the hypothesis with the candidacy rationale so the UI
            # surfaces *why* this warehouse was flagged AND which direction
            # we expect to be the interesting one.
            proposed.hypothesis = (
                f"{cand.rationale}\n\n"
                f"Recipe rationale: {proposed.hypothesis}"
            )
            proposed.proposed_by = f"recommender:{self.name}@{self.version}"
            proposals.append(proposed)
        return proposals


# ── candidate scoring ──────────────────────────────────────────────


class _Candidate:
    """A single warehouse's QAS-candidacy signals + a rendered rationale."""

    def __init__(
        self,
        *,
        warehouse_name: str,
        qas_state: str | None,
        credits_per_week: float,
        avg_scan_bytes: float,
        remote_spill_query_count: int,
        total_queue_overload_ms: int,
        total_queries: int,
    ) -> None:
        self.warehouse_name = warehouse_name
        self.qas_state = qas_state
        self.credits_per_week = credits_per_week
        self.avg_scan_bytes = avg_scan_bytes
        self.remote_spill_query_count = remote_spill_query_count
        self.total_queue_overload_ms = total_queue_overload_ms
        self.total_queries = total_queries

    @property
    def has_positive_signal(self) -> bool:
        """Workload looks like QAS could help (turn ON candidate)."""
        return (
            self.avg_scan_bytes >= _MIN_AVG_SCAN_BYTES_FOR_QAS_ON
            or self.remote_spill_query_count > 0
            or self.total_queue_overload_ms >= _MIN_TOTAL_QUEUE_OVERLOAD_MS
        )

    @property
    def has_negative_signal(self) -> bool:
        """Workload looks like QAS isn't earning its keep (turn OFF candidate)."""
        return (
            self.avg_scan_bytes < _LOW_AVG_SCAN_BYTES_FOR_QAS_OFF
            and self.remote_spill_query_count == 0
            and self.total_queue_overload_ms < _MIN_TOTAL_QUEUE_OVERLOAD_MS
        )

    @property
    def rationale(self) -> str:
        """Plain-English explanation tuned to whether we'd be testing
        QAS-on or QAS-off as the more interesting direction."""
        gb = self.avg_scan_bytes / (1024 * 1024 * 1024)
        queue_min = self.total_queue_overload_ms / 60_000
        common = (
            f"QAS candidate for warehouse {self.warehouse_name} (currently "
            f"{self.qas_state or 'unknown'}).  Over the last {_LOOKBACK_DAYS} "
            f"days: {self.total_queries} queries, sustained "
            f"~{self.credits_per_week:.1f} credits/week, avg scan "
            f"{gb:.2f} GB/query, {self.remote_spill_query_count} queries spilled "
            f"to remote storage, total queue overload {queue_min:.1f} minutes."
        )
        if self.qas_state == "off" and self.has_positive_signal:
            return (
                f"{common}\n\nDirection: **flip QAS ON**.  The workload has "
                f"the signals (heavy scan / spill / queueing) that typically "
                f"benefit from QAS's serverless burst capacity.  The "
                f"experiment will measure whether the speedup justifies "
                f"QAS's per-query credit surcharge."
            )
        if self.qas_state == "on" and self.has_negative_signal:
            return (
                f"{common}\n\nDirection: **flip QAS OFF**.  The workload "
                f"lacks the signals (no spill, no queue pressure, low avg "
                f"scan) that typically motivate keeping QAS enabled.  The "
                f"experiment will measure whether disabling it saves "
                f"credits without unacceptable latency regression."
            )
        # Mixed signal — keep the rationale honest.  Recipe still measures
        # both directions; we just can't predict which is more interesting.
        return (
            f"{common}\n\nDirection: **measure both ways**.  The signals "
            f"are mixed — the experiment will tell us whether QAS's current "
            f"state is the right one."
        )


def _score_candidates(conn: duckdb.DuckDBPyConnection) -> list[_Candidate]:
    """Compute signals for every warehouse whose QAS state we know."""
    rows = conn.execute(
        """
        SELECT name, qas_state FROM raw.warehouses
        WHERE qas_state IN ('on', 'off')
        ORDER BY name
        """
    ).fetchall()
    if not rows:
        return []

    out: list[_Candidate] = []
    for name, qas_state in rows:
        signal_row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_queries,
                COALESCE(AVG(bytes_scanned), 0) AS avg_scan,
                COALESCE(SUM(CASE WHEN bytes_spilled_to_remote > 0 THEN 1 ELSE 0 END), 0)
                    AS remote_spill_count,
                COALESCE(SUM(queued_overload_ms), 0) AS total_queue_ms
            FROM raw.query_history
            WHERE upper(warehouse_name) = upper(?)
              AND start_time >= now() - INTERVAL {_LOOKBACK_DAYS} DAYS
              AND execution_status = 'SUCCESS'
            """,
            [name],
        ).fetchone()
        if signal_row is None:
            continue
        total_queries, avg_scan, remote_spill, queue_ms = signal_row
        total_queries = int(total_queries or 0)
        if total_queries == 0:
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

        out.append(_Candidate(
            warehouse_name=name,
            qas_state=qas_state,
            credits_per_week=credits_per_week,
            avg_scan_bytes=float(avg_scan or 0),
            remote_spill_query_count=int(remote_spill or 0),
            total_queue_overload_ms=int(queue_ms or 0),
            total_queries=total_queries,
        ))
    return out


def _passes_gates(c: _Candidate) -> bool:
    """A warehouse qualifies if:

      * sustained credit consumption ≥ threshold (worth the experiment cost), AND
      * the current state has a clear direction worth measuring:
        - currently OFF + positive signal (might want ON), or
        - currently ON + negative signal (might want OFF), or
        - mixed signals (still worth flipping to measure)

    The third clause is intentionally liberal — when in doubt, an
    experiment costs little and produces actual evidence.  If this
    creates noise we'll tighten it.
    """
    if c.credits_per_week < _MIN_CREDITS_PER_WEEK:
        return False
    if c.qas_state not in ("on", "off"):
        return False
    return (
        (c.qas_state == "off" and c.has_positive_signal)
        or (c.qas_state == "on" and c.has_negative_signal)
        or (c.has_positive_signal and c.has_negative_signal)
    )


# ── helpers ────────────────────────────────────────────────────────


def _load_warehouse_config(
    conn: duckdb.DuckDBPyConnection, warehouse_name: str,
) -> WarehouseConfig | None:
    """Load a WarehouseConfig from raw.warehouses with QAS state filled in.

    Unlike the Gen2 finder's local loader, this one DOES need to pass
    ``qas_state`` through, because the qas_on_off recipe reads
    ``warehouse.qas_state`` to decide which direction to flip.
    """
    row = conn.execute(
        """
        SELECT name, size, auto_suspend_seconds, auto_resume,
               generation, qas_state, qas_max_scale_factor
        FROM raw.warehouses
        WHERE upper(name) = upper(?)
        """,
        [warehouse_name],
    ).fetchone()
    if not row:
        return None
    qas_state = None
    if row[5] == "on":
        qas_state = QASState.ON
    elif row[5] == "off":
        qas_state = QASState.OFF
    return WarehouseConfig(
        name=row[0],
        size=row[1],
        auto_suspend_seconds=row[2],
        auto_resume=bool(row[3]) if row[3] is not None else None,
        generation=None,
        qas_state=qas_state,
        qas_max_scale_factor=row[6],
    )


def _sample_query_stats(
    conn: duckdb.DuckDBPyConnection,
    warehouse_name: str,
    *,
    limit: int,
) -> list[QueryStats]:
    """Pull SELECT-only success-status queries for the recipe's cost estimator.
    Mirrors the helper in ``gen2_candidate_finder`` — same shape, same
    purpose."""
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
