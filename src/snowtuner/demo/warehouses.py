"""Static specs for the 6 cooked demo warehouses.

Each spec pairs a Snowflake warehouse config (size, auto-suspend) with a
workload key that points into ``demo/workloads.py``.  The runner provisions
one warehouse per spec, runs its workload, and tears down at the end (or on
``snowtuner demo teardown``).

Naming:  all demo warehouses start with ``SNOWTUNER_DEMO_`` so teardown is
a single ``SHOW WAREHOUSES LIKE 'SNOWTUNER_DEMO_%'`` sweep.  We never
collide with the user's real warehouses by accident.

Sizing decisions are paired with the workload to deliberately trip a
specific recommender rule.  See ``expected_finding`` for the mapping.  If
you change a spec's size or auto-suspend, double-check the workload still
trips the intended rule on a real account - the thresholds live in
``recommenders/builtins/rule_based_right_sizer.py`` and
``recommenders/builtins/auto_suspend_survival.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

DEMO_WAREHOUSE_PREFIX = "SNOWTUNER_DEMO_"


@dataclass(frozen=True)
class DemoWarehouseSpec:
    """One demo warehouse + the workload it gets paired with.

    Fields:
        short_name:        identifier WITHOUT the SNOWTUNER_DEMO_ prefix
                           (e.g. "MEMORY_HOG_WH").  warehouse_name() prepends.
        size:              WAREHOUSE_SIZE clause value (XSMALL | SMALL | ...).
        auto_suspend_seconds:
                           AUTO_SUSPEND clause.  Capped at 120 across the
                           board so a crashed run can't burn many credits.
        workload_key:      key into DEMO_WORKLOADS registry.
        expected_finding:  one-line human description of which recommender
                           rule this warehouse is engineered to trip.  Shown
                           by ``snowtuner demo status``.
    """
    short_name: str
    size: str
    auto_suspend_seconds: int
    workload_key: str
    expected_finding: str

    @property
    def warehouse_name(self) -> str:
        """Fully-qualified Snowflake warehouse name with the demo prefix."""
        return f"{DEMO_WAREHOUSE_PREFIX}{self.short_name}"


# ── The 6 cooked warehouses ────────────────────────────────────────────────
#
# Sizes deliberately chosen relative to the workload so the right-sizer's
# thresholds fire:
#   Rule 2 (>=20% local spill -> +1)        : MEMORY_HOG     (XSMALL + 1.5B-key distincts, deep spill)
#   Rule 2 (>=20% local spill -> +1)        : LOCAL_SPILL_WH (SMALL + same distincts, moderate spill)
#   Rule 3 (avg queue >=5s, n>=30 -> +1)    : SATURATED      (SMALL + 80 concurrent CPU-bound)
#   Rule 4 (p99 <=1s, n>=100 -> -1)         : OVERKILL       (LARGE + trivial queries)
#
# Rule 1 (any remote spill -> +1) is attempted but not guaranteed:
# MEMORY_HOG includes one monster query (6B-key distinct on SF1000
# LINEITEM, ~150-180 GB hash state) sized to potentially exceed an
# XSMALL node's local SSD - the actual remote-spill trigger.  Whether it
# tips over depends on the node's disk config.  If it does, Rule 1
# fires instead of Rule 2 - same upsize, stronger evidence.
#
# Every spill/queue warehouse must also clear the right-sizer's readiness
# gate of >=30 SUCCESS queries in the window (MIN_QUERIES_FOR_READINESS) -
# this is why the spill workloads pad with light queries.  Dogfood round 1
# missed this: 11 queries of perfect spill would still produce no rec.
#
# Auto-suspend recommender:
#   BURSTY uses AUTO_SUSPEND=120 with 12 cycles of burst + explicit
#   ALTER WAREHOUSE SUSPEND + 180s idle.  The explicit suspend exists
#   because Snowflake's auto-suspend timing is approximate - dogfood
#   showed AS=120 inside a 150s gap produced ZERO suspend events.  Twelve
#   cycles clears MIN_CYCLES_PER_WAREHOUSE=10 with margin; the proposed
#   AUTO_SUSPEND lands well below 120, past MIN_DELTA_SECONDS=30.
#
# Control:
#   HEALTHY is sized appropriately for its workload - no recommendation
#   expected.  Proves the optimizer doesn't fabricate findings.

DEMO_SPECS: tuple[DemoWarehouseSpec, ...] = (
    DemoWarehouseSpec(
        short_name="MEMORY_HOG_WH",
        size="XSMALL",
        auto_suspend_seconds=60,
        workload_key="memory_hog",
        expected_finding=(
            "Right-sizer Rule 2: sustained local spill (memory-bound on "
            "XSMALL) -> upsize to SMALL.  Rule 1 (remote spill) possible "
            "if the monster query exceeds local disk"
        ),
    ),
    DemoWarehouseSpec(
        short_name="LOCAL_SPILL_WH",
        size="SMALL",
        auto_suspend_seconds=60,
        workload_key="local_spill",
        expected_finding=(
            "Right-sizer Rule 2: >=20% of queries spilled to local -> upsize to MEDIUM"
        ),
    ),
    DemoWarehouseSpec(
        short_name="SATURATED_WH",
        size="SMALL",
        auto_suspend_seconds=60,
        workload_key="saturated",
        expected_finding=(
            "Right-sizer Rule 3: avg queue overload >=5s -> upsize to MEDIUM"
        ),
    ),
    DemoWarehouseSpec(
        short_name="OVERKILL_WH",
        size="LARGE",
        auto_suspend_seconds=60,
        workload_key="overkill",
        expected_finding=(
            "Right-sizer Rule 4: p99 <=1s with no spill/queueing -> downsize to MEDIUM"
        ),
    ),
    DemoWarehouseSpec(
        short_name="BURSTY_WH",
        size="SMALL",
        auto_suspend_seconds=120,
        workload_key="bursty",
        expected_finding=(
            "Auto-suspend survival: ~3 min reactivation gaps with "
            "AUTO_SUSPEND=120 -> propose lower AUTO_SUSPEND"
        ),
    ),
    DemoWarehouseSpec(
        short_name="HEALTHY_WH",
        size="SMALL",
        auto_suspend_seconds=60,
        workload_key="healthy",
        expected_finding=(
            "Control - sized appropriately, no recommendation expected"
        ),
    ),
)


def find_spec(short_name: str) -> DemoWarehouseSpec | None:
    """Look up a spec by its short_name (without prefix).  None if missing."""
    short_name = short_name.upper()
    for spec in DEMO_SPECS:
        if spec.short_name == short_name:
            return spec
    return None
