"""Unit tests for ``snowtuner.experiments.derive``.

``derive_actions`` translates a winning experiment arm's delta into a
list of concrete Actions that become the basis of the derived
Recommendation.  The math: take an Arm's WarehouseConfigDelta and emit
one AlterWarehouse per knob that differs from control.
"""
from __future__ import annotations

from snowtuner.actions.alter_warehouse import AlterWarehouse, WarehouseKnob
from snowtuner.experiments.arm import Arm
from snowtuner.experiments.axes import Generation, QASState
from snowtuner.experiments.config_delta import WarehouseConfig, WarehouseConfigDelta
from snowtuner.experiments.derive import derive_actions
from snowtuner.recommenders.sizes import normalize as normalize_size


_CONTROL = WarehouseConfig(
    name="ETL_WH",
    size=normalize_size("MEDIUM"),
    auto_suspend_seconds=60,
    auto_resume=True,
    generation=Generation.GEN1,
    qas_state=QASState.OFF,
)


def _arm(name: str, delta: WarehouseConfigDelta) -> Arm:
    return Arm.from_delta(delta, name=name)


class TestSingleKnobArms:
    """An arm that changes one knob produces one AlterWarehouse with one
    KnobChange."""

    def test_size_change(self):
        arm = _arm("size_up", WarehouseConfigDelta(size=normalize_size("LARGE")))
        actions = derive_actions(arm, _CONTROL)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, AlterWarehouse)
        assert action.warehouse_name == "ETL_WH"
        knob_set = {c.knob for c in action.changes}
        assert WarehouseKnob.WAREHOUSE_SIZE in knob_set

    def test_generation_change(self):
        arm = _arm("gen2", WarehouseConfigDelta(generation=Generation.GEN2))
        actions = derive_actions(arm, _CONTROL)
        assert len(actions) == 1
        knobs = {c.knob for c in actions[0].changes}
        assert WarehouseKnob.GENERATION in knobs

    def test_qas_change(self):
        arm = _arm("qas_on", WarehouseConfigDelta(qas_state=QASState.ON))
        actions = derive_actions(arm, _CONTROL)
        assert len(actions) == 1
        knobs = {c.knob for c in actions[0].changes}
        assert WarehouseKnob.ENABLE_QUERY_ACCELERATION in knobs


class TestEmptyDelta:
    """The implicit control arm has no delta — derive should return no
    actions because there's nothing to change."""

    def test_control_arm_produces_no_actions(self):
        ctrl_arm = Arm.control()
        actions = derive_actions(ctrl_arm, _CONTROL)
        assert actions == []


class TestMultiKnobArm:
    """An arm with multiple knob deltas (factorial recipe) produces an
    AlterWarehouse with multiple KnobChanges."""

    def test_size_and_generation(self):
        delta = WarehouseConfigDelta(
            size=normalize_size("LARGE"),
            generation=Generation.GEN2,
        )
        arm = _arm("gen2_size_LARGE", delta)
        actions = derive_actions(arm, _CONTROL)
        assert len(actions) == 1
        knobs = {c.knob for c in actions[0].changes}
        assert WarehouseKnob.WAREHOUSE_SIZE in knobs
        assert WarehouseKnob.GENERATION in knobs


class TestQASScaleFactor:
    """If qas_state changes AND a max_scale_factor is set, both knobs
    appear; if only qas_state changes, only that one."""

    def test_state_only(self):
        arm = _arm("qas_on", WarehouseConfigDelta(qas_state=QASState.ON))
        actions = derive_actions(arm, _CONTROL)
        knobs = {c.knob for c in actions[0].changes}
        # max_scale_factor isn't in the delta → shouldn't appear
        assert WarehouseKnob.QUERY_ACCELERATION_MAX_SCALE_FACTOR not in knobs

    def test_state_and_max(self):
        arm = _arm(
            "qas_on_capped",
            WarehouseConfigDelta(qas_state=QASState.ON, qas_max_scale_factor=8),
        )
        actions = derive_actions(arm, _CONTROL)
        knobs = {c.knob for c in actions[0].changes}
        assert WarehouseKnob.ENABLE_QUERY_ACCELERATION in knobs
        assert WarehouseKnob.QUERY_ACCELERATION_MAX_SCALE_FACTOR in knobs
