"""Snowflake WAREHOUSE_EVENTS_HISTORY event-name vocabulary.

Snowflake records warehouse suspend/resume activity under TWO vocabularies,
depending on account version / behavior-change bundle:

  legacy:   SUSPEND_WAREHOUSE / RESUME_WAREHOUSE
  current:  SUSPEND_CLUSTER  / RESUME_CLUSTER    (per-cluster rows; a
            single-cluster warehouse emits exactly one row per event)

Dogfood 2026-06-11: a freshly-created account emitted ONLY the *_CLUSTER
variants - not one *_WAREHOUSE row in the entire view - and every consumer
filtering on the legacy names silently saw zero events, so the auto-suspend
recommender never fired.  The mismatch survived testing because the
synthetic seed (seed/generate.py) emits the legacy names: the recommender
was validated against data generated to match its own filter.  Circular.

Every consumer of suspend/resume events MUST use these tuples instead of
hard-coding names.  On multi-cluster warehouses the *_CLUSTER vocabulary
emits one row per cluster per event; consumers that pair adjacent
suspend->resume events tolerate this (consecutive same-kind rows collapse
into one gap), but consumers that COUNT rows should treat counts as
approximate cycle multiples, not exact cycles.
"""
from __future__ import annotations

SUSPEND_EVENT_NAMES: tuple[str, ...] = ("SUSPEND_WAREHOUSE", "SUSPEND_CLUSTER")
RESUME_EVENT_NAMES: tuple[str, ...] = ("RESUME_WAREHOUSE", "RESUME_CLUSTER")
SUSPEND_RESUME_EVENT_NAMES: tuple[str, ...] = (
    SUSPEND_EVENT_NAMES + RESUME_EVENT_NAMES
)


def sql_in_list(names: tuple[str, ...]) -> str:
    """Render names as a SQL IN-list body: ``'A', 'B'``.

    Names are compile-time constants from this module, not user input, so
    quoting without escaping is safe.
    """
    return ", ".join(f"'{n}'" for n in names)
