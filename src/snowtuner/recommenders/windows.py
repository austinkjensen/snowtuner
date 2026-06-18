"""Configurable consideration windows for recommenders.

Every recommender aggregates some slice of ``raw.*`` / ``features.*``
history to decide whether to fire.  This module is the single place that
decides HOW FAR BACK that slice reaches.

Scope: read-side only.  Ingestion (sync watermarks, initial lookback,
backfill) is untouched - ``raw.*`` retention is governed there, and these
windows merely bound what the recommenders READ from whatever has been
ingested.  A window larger than the ingested history degrades gracefully
to "everything we have".

Resolution order (first hit wins)
---------------------------------
1. ``SNOWTUNER_WINDOW_DAYS__<RECOMMENDER>``  - per-recommender override,
   recommender name uppercased verbatim (it already contains underscores),
   e.g. ``SNOWTUNER_WINDOW_DAYS__RULE_BASED_RIGHT_SIZER=30``.
2. ``SNOWTUNER_WINDOW_DAYS``                 - global override for every
   recommender at once.
3. Built-in per-recommender default (``DEFAULT_WINDOW_DAYS``), falling
   back to ``FALLBACK_WINDOW_DAYS`` for recommenders not listed.

Value semantics
---------------
Positive integer = days of history considered.  ``0`` = unbounded (all
ingested history) - this is how the auto-suspend tuner's pre-2026-06
behavior can be restored.  Values above ``MAX_WINDOW_DAYS`` clamp; junk
values log a warning and fall through to the next resolution level.

Per-warehouse escape hatches (RESERVED - not applied yet)
---------------------------------------------------------
Two seams exist for later per-(warehouse, recommender) configuration:

* ``resolve_window_days()`` accepts ``warehouse_name=``.  Call sites that
  have a warehouse in hand may pass it today; until per-warehouse support
  lands it does not change the result.
* The env name ``SNOWTUNER_WINDOW_DAYS__<RECOMMENDER>__<WAREHOUSE>`` is
  reserved.  Setting one today logs a warning that it is ignored, so the
  intent is discoverable rather than silently dropped.

The blocker for actually honoring per-warehouse values is that every
recommender computes its aggregates in ONE SQL pass across all warehouses
with a single window in the WHERE clause; honoring per-warehouse windows
means per-warehouse query passes (or a window column join).  That's a
deliberate later refactor, not a config change.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_GLOBAL_ENV = "SNOWTUNER_WINDOW_DAYS"
_PER_REC_ENV_PREFIX = "SNOWTUNER_WINDOW_DAYS__"

#: 0 as a window value means "all ingested history".
UNBOUNDED = 0

MAX_WINDOW_DAYS = 3650

#: Built-in defaults.  The four query-history aggregators keep their
#: historical 14 days.  The auto-suspend survival tuner gets 30: it needs
#: more runway to accumulate >=10 modelable idle gaps on low-traffic
#: warehouses, and its pre-2026-06 behavior (unbounded -> proposals
#: drifting as history accumulates) is restorable with an explicit 0.
DEFAULT_WINDOW_DAYS: dict[str, int] = {
    "rule_based_right_sizer": 14,
    "multi_cluster_reducer": 14,
    "qas_candidate_finder": 14,
    "gen2_candidate_finder": 14,
    "auto_suspend_survival_tuner": 30,
}
FALLBACK_WINDOW_DAYS = 14


@dataclass(frozen=True)
class WindowResolution:
    """A resolved consideration window plus its provenance.

    ``source`` is one of ``'default'``, ``'env:SNOWTUNER_WINDOW_DAYS'``,
    or ``'env:SNOWTUNER_WINDOW_DAYS__<RECOMMENDER>'`` - stored in
    model_state / readiness signals so an operator can see WHY a window
    is what it is.
    """
    days: int            # UNBOUNDED (0) means all ingested history
    source: str

    @property
    def is_unbounded(self) -> bool:
        return self.days == UNBOUNDED

    def sql_filter(self, column: str) -> str:
        """Render the window as a SQL predicate on ``column``.

        Returns the literal ``TRUE`` when unbounded so callers can embed
        it unconditionally: ``WHERE {win.sql_filter('start_time')} AND ...``.
        ``column`` is a code-supplied identifier, never user input.
        """
        if self.is_unbounded:
            return "TRUE"
        return f"{column} >= now() - INTERVAL {self.days} DAY"

    def describe(self) -> str:
        """Human-readable form for rationale text and CLI display.

        Phrased so ``f"over {win.describe()}"`` reads naturally in both
        modes: "over the last 14 days" / "over all ingested history".
        """
        if self.is_unbounded:
            return "all ingested history"
        return f"the last {self.days} days"

    def weekly_factor(self, observed_span_days: float | None) -> float:
        """Multiplier that converts a window-total into a per-week rate.

        Bounded windows keep the historical semantics (``7 / window``).
        Unbounded windows have no fixed denominator, so normalize by the
        observed span of the data instead - callers pass
        ``date_diff('day', MIN(ts), MAX(ts))`` from the same query that
        produced the total.  Floored at one day so a single burst of
        activity doesn't extrapolate to infinity.
        """
        if not self.is_unbounded:
            return 7.0 / self.days
        return 7.0 / max(float(observed_span_days or 1.0), 1.0)


def _parse_days(raw: str, env_name: str) -> int | None:
    """Parse one env value.  None = invalid, caller falls through."""
    try:
        days = int(raw.strip())
    except (ValueError, AttributeError):
        logger.warning(
            "%s=%r is not an integer - ignoring this override", env_name, raw,
        )
        return None
    if days < 0:
        logger.warning(
            "%s=%d is negative - ignoring this override (use 0 for unbounded)",
            env_name, days,
        )
        return None
    if days > MAX_WINDOW_DAYS:
        logger.warning(
            "%s=%d exceeds the %d-day cap - clamping",
            env_name, days, MAX_WINDOW_DAYS,
        )
        return MAX_WINDOW_DAYS
    return days


def _warn_reserved_per_warehouse(recommender_name: str) -> None:
    """Surface (don't silently ignore) reserved per-warehouse overrides."""
    prefix = f"{_PER_REC_ENV_PREFIX}{recommender_name.upper()}__"
    for key in os.environ:
        if key.startswith(prefix):
            logger.warning(
                "%s looks like a per-warehouse window override; "
                "per-(warehouse, recommender) windows are reserved for a "
                "future release and this value is currently IGNORED.  "
                "Set %s%s or %s instead.",
                key, _PER_REC_ENV_PREFIX, recommender_name.upper(), _GLOBAL_ENV,
            )


def resolve_window_days(
    recommender_name: str,
    *,
    warehouse_name: str | None = None,
) -> WindowResolution:
    """Resolve the consideration window for one recommender.

    ``warehouse_name`` is accepted for forward compatibility with
    per-warehouse windows; it currently does not affect the result (see
    module docstring).  Reads the environment on every call - resolution
    happens once per recommender per pipeline run, so caching would only
    buy staleness.
    """
    del warehouse_name  # reserved - see module docstring

    _warn_reserved_per_warehouse(recommender_name)

    per_rec_env = f"{_PER_REC_ENV_PREFIX}{recommender_name.upper()}"
    raw = os.environ.get(per_rec_env)
    if raw is not None:
        days = _parse_days(raw, per_rec_env)
        if days is not None:
            return WindowResolution(days=days, source=f"env:{per_rec_env}")

    raw = os.environ.get(_GLOBAL_ENV)
    if raw is not None:
        days = _parse_days(raw, _GLOBAL_ENV)
        if days is not None:
            return WindowResolution(days=days, source=f"env:{_GLOBAL_ENV}")

    return WindowResolution(
        days=DEFAULT_WINDOW_DAYS.get(recommender_name, FALLBACK_WINDOW_DAYS),
        source="default",
    )
