"""Arm — one cell in an experiment's matrix."""
from __future__ import annotations

from pydantic import BaseModel, Field

from snowtuner.actions.base import Issue
from snowtuner.experiments.config_delta import WarehouseConfigDelta


class Arm(BaseModel):
    """One configuration point in an experiment.

    The control arm has an empty delta — its warehouse is the user's existing
    one, untouched.  Non-control arms describe a new warehouse that will be
    spun up for the experiment with the delta applied.
    """
    name: str  # auto-generated for presets; user-editable for custom arms
    delta: WarehouseConfigDelta
    # Frozen at proposal time.  None = not yet estimated.
    estimated_cost_credits: float | None = None
    # Eligibility issues recorded when the arm is built; recipes drop arms
    # that have severity='error' issues, surface 'warning' issues to the UI.
    eligibility_issues: list[Issue] = Field(default_factory=list)

    @property
    def is_control(self) -> bool:
        return self.delta.is_noop()

    @property
    def has_blocking_issues(self) -> bool:
        return any(i.severity == "error" for i in self.eligibility_issues)

    @classmethod
    def control(cls) -> "Arm":
        return cls(name="control", delta=WarehouseConfigDelta())

    @classmethod
    def from_delta(cls, delta: WarehouseConfigDelta, *, name: str | None = None) -> "Arm":
        """Build a non-control arm.  Default name is derived from delta fields,
        e.g. ``gen2_size_LARGE``."""
        if delta.is_noop():
            raise ValueError("control arm should be created via Arm.control()")
        if name is None:
            name = _auto_name_from_delta(delta)
        return cls(name=name, delta=delta)


def _auto_name_from_delta(delta: WarehouseConfigDelta) -> str:
    """Generate a stable, human-readable arm name from a non-empty delta.

    Examples:
      generation=GEN2                        → 'gen2'
      generation=GEN2, size=LARGE            → 'gen2_size_LARGE'
      qas_state=ON, qas_max_scale_factor=4   → 'qas_on_scale_4'
    """
    parts: list[str] = []
    if delta.generation is not None:
        parts.append(f"gen{delta.generation.value}")
    if delta.size is not None:
        parts.append(f"size_{delta.size}")
    if delta.qas_state is not None:
        parts.append(f"qas_{delta.qas_state.value}")
    if delta.qas_max_scale_factor is not None:
        parts.append(f"scale_{delta.qas_max_scale_factor}")
    return "_".join(parts) or "arm"
