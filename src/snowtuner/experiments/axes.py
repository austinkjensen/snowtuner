"""Axes — the canonical, finite list of dimensions an experiment arm can vary.

Adding a new axis is a deliberate code change: extend ``WarehouseConfigDelta``
with the new field, add eligibility logic to ``eligibility.py``, and add
rendering to ``alter_warehouse.py``'s ``WarehouseKnob`` enum.

The Tier-1 axes shipping in v0.2.0 are:

    generation              Gen1 / Gen2
    size                    XSMALL → X6LARGE (canonical via recommenders.sizes)
    qas_state               QAS off / on
    qas_max_scale_factor    integer (only meaningful when qas_state = ON)

Tier 2 will add cluster_count_min/max, scaling_policy, max_concurrency_level.
"""
from __future__ import annotations

from enum import Enum


class Generation(str, Enum):
    """Snowflake warehouse generation.  Set via ``GENERATION = '1' | '2'``."""
    GEN1 = "1"
    GEN2 = "2"


class QASState(str, Enum):
    """Whether Query Acceleration Service is enabled on the warehouse."""
    OFF = "off"
    ON = "on"
