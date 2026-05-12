"""snowtuner experiments — v0.2.

The third loop on top of observe → recommend → apply: actively run sample
queries against side-by-side warehouse configurations to gather the data
needed for confident recommendations.

Public surface:
  - axes / WarehouseConfigDelta — the typed knobs an arm can vary
  - Arm — one cell in an experiment's matrix
  - ProposedExperiment / Experiment / ExperimentReport — domain models
  - recipes — preset arm configurations (gen1→gen2, size sweep, …)
  - eligibility / cost_estimate / sampling — the runtime-engine inputs

The engine itself (replay, cleanup, cost monitoring) lives in
``snowtuner.experiments.engine`` and is not part of this primitive layer.
"""
from snowtuner.experiments.axes import Generation, QASState
from snowtuner.experiments.config_delta import WarehouseConfigDelta
from snowtuner.experiments.arm import Arm
from snowtuner.experiments.cost_estimate import CostEstimate
from snowtuner.experiments.model import (
    ArmObservation,
    Experiment,
    ExperimentReport,
    ExperimentRun,
    ExperimentStatus,
    ProposedExperiment,
    RunStatus,
)
from snowtuner.experiments.eligibility import check_arm_eligibility
from snowtuner.experiments.derive import derive_actions
from snowtuner.experiments.engine import EngineConfig, ExperimentEngine
from snowtuner.experiments.store import ExperimentStore

__all__ = [
    "Arm",
    "ArmObservation",
    "CostEstimate",
    "EngineConfig",
    "Experiment",
    "ExperimentEngine",
    "ExperimentReport",
    "ExperimentRun",
    "ExperimentStatus",
    "ExperimentStore",
    "Generation",
    "ProposedExperiment",
    "QASState",
    "RunStatus",
    "WarehouseConfigDelta",
    "check_arm_eligibility",
    "derive_actions",
]
