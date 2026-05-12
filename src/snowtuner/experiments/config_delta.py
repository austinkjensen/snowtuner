"""WarehouseConfigDelta — typed "diff from control" for an experiment arm.

Each field is optional; absence means "inherit from the control warehouse."
A control arm is constructed with no fields set (empty delta).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator

from snowtuner.experiments.axes import Generation, QASState
from snowtuner.recommenders.sizes import normalize as normalize_size


class WarehouseConfigDelta(BaseModel):
    """Subset of ``CREATE WAREHOUSE`` knobs an experiment arm can vary.

    Only fields explicitly set are interpreted as overrides; everything else
    inherits from the control warehouse's config.
    """
    generation:           Generation | None = None
    size:                 str | None = None  # canonical size string (e.g. "MEDIUM")
    qas_state:            QASState | None = None
    qas_max_scale_factor: int | None = None

    def is_noop(self) -> bool:
        """Empty delta = control arm.  The engine refuses to run an experiment
        without exactly one such arm."""
        return self.model_dump(exclude_none=True) == {}

    def fields_set(self) -> list[str]:
        """Names of explicitly-set fields, sorted.  Used for arm naming and
        eligibility error messages."""
        return sorted(self.model_dump(exclude_none=True).keys())

    @model_validator(mode="after")
    def _validate_size(self) -> "WarehouseConfigDelta":
        """Normalize size to canonical form (XSMALL / SMALL / … / X6LARGE).
        Reject unknown sizes loudly rather than letting them flow to Snowflake."""
        if self.size is not None:
            canonical = normalize_size(self.size)
            if canonical is None:
                raise ValueError(
                    f"unknown size {self.size!r} — must be one of "
                    f"XSMALL / SMALL / MEDIUM / LARGE / XLARGE / X2LARGE / "
                    f"X3LARGE / X4LARGE / X5LARGE / X6LARGE"
                )
            self.size = canonical
        return self

    @model_validator(mode="after")
    def _validate_qas_consistency(self) -> "WarehouseConfigDelta":
        """qas_max_scale_factor only makes sense when QAS is on (or
        unspecified, in which case we inherit from control)."""
        if (
            self.qas_state == QASState.OFF
            and self.qas_max_scale_factor is not None
            and self.qas_max_scale_factor > 0
        ):
            raise ValueError(
                "qas_max_scale_factor > 0 is incompatible with qas_state=OFF"
            )
        return self


class WarehouseConfig(BaseModel):
    """Snapshot of a warehouse's current config — what the control arm
    inherits from.  Populated from ``raw.warehouses`` at proposal time.

    Distinct from ``raw.warehouses``'s row shape because we want a strongly-typed
    object that recipes can reason about, not a dict of mixed-type columns.
    """
    name: str
    size: str | None = None
    generation: Generation | None = None
    qas_state: QASState | None = None
    qas_max_scale_factor: int | None = None
    auto_suspend_seconds: int | None = None
    auto_resume: bool | None = None

    def merge(self, delta: WarehouseConfigDelta) -> "WarehouseConfig":
        """Apply a delta on top of this config.  Used by the engine to compute
        the actual ``CREATE WAREHOUSE`` parameters for an arm."""
        merged = self.model_dump()
        for k in delta.fields_set():
            merged[k] = getattr(delta, k)
        return WarehouseConfig(**merged)

    def to_create_warehouse_clauses(self) -> dict[str, Any]:
        """Render to a dict suitable for building a CREATE WAREHOUSE statement."""
        out: dict[str, Any] = {}
        if self.size is not None:
            out["WAREHOUSE_SIZE"] = self.size
        if self.generation is not None:
            out["GENERATION"] = self.generation.value
        if self.qas_state == QASState.ON:
            out["ENABLE_QUERY_ACCELERATION"] = True
        elif self.qas_state == QASState.OFF:
            out["ENABLE_QUERY_ACCELERATION"] = False
        if self.qas_max_scale_factor is not None:
            out["QUERY_ACCELERATION_MAX_SCALE_FACTOR"] = self.qas_max_scale_factor
        if self.auto_suspend_seconds is not None:
            out["AUTO_SUSPEND"] = self.auto_suspend_seconds
        if self.auto_resume is not None:
            out["AUTO_RESUME"] = self.auto_resume
        return out
