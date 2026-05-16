"""Saved query groups — reusable named subsets of the ingested query history.

Two kinds:

* **static**: members are snapshotted at creation time.  The group is a
  frozen list of ``query_id``s for reproducibility (re-run an experiment
  six months later on exactly the same queries).
* **dynamic**: members are re-evaluated against ``raw.query_history`` on
  every read.  Filters are persisted as the canonical definition; the
  current member list is whatever currently matches.

Groups are immutable in this slice — no edit-in-place.  Versioning
(promotion / demotion / filter changes with v0→v1→v2) is a future concern.
"""
from snowtuner.query_groups.model import (
    QueryFilterSpec,
    QueryGroup,
    QueryGroupKind,
)
from snowtuner.query_groups.store import QueryGroupStore

__all__ = [
    "QueryFilterSpec",
    "QueryGroup",
    "QueryGroupKind",
    "QueryGroupStore",
]
