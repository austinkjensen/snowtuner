"""In-process recommender registry.

Built-in recommenders are registered explicitly in :func:`default_registry`.
The ``Recommender`` base class and this registry are kept pluggable internally
so new in-tree recommenders slot in cleanly, but there is no third-party
discovery mechanism: v0.1 targets a tight, curated set of recommenders.
"""
from __future__ import annotations

from snowtuner.recommenders.base import Recommender


class RecommenderRegistry:
    def __init__(self) -> None:
        self._recommenders: dict[str, Recommender] = {}

    def register(self, recommender: Recommender) -> None:
        name = recommender.name
        if name in self._recommenders:
            raise ValueError(f"Recommender {name!r} already registered")
        self._recommenders[name] = recommender

    def unregister(self, name: str) -> None:
        self._recommenders.pop(name, None)

    def get(self, name: str) -> Recommender | None:
        return self._recommenders.get(name)

    def all(self) -> list[Recommender]:
        return list(self._recommenders.values())

    def names(self) -> list[str]:
        return list(self._recommenders.keys())


def default_registry() -> RecommenderRegistry:
    """Return a registry populated with the built-in recommenders.

    Note: ``RuleBasedRightSizer`` and ``SpillAwareRightSizer`` both target
    ALTER_WAREHOUSE on the same warehouses, so running both at once causes
    them to mutually-supersede each other's proposals.  Until proper ensemble
    logic lands, only the rule-based one is registered by default; swap to
    the spill-aware one by editing this function.
    """
    from snowtuner.recommenders.builtins.auto_suspend_survival import (
        AutoSuspendSurvivalTuner,
    )
    from snowtuner.recommenders.builtins.gen2_candidate_finder import (
        Gen2CandidateFinder,
    )
    from snowtuner.recommenders.builtins.multi_cluster_reducer import (
        MultiClusterReducer,
    )
    from snowtuner.recommenders.builtins.qas_candidate_finder import (
        QASCandidateFinder,
    )
    from snowtuner.recommenders.builtins.rule_based_right_sizer import (
        RuleBasedRightSizer,
    )

    reg = RecommenderRegistry()
    reg.register(AutoSuspendSurvivalTuner())
    reg.register(RuleBasedRightSizer())
    # Direct-recommendation recommender for multi-cluster waste — pure
    # observational, no experiment needed (peak observed cluster fully
    # determines safe bounds).
    reg.register(MultiClusterReducer())
    # Candidate finders — emit experiment proposals only, no direct recs.
    # The eventual AlterWarehouse rec comes from experiments/derive.py once
    # the proposed experiment completes.
    reg.register(Gen2CandidateFinder())
    reg.register(QASCandidateFinder())
    return reg
