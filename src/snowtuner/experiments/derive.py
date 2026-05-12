"""derive_actions — convert a winning experiment arm into the list of Actions
that, applied to the target warehouse, realize the arm's configuration.

Default implementation produces a single ``AlterWarehouse`` covering every
non-None field in the delta.  Recipes can override if their arm composition
needs other action types (Tier-2+ scenarios involving routing rules, etc.).

Returning a *list* of actions rather than a single action future-proofs for
composite recommendations even when v0.2.0 always emits one element.
"""
from __future__ import annotations

from typing import Any

from snowtuner.actions import AlterWarehouse, WarehouseKnob
from snowtuner.actions.alter_warehouse import KnobChange
from snowtuner.actions.base import Action
from snowtuner.experiments.arm import Arm
from snowtuner.experiments.axes import Generation, QASState
from snowtuner.experiments.config_delta import WarehouseConfig


# Maps WarehouseConfigDelta field → (WarehouseKnob, value-coercion function)
# applied to the proposed value when building the KnobChange.
_FIELD_TO_KNOB: dict[str, tuple[WarehouseKnob, Any]] = {
    "size": (WarehouseKnob.WAREHOUSE_SIZE, lambda v: v),
    "generation": (WarehouseKnob.GENERATION, lambda v: v.value if isinstance(v, Generation) else str(v)),
    "qas_state": (WarehouseKnob.ENABLE_QUERY_ACCELERATION, lambda v: v == QASState.ON),
    "qas_max_scale_factor": (WarehouseKnob.QUERY_ACCELERATION_MAX_SCALE_FACTOR, int),
}


def derive_actions(arm: Arm, control: WarehouseConfig) -> list[Action]:
    """Build the list of Actions that, applied in order, realize the arm's
    config on the control warehouse.

    For Tier-1 arms this always returns a single-element list containing one
    ``AlterWarehouse`` that sets every non-None field in the arm's delta.
    The control's current values are used to populate ``KnobChange.current_value``
    so generated SQL has correct rollback statements.
    """
    if arm.is_control:
        return []

    changes: list[KnobChange] = []
    delta_fields = arm.delta.fields_set()
    for field in delta_fields:
        if field not in _FIELD_TO_KNOB:
            # Unknown delta field — should be unreachable since the delta
            # model is closed.  Surface loudly if we ever extend the delta
            # without extending this map.
            raise ValueError(
                f"derive_actions has no mapping for delta field {field!r}; "
                f"add it to _FIELD_TO_KNOB"
            )
        knob, coerce = _FIELD_TO_KNOB[field]
        proposed = coerce(getattr(arm.delta, field))
        current = _current_for(control, field)
        changes.append(KnobChange(
            knob=knob,
            current_value=current,
            proposed_value=proposed,
        ))

    return [AlterWarehouse(warehouse_name=control.name, changes=changes)]


def _current_for(control: WarehouseConfig, field: str) -> Any:
    """Extract the current value of a config field from the control warehouse,
    coerced to the same Python type the proposed value will use."""
    if field == "size":
        return control.size
    if field == "generation":
        return control.generation.value if control.generation else None
    if field == "qas_state":
        if control.qas_state == QASState.ON:
            return True
        if control.qas_state == QASState.OFF:
            return False
        return None
    if field == "qas_max_scale_factor":
        return control.qas_max_scale_factor
    return None
